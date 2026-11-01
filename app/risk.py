"""风控引擎 — 反风控策略的"节奏经济学"维度 (本项目核心)

第一性原理拆解: 平台封禁的本质是一个代价函数 —
    封禁成本(误伤真人) vs 放行成本(数据被采集)。
Agent 要做的不是"骗过风控"(军备竞赛必败), 而是让自己落在
风控"不值得动手"的区间:
  1. 体积足够小: 配额经济学 — 单账号读写量压到真人日常使用量以内
  2. 行为像真人: 见 behavior.py
  3. 环境一致性: 见 browser.py (一号一画像一出口)
  4. 信号响应: 把平台的风控反馈(403/429/461/471/验证码)当作通信协议,
     立即退避而不是硬闯 — 这是"对抗性原则"里的关键克制

冷却阶梯 + 渐进恢复(借鉴 CreatorHub 风控中心设计):
  风险信号 -> 等级+1 -> 指数冷却(5min→30min→2h→12h→24h)
  冷却结束 -> 探针模式(只允许1次轻量动作) -> 连续成功 N 次才降一级
  恢复期请求间隔 ×(1 + 等级×0.5), 配额 ÷(等级+1)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from .config import RiskConfig
from .db import Account, RiskEvent

log = logging.getLogger("risk")


class RiskBlocked(Exception):
    """请求被风控引擎拦截(冷却中/配额用尽/账号被禁)."""

    def __init__(self, reason: str, until: datetime | None = None):
        self.reason = reason
        self.until = until
        super().__init__(reason)


def _default_state() -> dict:
    return {
        "level": 0,                    # 风险等级 0..5
        "cooldown_until": None,        # ISO 时间串
        "consec_risk": 0,              # 连续风险信号
        "consec_ok": 0,                # 连续成功
        "probe_pending": False,        # 冷却结束后待探针
        "hour_window": "",             # "YYYY-MM-DDTHH"
        "hour_search": 0,
        "hour_detail": 0,
        "day_window": "",              # "YYYY-MM-DD"
        "day_search": 0,
        "day_detail": 0,
        "last_action_at": None,        # ISO 时间串
    }


class RiskEngine:
    def __init__(self, cfg: RiskConfig, db):
        self.cfg = cfg
        self.db = db

    # ---------- 状态读写 ----------

    def state_of(self, account: Account) -> dict:
        s = dict(account.risk_state or {})
        defaults = _default_state()
        defaults.update({k: v for k, v in s.items() if v is not None})
        return defaults

    def _save(self, account: Account, state: dict) -> None:
        """写内存并持久化 — 配额/冷却状态必须在进程重启后仍有效."""
        account.risk_state = dict(state)  # 整体替换以触发 SQLAlchemy 变更检测
        try:
            with self.db.session() as sess:
                sess.merge(account)
                sess.commit()
        except Exception as e:  # 持久化失败不阻断风控判定(内存态仍正确)
            log.debug("risk_state 持久化失败: %s", e)

    # ---------- 检查与配额 ----------

    def check(self, account: Account, action: str, weight: int = 1) -> dict:
        """动作前的风控检查. 通过则扣减配额并返回状态; 否则抛 RiskBlocked.

        action: "search" | "detail"
        """
        now = datetime.now()
        s = self.state_of(account)

        if account.status == "disabled":
            raise RiskBlocked("账号已被禁用, 需人工介入")

        # 1) 冷却检查
        until = s.get("cooldown_until")
        if until:
            until_dt = datetime.fromisoformat(until)
            if now < until_dt:
                raise RiskBlocked(f"冷却中, 至 {until_dt:%m-%d %H:%M}", until_dt)
            # 冷却结束 -> 清除过期时间并进入探针模式, 避免后续检查反复进入探针
            s["cooldown_until"] = None
            if s["level"] > 0:
                s["probe_pending"] = True
                self._save(account, s)
                self._log(account, "cooldown_end", s["level"],
                          "冷却结束, 进入探针模式")

        # 2) 配额窗口滚动
        self._rollover(s, now)

        h_key = f"hour_{action}"
        d_key = f"day_{action}"
        h_cap = self.cfg.hourly_search_quota if action == "search" \
            else self.cfg.hourly_detail_quota
        d_cap = self.cfg.daily_search_quota if action == "search" \
            else self.cfg.daily_detail_quota
        # 恢复期配额折减
        if s["level"] > 0:
            h_cap = max(1, h_cap // (s["level"] + 1))
            d_cap = max(1, d_cap // (s["level"] + 1))

        if s[h_key] + weight > h_cap or s[d_key] + weight > d_cap:
            self._log(account, "quota_exceeded", s["level"],
                      f"{action} 配额用尽 (hour={s[h_key]}/{h_cap}, "
                      f"day={s[d_key]}/{d_cap})")
            nxt = self._next_window(now, s)
            raise RiskBlocked(f"{action} 配额用尽", nxt)

        # 3) 最小间隔(防高频) — 搜索/详情采用不同阈值(真人会话内翻看详情只需几秒)
        last = s.get("last_action_at")
        if last:
            gap = (now - datetime.fromisoformat(last)).total_seconds()
            base_gap = (self.cfg.min_request_gap_seconds if action == "search"
                        else self.cfg.min_detail_gap_seconds)
            min_gap = base_gap * self.delay_multiplier(s)
            if gap < min_gap:
                raise RiskBlocked(
                    f"距上次动作仅 {gap:.0f}s < 最小间隔 {min_gap:.0f}s")

        # 4) 扣减配额
        s[h_key] += weight
        s[d_key] += weight
        s["last_action_at"] = now.isoformat(timespec="seconds")
        self._save(account, s)
        return s

    def _next_window(self, now: datetime, s: dict) -> datetime:
        if s["hour_window"]:
            cur = datetime.fromisoformat(s["hour_window"])
            return cur + timedelta(hours=1)
        return now + timedelta(hours=1)

    @staticmethod
    def _rollover(s: dict, now: datetime) -> None:
        hw = now.strftime("%Y-%m-%dT%H")
        dw = now.strftime("%Y-%m-%d")
        if s["hour_window"] != hw:
            s["hour_window"] = hw
            s["hour_search"] = 0
            s["hour_detail"] = 0
        if s["day_window"] != dw:
            s["day_window"] = dw
            s["day_search"] = 0
            s["day_detail"] = 0

    @staticmethod
    def delay_multiplier(s: dict) -> float:
        """恢复期请求间隔放大系数."""
        return 1.0 + s["level"] * 0.5

    def is_probe(self, s: dict) -> bool:
        return bool(s.get("probe_pending"))

    # ---------- 结果上报 ----------

    def report_success(self, account: Account) -> None:
        s = self.state_of(account)
        s["consec_risk"] = 0
        s["consec_ok"] += 1

        if s["probe_pending"]:
            # 探针成功: 退出探针, 开始降级恢复
            s["probe_pending"] = False
            self._log(account, "probe_ok", s["level"], "探针成功, 开始渐进恢复")

        if s["level"] > 0 and s["consec_ok"] >= self.cfg.recover_success_needed:
            s["level"] -= 1
            s["consec_ok"] = 0
            if s["level"] == 0:
                s["cooldown_until"] = None
            self._log(account, "recover", s["level"],
                      f"连续成功 {self.cfg.recover_success_needed} 次, 风险等级降至 {s['level']}")
        if account.status == "cooldown":
            account.status = "active"
        # 状态与风控进度一起持久化, 避免重启后账号仍停留在 cooldown。
        self._save(account, s)

    def report_risk_signal(self, account: Account, signal: str,
                           detail: str = "") -> None:
        """上报一个风控信号(HTTP 403/429/461/471, 验证码页, 业务码)."""
        now = datetime.now()
        s = self.state_of(account)
        if s["probe_pending"]:
            # 探针失败: 重新进入冷却, 等级不降反升
            s["probe_pending"] = False
            self._log(account, "probe_fail", s["level"],
                      f"探针失败({signal}), 重新冷却")

        s["consec_ok"] = 0
        s["consec_risk"] += 1
        s["level"] = min(s["level"] + 1,
                         len(self.cfg.cooldown_ladder_minutes) - 1)
        minutes = self.cfg.cooldown_ladder_minutes[s["level"]]
        if minutes > 0:
            until = now + timedelta(minutes=minutes)
            s["cooldown_until"] = until.isoformat(timespec="seconds")
            account.status = "cooldown"
            self._log(account, "cooldown_start", s["level"],
                      f"信号={signal} {detail}; 冷却至 {until:%m-%d %H:%M} "
                      f"({minutes}min)")
        else:
            self._log(account, "risk_signal", s["level"],
                      f"信号={signal} {detail}")

        if s["level"] >= len(self.cfg.cooldown_ladder_minutes) - 1:
            account.status = "disabled"
            self._log(account, "account_disabled", s["level"],
                      "风险等级封顶, 账号禁用, 等待人工介入")
        self._save(account, s)

    # ---------- 审计 ----------

    def _log(self, account: Account, event_type: str, level: int,
             detail: str) -> None:
        with self.db.session() as sess:
            sess.merge(account)
            sess.add(RiskEvent(account_name=account.name,
                               event_type=event_type, level=level,
                               detail=detail))
            sess.commit()
        log.warning("[%s] %s: %s", account.name, event_type, detail)

    def snapshot(self, account: Account) -> dict:
        """给控制台/接口用的风控快照."""
        s = self.state_of(account)
        until = s.get("cooldown_until")
        return {
            "account": account.name,
            "status": account.status,
            "level": s["level"],
            "cooldown_until": until,
            "probe_pending": s["probe_pending"],
            "hour_search": s["hour_search"],
            "hour_detail": s["hour_detail"],
            "day_search": s["day_search"],
            "day_detail": s["day_detail"],
            "delay_multiplier": self.delay_multiplier(s),
        }
