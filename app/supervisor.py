"""监督决策层 Supervisor — LLM 作为兜底策略大脑

架构升级(第一性原理 + 对抗性):
- 平时不干预: 采集/风控由确定性代码驱动, LLM 完全不碰浏览器行为
- 异常时兜底: 当出现账号被风控/连续失败/风控信号激增, 激活 LLM 诊断,
  并在"受限工具集"内选择策略(换号池/降速/暂停任务/观察模式)
- 安全边界: LLM 只能从预定义工具里选, 且只允许"减速/停用/观察"方向;
  禁止提速、禁止重置风控冷却(那是人工决策)
- 可审计: 每次决策落库(SupervisorLog) + 推飞书, 附中文判断依据
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from .db import Account, Database, RiskEvent, SupervisorLog, Task

log = logging.getLogger("supervisor")

# 受限工具集: LLM 唯一可选择的动作(安全边界)
TOOL_SCHEMA = [
    {"name": "disable_account",
     "desc": "停用指定账号(风控等级过高/状态异常, 停止使用等待人工处理)",
     "args": {"account": "string 账号名"}},
    {"name": "prefer_account",
     "desc": "把后续采集优先切到指定健康账号(更换号池)",
     "args": {"account": "string 账号名"}},
    {"name": "pause_task",
     "desc": "暂停某监控任务(该关键词反复失败或价值低)",
     "args": {"task_id": "int 任务ID"}},
    {"name": "resume_task",
     "desc": "恢复某已暂停任务",
     "args": {"task_id": "int 任务ID"}},
    {"name": "scale_intervals",
     "desc": "所有任务采集周期乘以系数(仅允许 >=1, 即只放缓)",
     "args": {"factor": "float 1.0~4.0"}},
    {"name": "global_pause",
     "desc": "全局暂停采集 N 分钟(观察模式, 让风控冷却)",
     "args": {"minutes": "int 10~1440"}},
    {"name": "noop",
     "desc": "不动作(现状可控, 仅观察记录)",
     "args": {}},
]

SYSTEM_PROMPT = """你是小红书内容监控Agent的"监督决策层(Supervisor)"。
平时你完全不干预任何操作; 只有当采集系统出现异常时才被激活, 负责诊断并给出策略调整。

