"""多渠道告警推送 — 飞书/钉钉/企微/通用Webhook/邮件

借鉴 TrendRadar 的多渠道推送设计。所有渠道并发分发, 单渠道失败
不影响其他渠道; 推送结果落库(AlertRecord)防重发、可审计。
摘要节流: 常规内容按 digest_throttle_minutes 合并降噪, 高优先级
(负面舆情)可绕过节流立即送达。
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr

import httpx

from .config import NotifyConfig
from .db import AlertRecord, Analysis, Database, Note

log = logging.getLogger("notify")


class Notifier:
    def __init__(self, cfg: NotifyConfig, db: Database):
        self.cfg = cfg
        self.db = db
        self._last_digest_at: datetime | None = None

    # ---------- 对外 ----------

    async def send_digest(self, alerts: list[tuple[Note, Analysis]]) -> bool:
        """发送告警摘要. alerts 为 (note, analysis) 列表.

        返回是否实际发送(节流期内且非紧急则跳过)。
        """
        if not alerts:
            return False
        now = datetime.now()
        critical = any(a.sentiment == "negative" for _, a in alerts)

        # 节流
        if self._last_digest_at and self.cfg.digest_throttle_minutes > 0:
            due = self._last_digest_at + timedelta(
                minutes=self.cfg.digest_throttle_minutes)
            if now < due and not (critical and self.cfg.critical_bypass_throttle):
                log.info("推送节流中, %d 条告警暂缓 (下次 %s)",
                         len(alerts), due.strftime("%H:%M"))
                return False

        text = self._build_text(alerts)
        sent_channels = await self._dispatch(text, critical)
        self._last_digest_at = now
        for note, _ in alerts:
            for ch in sent_channels:
                self._record(note.note_id, ch, True, "")
        log.info("告警摘要已推送: %d 条笔记 -> %d 渠道",
                 len(alerts), len(sent_channels))
        return True

    async def send_supervisor(self, rationale: str, results: list) -> None:
        """监督层策略调整告警 — 推送 LLM 决策依据与执行结果, 供人工审计."""
        lines = ["🤖 [小红书监控·监督层] 已自动调整采集策略", ""]
        lines.append(f"判断依据: {rationale or '(未提供)'}")
        lines.append("")
        for r in results:
            if r.get("ok"):
                lines.append(f"✅ {r.get('tool')} {r.get('args')} — "
                             f"{r.get('detail', '')}")
            else:
                lines.append(f"❌ {r.get('tool')} {r.get('args')} — "
                             f"{r.get('error', '')}")
        text = "\n".join(lines)
        await self._dispatch(text, critical=False)

    # ---------- 组装 ----------

    def _build_text(self, alerts: list[tuple[Note, Analysis]]) -> str:
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        lines = [f"[小红书监控] {now} 新增 {len(alerts)} 条关注内容", ""]
        for note, a in alerts[:30]:
            flag = "🔴" if a.sentiment == "negative" else "🟡"
            rules = ",".join(a.matched_rules) or "-"
            summary = a.summary or (note.desc or note.title or "")[:60]
            lines.append(
                f"{flag} [{a.category}] {note.title or '(无标题)'}\n"
                f"   作者: {note.user_nickname} | 点赞: {note.liked_count} | "
                f"相关度: {a.relevance:.0f} | 规则: {rules}\n"
                f"   摘要: {summary}\n"
                f"   链接: {note.url}")
        if len(alerts) > 30:
            lines.append(f"... 其余 {len(alerts) - 30} 条略")
        return "\n".join(lines)

    # ---------- 分发 ----------

    async def _dispatch(self, text: str, critical: bool) -> list[str]:
        tasks: list[tuple[str, asyncio.Task]] = []

        for url in self.cfg.feishu_webhooks:
            tasks.append(("feishu", asyncio.ensure_future(self._feishu(url, text))))
        for url in self.cfg.dingtalk_webhooks:
            tasks.append(("dingtalk",
                          asyncio.ensure_future(self._dingtalk(url, text, critical))))
        for url in self.cfg.wecom_webhooks:
            tasks.append(("wecom", asyncio.ensure_future(self._wecom(url, text))))
        for url in self.cfg.generic_webhooks:
            tasks.append(("webhook", asyncio.ensure_future(self._generic(url, text))))
        if self.cfg.smtp_host and self.cfg.smtp_to:
            tasks.append(("email", asyncio.ensure_future(
                asyncio.to_thread(self._email, text))))

        sent = []
        for name, task in tasks:
            try:
                ok = await task
                if ok:
                    sent.append(name)
            except Exception as e:
                log.warning("推送渠道 [%s] 失败: %s", name, e)
        return sent

    async def _feishu(self, url: str, text: str) -> bool:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(url, json={"msg_type": "text",
                                        "content": {"text": text}})
            return r.status_code == 200

    async def _dingtalk(self, url: str, text: str, critical: bool) -> bool:
        title = "⚠️ 小红书负面舆情告警" if critical else "小红书内容监控摘要"
        # 钉钉 markdown 对文本长度敏感, 截断保护
        body = text[:18000]
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(url, json={
                "msgtype": "markdown",
                "markdown": {"title": title, "text": f"## {title}\n\n{body}"},
            })
            return r.status_code == 200

    async def _wecom(self, url: str, text: str) -> bool:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(url, json={
                "msgtype": "markdown",
                "markdown": {"content": text[:4000]},
            })
            return r.status_code == 200

    async def _generic(self, url: str, text: str) -> bool:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(url, json={
                "text": text,
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "source": "xhs-monitor-agent",
            })
            return 200 <= r.status_code < 300

    def _email(self, text: str) -> bool:
        import os
        pwd = os.environ.get(self.cfg.smtp_password_env, "")
        msg = MIMEText(text, "plain", "utf-8")
        msg["Subject"] = Header("小红书内容监控告警", "utf-8")
        msg["From"] = formataddr(("XHS Monitor", self.cfg.smtp_user))
        msg["To"] = ",".join(self.cfg.smtp_to)
        with smtplib.SMTP_SSL(self.cfg.smtp_host,
                              self.cfg.smtp_port, timeout=20) as s:
            s.login(self.cfg.smtp_user, pwd)
            s.sendmail(self.cfg.smtp_user, self.cfg.smtp_to,
                       msg.as_string())
        return True

    # ---------- 落库 ----------

    def _record(self, note_id: str, channel: str, ok: bool, detail: str):
        try:
            with self.db.session() as sess:
                sess.add(AlertRecord(note_id=note_id, channel=channel,
                                     ok=ok, detail=detail))
                sess.commit()
        except Exception as e:  # 推送记录失败不影响主流程
            log.debug("AlertRecord 写入失败: %s", e)
