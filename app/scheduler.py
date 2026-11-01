"""调度器 — 反周期性设计

对抗性原则: 完美等间隔的请求序列是最强的机器指纹(真人不会每 120.0 分钟
准时刷新)。因此:
- 任务启动加随机抖动(0 ~ start_jitter_max_seconds)
- 下一轮间隔 = interval × uniform(1-δ, 1+δ) — 永不周期
- 活跃时段约束: 深夜不采集(贴合真人作息, 也降低夜间风控敏感期暴露)
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta

from sqlalchemy import select

from .db import Database, Task

log = logging.getLogger("scheduler")


class Scheduler:
    def __init__(self, cfg, db: Database, pipeline, supervisor=None):
        self.cfg = cfg
        self.db = db
        self.pipeline = pipeline
        self.supervisor = supervisor
        self.rng = random.Random()
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def _in_active_hours(self, now: datetime) -> bool:
        sc = self.cfg.scheduler
        h = now.hour
        if sc.active_hours_start <= sc.active_hours_end:
            return sc.active_hours_start <= h < sc.active_hours_end
        return h >= sc.active_hours_start or h < sc.active_hours_end  # 跨午夜

    async def run_forever(self) -> None:
        sc = self.cfg.scheduler
        log.info("调度器启动 (tick=%ds, 活跃时段 %02d:00-%02d:00, 启动抖动 0-%ds)",
                 sc.tick_seconds, sc.active_hours_start, sc.active_hours_end,
                 sc.start_jitter_max_seconds)
        while not self._stop:
            try:
                await self._tick()
                # 监督层: 平时静默, 异常时 LLM 兜底决策(内部有节流)
                if self.supervisor:
                    await self.supervisor.run_once()
            except Exception:
                log.exception("调度 tick 异常")
            await asyncio.sleep(sc.tick_seconds)

    async def _tick(self) -> None:
        now = datetime.now()
        if not self._in_active_hours(now):
            return
        # 监督层已进入观察模式(全局暂停): 跳过采集
        if self.supervisor and self.supervisor.is_paused():
            return

        with self.db.session() as sess:
            due = list(sess.scalars(select(Task).where(
                Task.enabled.is_(True), Task.next_run_at <= now)).all())
        if not due:
            return

        # 启动抖动: 打散任务的整齐出发时间, 避免周期性指纹
        jitter = self.rng.uniform(0, self.cfg.scheduler.start_jitter_max_seconds)
        log.info("%d 个任务到期, 抖动 %.0fs 后执行", len(due), jitter)
        await asyncio.sleep(jitter)

        await self.pipeline.run_cycle(task_ids=[t.id for t in due])

        # 重算下一轮: interval × 随机浮动(反周期)
        sc = self.cfg.scheduler
        with self.db.session() as sess:
            for t in due:
                task = sess.get(Task, t.id)
                if task and task.enabled:
                    factor = 1.0 + self.rng.uniform(
                        -sc.interval_jitter_ratio, sc.interval_jitter_ratio)
                    task.next_run_at = datetime.now() + timedelta(
                        minutes=task.interval_minutes * factor)
            sess.commit()