铁律:
1. 只能从提供的工具列表里选动作, 不得臆造工具或参数。
2. 保守原则: 宁可降速停采, 不可冒险。禁止提高采集频率, 禁止重置风控冷却(那是人工决策)。
3. 每次必须给出中文 rationale(判断依据), 供人工审计。
4. 只输出 JSON: {"rationale":"...","actions":[{"tool":"...","args":{...}}]}, 动作 0~3 个按需。"""


class SupervisorAgent:
    def __init__(self, cfg, db: Database, llm, notifier, risk):
        self.cfg = cfg
        self.db = db
        self.llm = llm
        self.notifier = notifier
        self.risk = risk
        self._tools = {
            "disable_account": self._t_disable_account,
            "prefer_account": self._t_prefer_account,
            "pause_task": self._t_pause_task,
            "resume_task": self._t_resume_task,
            "scale_intervals": self._t_scale_intervals,
            "global_pause": self._t_global_pause,
            "noop": self._t_noop,
        }

    # ---------- 感知: 采集上下文 ----------

    def collect_context(self) -> dict:
        sc = self.cfg.supervisor
        with self.db.session() as s:
            accounts = list(s.scalars(select(Account)).all())
            accts = []
            for a in accounts:
                st = self.risk.state_of(a)
                accts.append({
                    "name": a.name, "status": a.status, "level": st["level"],
                    "cooldown_until": st.get("cooldown_until"),
                    "day_search": st["day_search"], "day_detail": st["day_detail"],
                })
            tasks = list(s.scalars(select(Task)).all())
            task_list = []
            for t in tasks:
                task_list.append({
                    "id": t.id, "name": t.name, "keyword": t.keyword,
                    "enabled": t.enabled, "interval_minutes": t.interval_minutes,
                    "consecutive_failures": int(
                        self.db.get_state(f"task_fail_{t.id}", 0) or 0),
                })
            window = datetime.now() - timedelta(
                minutes=sc.risk_event_window_minutes)
            events = list(s.scalars(
                select(RiskEvent).where(RiskEvent.created_at >= window)
                .order_by(RiskEvent.created_at.desc()).limit(20)).all())
            ev = [{"account": e.account_name, "type": e.event_type,
                   "level": e.level, "detail": e.detail,
                   "time": e.created_at.strftime("%H:%M:%S")} for e in events]
        return {
            "accounts": accts,
            "tasks": task_list,
            "recent_risk_events": ev,
            "available_accounts": [a["name"] for a in accts
                                   if a["status"] == "active"],
        }

    def should_activate(self, ctx: dict) -> bool:
        """确定性激活条件(零成本, 不调 LLM): 满足任一即激活."""
        sc = self.cfg.supervisor
        if any(a["status"] in ("disabled", "cooldown", "needs_login")
               for a in ctx["accounts"]):
            return True
        if len(ctx["recent_risk_events"]) >= sc.risk_event_threshold:
            return True
        if any(t["consecutive_failures"] >= sc.task_failure_threshold
               for t in ctx["tasks"]):
            return True
        return False

    # ---------- 决策主流程 ----------

    async def run_once(self, force: bool = False) -> dict | None:
        sc = self.cfg.supervisor
        if not sc.enabled:
            return None
        if not self.llm.available:
            return None  # 无 LLM 则监督层不激活(退化到确定性风控)

        # 激活节流: 两次 LLM 决策之间最小间隔
        last = self.db.get_state("supervisor_last_run")
        if last and not force:
            try:
                last_dt = datetime.fromisoformat(last)
                if datetime.now() - last_dt < timedelta(
                        minutes=sc.min_activation_interval_minutes):
                    return None
            except ValueError:
                pass

        ctx = self.collect_context()
        if not force and not self.should_activate(ctx):
            return None  # 一切正常, 静默

        plan = await self._decide(ctx)
        if not plan:
            return None

        results = self.execute(plan)
        self._persist(ctx, plan, results)
        await self.notifier.send_supervisor(plan.get("rationale", ""), results)
        self.db.set_state("supervisor_last_run",
                          datetime.now().isoformat(timespec="seconds"))
        log.info("监督层决策: %s -> %s", plan.get("rationale", "")[:60],
                 [r.get("tool") for r in results])
        return {"context": ctx, "plan": plan, "results": results}

    async def _decide(self, ctx: dict) -> dict:
        user = ("当前系统状态(JSON):\n"
                f"{json.dumps(ctx, ensure_ascii=False, indent=2)}\n\n"
                "可用工具:\n"
                f"{json.dumps(TOOL_SCHEMA, ensure_ascii=False, indent=2)}\n\n"
                "请诊断并给出策略调整(严格按 JSON 格式输出)。")
        try:
            plan = await self.llm.chat_json(SYSTEM_PROMPT, user)
        except Exception as e:
            log.warning("监督层 LLM 调用失败: %s", e)
            return {}
        if isinstance(plan, list):
            plan = plan[0] if plan else {}
        if not isinstance(plan, dict):
            return {}
        return plan

    # ---------- 执行: 受限工具 ----------

    def execute(self, plan: dict) -> list:
        results = []
        for act in (plan.get("actions") or [])[:3]:  # 单次最多 3 个动作
            tool = act.get("tool") if isinstance(act, dict) else None
            args = act.get("args") or {} if isinstance(act, dict) else {}
            if tool not in self._tools:
                results.append({"tool": tool, "args": args, "ok": False,
                                "error": f"未知工具 {tool}"})
                continue
            try:
                detail = self._tools[tool](args)
                results.append({"tool": tool, "args": args, "ok": True,
                                "detail": detail})
            except Exception as e:
                results.append({"tool": tool, "args": args, "ok": False,
                                "error": str(e)})
        return results

    def _persist(self, ctx: dict, plan: dict, results: list) -> None:
        try:
            with self.db.session() as s:
                s.add(SupervisorLog(
                    rationale=plan.get("rationale", ""),
                    actions=results, context=ctx))
                s.commit()
        except Exception as e:
            log.debug("SupervisorLog 落库失败: %s", e)

    # ---------- 工具实现 ----------

    def _t_disable_account(self, args: dict) -> str:
        name = args["account"]
        with self.db.session() as s:
            a = s.scalar(select(Account).where(Account.name == name))
            if not a:
                raise ValueError(f"账号 {name} 不存在")
            a.status = "disabled"
            s.add(RiskEvent(account_name=name, event_type="supervisor_disable",
                            level=0, detail="监督层停用账号"))
            s.commit()
        return f"账号 {name} 已停用"

    def _t_prefer_account(self, args: dict) -> str:
        name = args["account"]
        with self.db.session() as s:
            a = s.scalar(select(Account).where(Account.name == name))
            if not a:
                raise ValueError(f"账号 {name} 不存在")
            if a.status != "active":
                raise ValueError(f"账号 {name} 非 active 状态, 不可切换")
        self.db.set_state("preferred_account", name)
        return f"后续采集优先使用账号 {name}"

    def _t_pause_task(self, args: dict) -> str:
        tid = int(args["task_id"])
        with self.db.session() as s:
            t = s.get(Task, tid)
            if not t:
                raise ValueError(f"任务#{tid} 不存在")
            t.enabled = False
            s.commit()
        return f"任务#{tid} 已暂停"

    def _t_resume_task(self, args: dict) -> str:
        tid = int(args["task_id"])
        with self.db.session() as s:
            t = s.get(Task, tid)
            if not t:
                raise ValueError(f"任务#{tid} 不存在")
            t.enabled = True
            s.commit()
        return f"任务#{tid} 已恢复"

    def _t_scale_intervals(self, args: dict) -> str:
        factor = float(args["factor"])
        if not (1.0 <= factor <= 4.0):
            raise ValueError("factor 超范围(仅允许 1.0~4.0)")
        cap = self.cfg.supervisor.max_interval_minutes
        with self.db.session() as s:
            for t in s.scalars(select(Task).where(Task.enabled.is_(True))).all():
                t.interval_minutes = min(int(t.interval_minutes * factor), cap)
            s.commit()
        return f"所有启用任务周期 ×{factor}"

    def _t_global_pause(self, args: dict) -> str:
        minutes = int(args["minutes"])
        if not (10 <= minutes <= 1440):
            raise ValueError("minutes 超范围(仅允许 10~1440)")
        until = (datetime.now() + timedelta(minutes=minutes)).isoformat(
            timespec="seconds")
        self.db.set_state("supervisor_pause_until", until)
        return f"全局暂停采集至 {until}"

    def _t_noop(self, args: dict) -> str:
        return "观察, 无动作"

    # ---------- 暂停状态(供调度器查询) ----------

    def is_paused(self) -> bool:
        until = self.db.get_state("supervisor_pause_until")
        if not until:
            return False
        try:
            return datetime.now() < datetime.fromisoformat(until)
        except ValueError:
            return False
