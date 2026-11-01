"""浏览器环境管理 — 反风控策略的"环境一致性"维度

第一性原理: 平台通过跨请求的环境指纹(IP / UA / Canvas / Cookie / 行为历史)
给"设备"画像。画像内部自相矛盾 = 最强的机器人信号。因此铁律是:
  一号 · 一画像 · 一出口, 长期稳定。

实现(借鉴 MediaCrawler CDP 思路 + CreatorHub 账号隔离):
- 每个账号一个独立持久化 user_data_dir(Cookie/缓存/localStorage 全隔离)
- 每个账号绑定固定代理出口; 代理失效即停止该账号, 绝不静默回落直连
  (回落会导致出口 IP 突变, 画像自毁)
- 每账号固定 viewport / locale, 升级不漂移
- 优先使用 patchright(Playwright 反检测版); 探测不到自动回退 playwright
- 登录态只认 web_session Cookie; 失效则挂起任务等待人工重登, 不自动重试
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .config import AppConfig
from .db import Account

log = logging.getLogger("browser")

try:  # patchright: 隐藏了 CDP 自动化指纹的 Playwright 分支
    from patchright.async_api import async_playwright
    BROWSER_LIB = "patchright"
except ImportError:  # pragma: no cover - 回退方案
    from playwright.async_api import async_playwright
    BROWSER_LIB = "playwright"

XHS_HOME = "https://www.xiaohongshu.com"


class BrowserSession:
    """一个账号的浏览器会话封装(持久化上下文 + 首页标签)."""

    def __init__(self, account: Account, context, page):
        self.account = account
        self.context = context
        self.page = page

    async def has_login(self) -> bool:
        cookies = await self.context.cookies()
        return any(c["name"] == "web_session" and c.get("value")
                   for c in cookies)

    async def new_page(self):
        return await self.context.new_page()


class BrowserManager:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self._pw = None
        self._sessions: dict[str, BrowserSession] = {}

    async def _ensure_pw(self):
        if self._pw is None:
            self._pw = await async_playwright().start()
        return self._pw

    def _profile_dir(self, account_name: str) -> Path:
        d = Path(self.cfg.profiles_dir) / account_name
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def open(self, account: Account) -> BrowserSession:
        """打开(或复用)账号的持久化浏览器会话."""
        if account.name in self._sessions:
            return self._sessions[account.name]

        pw = await self._ensure_pw()
        profile = self._profile_dir(account.name)

        proxy = None
        if account.proxy:
            proxy = {"server": account.proxy}
            # 支持 http://user:pass@host:port 形式
            if "@" in account.proxy:
                creds, host_part = account.proxy.rsplit("@", 1)
                scheme = host_part.split("://")[0] if "://" in host_part else "http"
                server = host_part.split("://")[-1]
                user, pwd = creds.split("://")[-1].split(":", 1)
                proxy = {"server": f"{scheme}://{server}",
                         "username": user, "password": pwd}

        # 每账号固定 viewport(避免画像漂移); 未配置则用默认
        viewport = None
        for ac in self.cfg.account_configs():
            if ac.name == account.name and ac.viewport:
                viewport = {"width": ac.viewport[0], "height": ac.viewport[1]}

        context = await pw.chromium.launch_persistent_context(
            str(profile),
            headless=False,          # 有头模式: 无头是显著风控特征
            proxy=proxy,
            viewport=viewport or {"width": 1280, "height": 800},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            args=["--disable-blink-features=AutomationControlled"]
            if BROWSER_LIB == "playwright" else [],
        )
        context.set_default_timeout(self.cfg.behavior.page_load_timeout * 1000)

        page = context.pages[0] if context.pages else await context.new_page()
        session = BrowserSession(account, context, page)
        self._sessions[account.name] = session
        log.info("账号 [%s] 浏览器会话已建立 (lib=%s, proxy=%s)",
                 account.name, BROWSER_LIB, "yes" if proxy else "direct")
        return session

    async def interactive_login(self, account: Account,
                                timeout_minutes: int = 5) -> bool:
        """人工扫码登录: 打开首页, 轮询 web_session Cookie.

        登录态只拿一次, 之后长期复用持久化 Profile — 登录动作本身
        是最高风险操作, 绝不自动化(最小暴露面原则)。
        """
        session = await self.open(account)
        await session.page.goto(XHS_HOME, wait_until="domcontentloaded")
        print(f"\n>>> 请在弹出的浏览器中完成账号 [{account.name}] 的扫码登录"
              f" (等待 {timeout_minutes} 分钟)...")
        deadline = asyncio.get_event_loop().time() + timeout_minutes * 60
        while asyncio.get_event_loop().time() < deadline:
            if await session.has_login():
                account.status = "active"
                log.info("账号 [%s] 登录成功", account.name)
                await self.close(account.name)
                return True
            await asyncio.sleep(5)
        print(">>> 超时未登录, 请稍后重试 `main.py login --name "
              f"{account.name}`")
        await self.close(account.name)
        return False

    async def close(self, account_name: str) -> None:
        session = self._sessions.pop(account_name, None)
        if session:
            try:
                await session.context.close()
            except Exception:
                pass

    async def close_all(self) -> None:
        for name in list(self._sessions):
            await self.close(name)
        if self._pw:
            await self._pw.stop()
            self._pw = None
