"""Web 控制台 API — 状态总览 / 任务管理 / 笔记检索 / 风控审计"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from .db import (Account, Analysis, Database, Note, RiskEvent,
                 SupervisorLog, Task)

log = logging.getLogger("web")

WEBUI_DIR = Path(__file__).resolve().parent.parent / "webui"


class TaskIn(BaseModel):
    """注意: 必须定义在模块级 — FastAPI 通过 get_type_hints 解析注解,
    函数内局部类在 __future__.annotations 下无法解析, 会被当成 query 参数."""
    name: str
    keyword: str
    task_type: str = "keyword"
    interval_minutes: int = Field(default=120, ge=10)
    max_notes_per_run: int = Field(default=20, ge=1, le=50)
    fetch_detail: bool = False


def create_app(cfg, db: Database, risk_engine, pipeline,
               supervisor=None) -> FastAPI:
    app = FastAPI(title="XHS Monitor Agent", version="1.0.0")

    # ---------- 页面 ----------

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(WEBUI_DIR / "index.html")

    # ---------- 状态 ----------

    @app.get("/api/status")
    async def status():
        with db.session() as sess:
            accounts = list(sess.scalars(select(Account)).all())
            task_total = sess.scalar(
                select(func.count()).select_from(Task)) or 0
            task_enabled = sess.scalar(
                select(func.count()).select_from(Task).where(
                    Task.enabled.is_(True))) or 0
            note_total = sess.scalar(
                select(func.count()).select_from(Note)) or 0
            alert_total = sess.scalar(
                select(func.count()).select_from(Analysis).where(
                    Analysis.is_alert.is_(True))) or 0
            return {
                "time": datetime.now().isoformat(timespec="seconds"),
                "accounts": [risk_engine.snapshot(a) for a in accounts],
                "tasks": {"total": task_total, "enabled": task_enabled},
                "notes": {"total": note_total, "alerts": alert_total},
            }

    # ---------- 任务 ----------

    @app.get("/api/tasks")
    async def list_tasks():
        with db.session() as sess:
            tasks = list(sess.scalars(select(Task)).all())
            return [{
                "id": t.id, "name": t.name, "keyword": t.keyword,
                "task_type": t.task_type, "enabled": t.enabled,
                "interval_minutes": t.interval_minutes,
                "max_notes_per_run": t.max_notes_per_run,
                "fetch_detail": t.fetch_detail,
                "last_run_at": t.last_run_at.isoformat() if t.last_run_at else None,
                "next_run_at": t.next_run_at.isoformat() if t.next_run_at else None,
            } for t in tasks]

    @app.post("/api/tasks")
    async def create_task(body: TaskIn):
        with db.session() as sess:
            t = Task(name=body.name, keyword=body.keyword,
                     task_type=body.task_type,
                     interval_minutes=body.interval_minutes,
                     max_notes_per_run=body.max_notes_per_run,
                     fetch_detail=body.fetch_detail,
                     next_run_at=datetime.now())
            sess.add(t)
            sess.commit()
            return {"id": t.id}

    @app.post("/api/tasks/{task_id}/toggle")
    async def toggle_task(task_id: int):
        with db.session() as sess:
            t = sess.get(Task, task_id)
            if not t:
                raise HTTPException(404, "task not found")
            t.enabled = not t.enabled
            sess.commit()
            return {"id": t.id, "enabled": t.enabled}

    @app.delete("/api/tasks/{task_id}")
    async def delete_task(task_id: int):
        with db.session() as sess:
            t = sess.get(Task, task_id)
            if not t:
                raise HTTPException(404, "task not found")
            sess.delete(t)
            sess.commit()
            return {"ok": True}

    # ---------- 笔记 ----------

    @app.get("/api/notes")
    async def list_notes(category: str = "", sentiment: str = "",
                         min_relevance: float = 0.0, limit: int = 50):
        stmt = (select(Note, Analysis)
                .outerjoin(Analysis, Analysis.note_id == Note.note_id)
                .order_by(Note.collected_at.desc()).limit(min(limit, 200)))
        rows: list[dict] = []
        with db.session() as sess:
            for note, a in sess.execute(stmt).all():
                if a and category and a.category != category:
                    continue
                if a and sentiment and a.sentiment != sentiment:
                    continue
                if a and a.relevance < min_relevance:
                    continue
                rows.append({
                    "note_id": note.note_id, "title": note.title,
                    "desc": (note.desc or "")[:200],
                    "user": note.user_nickname,
                    "liked": note.liked_count,
                    "url": note.url,
                    "keyword": note.keyword,
                    "collected_at": note.collected_at.isoformat(
                        timespec="seconds"),
                    "analysis": None if not a else {
                        "relevance": a.relevance, "category": a.category,
                        "sentiment": a.sentiment, "is_alert": a.is_alert,
                        "matched_rules": a.matched_rules,
                        "summary": a.summary, "analyzed_by": a.analyzed_by,
                    },
                })
        return rows

    # ---------- 风控 ----------

    @app.get("/api/risk/events")
    async def risk_events(limit: int = 100):
        with db.session() as sess:
            events = list(sess.scalars(
                select(RiskEvent).order_by(
                    RiskEvent.created_at.desc()).limit(min(limit, 500))).all())
            return [{
                "account": e.account_name, "type": e.event_type,
                "level": e.level, "detail": e.detail,
                "created_at": e.created_at.isoformat(timespec="seconds"),
            } for e in events]

    @app.post("/api/accounts/{name}/reset")
    async def reset_account(name: str):
        """人工介入后重置账号风控(等级清零, 恢复可用)."""
        with db.session() as sess:
            a = sess.scalar(select(Account).where(Account.name == name))
            if not a:
                raise HTTPException(404, "account not found")
            a.status = "active"
            a.risk_state = {"level": 0, "cooldown_until": None,
                            "consec_risk": 0, "consec_ok": 0,
                            "probe_pending": False}
            sess.add(RiskEvent(account_name=name, event_type="manual_reset",
                               level=0, detail="人工重置风控状态"))
            sess.commit()
        return {"ok": True}

    # ---------- 监督层 ----------

    @app.get("/api/supervisor/logs")
    async def supervisor_logs(limit: int = 20):
        with db.session() as sess:
            logs = list(sess.scalars(
                select(SupervisorLog).order_by(
                    SupervisorLog.created_at.desc()).limit(min(limit, 100))).all())
            return [{
                "rationale": l.rationale, "actions": l.actions,
                "created_at": l.created_at.isoformat(timespec="seconds"),
            } for l in logs]

    @app.post("/api/supervisor/run")
    async def supervisor_run():
        """手动触发监督层做一次诊断(force=True, 绕过激活条件与节流)."""
        if supervisor is None:
            raise HTTPException(400, "supervisor 未启用")
        result = await supervisor.run_once(force=True)
        if result is None:
            return {"ok": True, "activated": False}
        return {"ok": True, "activated": True,
                "rationale": result["plan"].get("rationale", ""),
                "results": result["results"]}

    # ---------- 手动触发 ----------

    @app.post("/api/run/{task_id}")
    async def run_task(task_id: int):
        import asyncio
        try:
            report = await pipeline.run_cycle(task_ids=[task_id])
            return JSONResponse({
                "ok": report.ok,
                "summary": report.summary(),
                "errors": report.errors,
                "risk_blocked": report.risk_blocked,
            })
        except Exception as e:
            raise HTTPException(500, str(e))

    return app
