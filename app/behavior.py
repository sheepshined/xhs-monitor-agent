"""拟人行为模拟 — 反风控策略的"行为真实性"维度

第一性原理: 平台风控通过行为遥测区分机器与真人。机器的典型特征是
分布过于规整 — 恒定间隔、匀速滚动、瞬时点击、停留时长与内容长度无关。
本模块把每个行为维度都变成"有界的随机变量", 并让停留时长与内容长度
正相关, 使 Agent 的行为分布覆盖真人分布的正常区间。

设计原则(对抗性):
- 所有延迟取有界随机分布, 永远不产生完全等间隔的请求序列
- 滚动带缓急节奏与偶发回滚(真人会往回翻)
- 阅读时长 ~ f(内容字数), 上限截断
- 打字逐字符延迟, 且模拟偶尔的思考停顿
"""

from __future__ import annotations

import asyncio
import random

from .config import BehaviorConfig


class HumanBehavior:
    def __init__(self, cfg: BehaviorConfig, rng: random.Random | None = None):
        self.cfg = cfg
        self.rng = rng or random.Random()

    # ---------- 基础延迟 ----------

    async def pause(self, lo: float, hi: float) -> None:
        """[lo, hi] 秒的随机停顿."""
        await asyncio.sleep(self.rng.uniform(lo, hi))

    async def inter_request_sleep(self, multiplier: float = 1.0) -> None:
        """相邻两次平台动作之间的基础间隔.

        multiplier 由 RiskEngine 注入: 风险等级越高间隔越长(渐进恢复期降速)。
        """
        c = self.cfg
        lo = c.request_interval_min * multiplier
        hi = c.request_interval_max * multiplier
        await self.pause(lo, hi)

    # ---------- 页面行为 ----------

    async def human_scroll(self, page, rounds: int | None = None) -> None:
        """拟人滚动: 多轮小步滚动, 带随机停顿与偶发回滚."""
        c = self.cfg
        if rounds is None:
            rounds = self.rng.randint(c.scroll_rounds_min, c.scroll_rounds_max)
        for _ in range(rounds):
            dy = self.rng.randint(c.scroll_dy_min, c.scroll_dy_max)
            # 分成 2-4 个小步, 模拟滚轮的连续触发
            steps = self.rng.randint(2, 4)
            for _ in range(steps):
                await page.mouse.wheel(0, dy // steps)
                await asyncio.sleep(self.rng.uniform(0.05, 0.18))
            # 概率性回滚一段(真人回看)
            if self.rng.random() < c.reverse_scroll_prob:
                await asyncio.sleep(self.rng.uniform(0.2, 0.6))
                await page.mouse.wheel(0, -self.rng.randint(120, 300))
            # 轮间停顿(阅读)
            await self.pause(0.8, 2.5)

    async def dwell_for_content(self, text: str) -> None:
        """按内容长度停留: 字数 × 单字阅读耗时, 上下限截断."""
        c = self.cfg
        ms = len(text or "") * c.dwell_ms_per_char
        ms = max(c.dwell_ms_min, min(c.dwell_ms_max, ms))
        await asyncio.sleep(ms / 1000.0)

    async def type_text(self, page, text: str) -> None:
        """逐字符输入, 模拟真人打字节奏(含偶发长停顿)."""
        c = self.cfg
        for ch in text:
            await page.keyboard.type(ch, delay=self.rng.randint(
                c.type_delay_ms_min, c.type_delay_ms_max))
            if self.rng.random() < 0.05:  # 5% 概率思考停顿
                await asyncio.sleep(self.rng.uniform(0.3, 0.9))

    async def mouse_move_jitter(self, page, w: int = 1280, h: int = 800) -> None:
        """操作前把鼠标移到一个自然位置, 避免鼠标长期停在 (0,0)."""
        x = self.rng.randint(int(w * 0.2), int(w * 0.8))
        y = self.rng.randint(int(h * 0.2), int(h * 0.7))
        try:
            await page.mouse.move(x, y, steps=self.rng.randint(8, 20))
        except Exception:
            pass
