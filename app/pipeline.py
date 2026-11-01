"""监控流水线 — 编排: 任务调度执行 -> 采集 -> 去重 -> 识别 -> 告警

单次周期(cycle)的执行契约:
  1. 取到期任务(Task.next_run_at <= now 且 enabled)
  2. 选号: 在健康账号中风控等级最低、空闲最久的(轮换降密度)
  3. 采集: 风控 check -> 搜索收割 -> (可选)详情页补正文
  4. 去重: note_id 精确去重 + SimHash 近似去重(识别内容农场)
  5. 识别: 规则引擎 L1 -> (可选) LLM 语义分析 L2 覆盖增强
  6. 告警: is_alert 的笔记进入摘要推送
  7. 风控上报: 成功/风险信号, 全程审计落库

任何一个任务失败不影响其他任务(故障隔离)。
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select

from .behavior import HumanBehavior
from .browser import BrowserManager
from .classifier import AnalysisResult, RuleClassifier
from .collector import ApiHarvestError, XhsCollector
from .db import Account, Analysis, Database, Note, Task
from .dedup import exact_hash, is_near_duplicate, simhash, simhash_prefilter
from .llm import LLMAnalyzer
from .notifier import Notifier
from .risk import RiskBlocked, RiskEngine

log = logging.getLogger("pipeline")


class CycleReport:
    def __init__(self):
        self.tasks_run = 0
        self.notes_new = 0
        self.notes_duplicate = 0
        self.alerts: list[tuple[Note, Analysis]] = []
        self.errors: list[str] = []
        self.risk_blocked: list[str] = []

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        return (f"任务 {self.tasks_run} | 新笔记 {self.notes_new} | "
                f"去重 {self.notes_duplicate} | 告警 {len(self.alerts)} | "
                f"错误 {len(self.errors)} | 风控拦截 {len(self.risk_blocked)}")


class MonitorPipeline:
    def __init__(self, cfg, db: Database, classifier: RuleClassifier,
                 llm: LLMAnalyzer, notifier: Notifier,
                 risk: RiskEngine, browsers: BrowserManager):
        self.cfg = cfg
        self.db = db
        self.classifier = classifier
        self.llm = llm
        self.notifier = notifier
        self.risk = risk
        self.browsers = browsers
        self.behavior = HumanBehavior(cfg.behavior)

    # ---------- 主入口 ----------

    async def run_cycle(self, task_ids: list[int] | None = None) -> CycleReport:
        report = CycleReport()
        with self.db.session() as sess:
            stmt = select(Task).where(Task.enabled.is_(True))
            if task_ids:
                stmt = stmt.where(Task.id.in_(task_ids))
            tasks = list(sess.scalars(stmt).all())

        now = datetime.now()
        for task in tasks:
            if task_ids is None and task.next_run_at and task.next_run_at > now:
                continue  # 未到期(手动触发则跳过该检查)
            result = None  # True=成功 / False=失败 / None=风控拦截(不计数)
            try:
                await self._run_task(task, report)
                result = True
            except RiskBlocked as e:
                report.risk_blocked.append(f"task#{task.id}({task.keyword}): {e}")
                log.warning("任务 [%s] 被风控拦截: %s", task.keyword, e)
            except Exception as e:
                report.errors.append(f"task#{task.id}({task.keyword}): {e}")
                log.exception("任务 [%s] 执行失败", task.keyword)
            finally:
                self._mark_task_failure(task.id, result)
                with self.db.session() as sess:
                    t = sess.get(Task, task.id)
                    if t:
                        t.last_run_at = now
                        t.next_run_at = now  # scheduler 会重算
                        sess.commit()

        if report.alerts:
            await self.notifier.send_digest(report.alerts)
        log.info("周期完成: %s", report.summary())
        return report

    # ---------- 单任务 ----------

    async def _run_task(self, task: Task, report: CycleReport) -> None:
        account = self._pick_account()
        if account is None:
            raise RiskBlocked("无可用账号(全部冷却/禁用/待登录)")

        # 风控检查(搜索动作)
        state = self.risk.check(account, "search")
        multiplier = self.risk.delay_multiplier(state)
        probe = self.risk.is_probe(state)

        session = await self.browsers.open(account)
        if not await session.has_login():
            account.status = "needs_login"
            with self.db.session() as sess:
                a = sess.get(Account, account.id)
                if a:
                    a.status = "needs_login"
                    sess.commit()
            raise RiskBlocked(f"账号 [{account.name}] 登录态失效, 等待人工重登")

        collector = XhsCollector(
            session, self.behavior,
            self.cfg.risk.risk_http_codes, self.cfg.risk.risk_biz_codes)

        # 探针模式: 只做一次极小量动作验证可用性
        max_notes = 3 if probe else task.max_notes_per_run

        try:
            await self.behavior.inter_request_sleep(multiplier)
            raw_notes = await collector.search(task.keyword,
                                               max_notes=max_notes)
            self.risk.report_success(account)
        except ApiHarvestError as e:
            if e.is_risk:
                self.risk.report_risk_signal(account, e.signal, task.keyword)
            raise RiskBlocked(f"采集失败[{e.signal}]") from e

        # 详情补全(标题来自详情页; 每条都要过风控配额)
        if task.fetch_detail and not probe:
            for i, note in enumerate(raw_notes):
                try:
                    self.risk.check(account, "detail")
                    # 会话内翻看节奏: 轻量间隔(阅读时间在 note_detail 内按内容计算)
                    await self.behavior.pause(4.0, 10.0)
                    detail = await collector.note_detail(note)
                    if detail:
                        raw_notes[i] = detail
                        self.risk.report_success(account)
                except RiskBlocked:
                    log.info("详情配额受限, 剩余笔记用卡片数据")
                    break
                except ApiHarvestError as e:
                    if e.is_risk:
                        self.risk.report_risk_signal(account, e.signal, "detail")
                    break

        # 去重 + 入库 + 识别
        report.tasks_run += 1
        new_notes: list[dict] = []
        with self.db.session() as sess:
            existing_ids = set(sess.scalars(
                select(Note.note_id).where(
                    Note.note_id.in_([n["note_id"] for n in raw_notes] or [""]))
            ).all())
            # 近似去重(两段式): 与库内最近 500 条比 —
            # SimHash 海明距离<=20 廉价预筛 -> Jaccard>=0.7 精确确认
            recent = list(sess.scalars(
                select(Note).order_by(Note.collected_at.desc()).limit(500)
            ).all())

            def is_dup(text: str, sh: int) -> bool:
                for rn in recent:
                    if not rn.simhash:
                        continue
                    if simhash_prefilter(sh, rn.simhash):
                        if is_near_duplicate(
                                text, (rn.title or "") + (rn.desc or "")):
                            return True
                return False

            for n in raw_notes:
                if n["note_id"] in existing_ids:
                    report.notes_duplicate += 1
                    continue
                text = (n.get("title") or "") + (n.get("desc") or "")
                sh = simhash(text)
                if is_dup(text, sh):
                    report.notes_duplicate += 1
                    continue
                note_row = Note(
                    note_id=n["note_id"],
                    xsec_token=n.get("xsec_token", ""),
                    title=n.get("title", ""),
                    desc=n.get("desc", ""),
                    note_type=n.get("note_type", "normal"),
                    user_id=n.get("user_id", ""),
                    user_nickname=n.get("user_nickname", ""),
                    liked_count=n.get("liked_count", ""),
                    comment_count=n.get("comment_count", ""),
                    liked_count_num=n.get("liked_count_num", 0),
                    url=n.get("url", ""),
                    media=n.get("media", []),
                    content_hash=exact_hash(n.get("title", ""),
                                            n.get("desc", "")),
                    simhash=f"{sh:016x}",
                    task_id=task.id,
                    keyword=task.keyword,
                    raw=n.get("raw", {}),
                )
                sess.add(note_row)
                new_notes.append(n)
                report.notes_new += 1
            sess.commit()

        # L1 规则识别 + L2 LLM 增强
        analyses: dict[str, AnalysisResult] = {}
        for n in new_notes:
            analyses[n["note_id"]] = self.classifier.classify(n)
        if new_notes and self.llm.available:
            llm_results = await self.llm.analyze_batch(
                new_notes, topic=task.keyword)
            for nid, llm_r in llm_results.items():
                base = analyses.get(nid)
                if base:
                    # LLM 结果覆盖规则结果(语义级优先), 保留规则命中记录
                    llm_r.matched_rules = base.matched_rules
                    llm_r.is_alert = llm_r.is_alert or base.is_alert
                    analyses[nid] = llm_r

        with self.db.session() as sess:
            for nid, a in analyses.items():
                sess.merge(Analysis(
                    note_id=nid, matched_rules=a.matched_rules,
                    relevance=a.relevance, category=a.category,
                    sentiment=a.sentiment, is_alert=a.is_alert,
                    summary=a.summary, analyzed_by=a.analyzed_by))
            sess.commit()

        # 告警收集: 相关度>=40 或 is_alert
        if new_notes:
            with self.db.session() as sess:
                by_id = {n.note_id: n for n in sess.scalars(
                    select(Note).where(
                        Note.note_id.in_([n["note_id"] for n in new_notes]))
                ).all()}
                for nid, a in analyses.items():
                    if a.is_alert or a.relevance >= 40:
                        if nid in by_id:
                            report.alerts.append((by_id[nid], a))

    # ---------- 选号 ----------

    def _mark_task_failure(self, task_id: int, success: bool | None) -> None:
        """记录任务连续失败次数(供监督层感知). None 表示风控拦截, 不计数."""
        key = f"task_fail_{task_id}"
        if success is True:
            self.db.set_state(key, 0)
        elif success is False:
            cur = int(self.db.get_state(key, 0) or 0)
            self.db.set_state(key, cur + 1)

    def _pick_account(self) -> Account | None:
        """选健康账号: 优先监督层指定的偏好账号, 再按风险等级最低/最久未用."""
        with self.db.session() as sess:
            accounts = list(sess.scalars(
                select(Account).where(Account.status == "active")).all())
            if not accounts:
                return None
            pref = self.db.get_state("preferred_account")

            def sort_key(a):
                s = self.risk.state_of(a)
                return (0 if a.name == pref else 1, s.get("level", 0),
                        a.last_used_at or datetime.min)
            accounts.sort(key=sort_key)
            chosen = accounts[0]
            chosen.last_used_at = datetime.now()
            sess.commit()
            return chosen
