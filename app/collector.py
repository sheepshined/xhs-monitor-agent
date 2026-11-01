"""小红书采集器 — 页面拦截式采集 + DOM 提取 (基于真实环境验证)

核心技术决策(第一性原理): 小红书 Web 端每个 XHR 都有 x-s/x-t 签名, 逆向
签名是一场无止境的军备竞赛。而"让真实页面自己发请求, 我们在网络层收割
JSON"把对抗面从"伪造请求"缩小到"像真人一样浏览"。=> 不碰签名, 不碰协议。

2026-09 真实环境验证后的关键适配:
- 搜索接口已从 /api/sns/web/v1/search/notes 升级为
  so.xiaohongshu.com/api/sns/web/**v2**/search/notes
- 搜索列表 note_card 不再返回标题(display_title=None), 标题被烧进封面图;
  标题/正文必须从笔记详情页 DOM 的 #detail-desc 提取
- 响应监听器必须在 page.goto 之前挂载, 否则漏掉首个响应
- 封面 URL 字段是 cover.url_default(而非 url); 互动数现在多为字符串
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote

from .behavior import HumanBehavior
from .browser import BrowserSession, XHS_HOME

log = logging.getLogger("collector")

SEARCH_API = "/api/sns/web/v2/search/notes"
DETAIL_DESC_SELECTORS = ["#detail-desc", ".desc", ".note-text", "#detail-title"]
CAPTCHA_HINTS = ("captcha", "user-proof", "verify")


class ApiHarvestError(Exception):
    """采集失败. is_risk=True 表示是风控信号(应触发冷却), 否则为中性错误."""

    def __init__(self, signal: str, is_risk: bool = True):
        self.signal = signal
        self.is_risk = is_risk
        super().__init__(signal)


def parse_count(v) -> int:
    """'1.2万' -> 12000; '8561' -> 8561; 671 -> 671."""
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip()
    if not s:
        return 0
    try:
        if "万" in s:
            return int(float(s.replace("万", "")) * 10000)
        if "亿" in s:
            return int(float(s.replace("亿", "")) * 100000000)
        return int(float(s))
    except ValueError:
        return 0


class XhsCollector:
    def __init__(self, session: BrowserSession, behavior: HumanBehavior,
                 risk_codes_http: list, risk_codes_biz: list):
        self.session = session
        self.b = behavior
        self.risk_http = set(risk_codes_http)
        self.risk_biz = set(risk_codes_biz)

    # ---------- 底层: 响应流 ----------

    def _attach_stream(self, page, api_fragment: str) -> asyncio.Queue:
        """在导航前挂载监听, 命中目标 API 的响应入队(风控信号单独标记)."""
        q: asyncio.Queue = asyncio.Queue()

        async def on_response(resp):
            if api_fragment not in resp.url:
                return
            if resp.status in self.risk_http:
                await q.put({"risk": f"http_{resp.status}"})
                return
            try:
                payload = await resp.json()
            except Exception:
                return  # 非 JSON
            code = payload.get("code", 0)
            if code in self.risk_biz or (payload.get("success") is False
                                          and code != 0):
                await q.put({"risk": f"biz_{code}"})
                return
            await q.put({"data": payload.get("data", {})})

        page.on("response", lambda r: asyncio.ensure_future(on_response(r)))
        return q

    async def _next(self, q: asyncio.Queue, timeout: float) -> dict:
        try:
            item = await asyncio.wait_for(q.get(), timeout)
        except asyncio.TimeoutError:
            return {"ok": False, "signal": "timeout", "is_risk": False}
        if "risk" in item:
            return {"ok": False, "signal": item["risk"], "is_risk": True}
        return {"ok": True, "data": item["data"]}

    async def _check_captcha(self, page) -> str | None:
        url = page.url or ""
        if any(h in url for h in CAPTCHA_HINTS):
            return "captcha_page"
        return None

    # ---------- 搜索 ----------

    async def _goto_retry(self, page, url: str, retries: int = 2) -> None:
        """导航 + 偶发连接重置重试(真人加载失败也会刷新)."""
        last = None
        for i in range(retries):
            try:
                await page.goto(url, wait_until="domcontentloaded")
                return
            except Exception as e:
                last = e
                if i < retries - 1:
                    await asyncio.sleep(6.0 + i * 4.0)
        raise last

    async def search(self, keyword: str, max_notes: int = 20) -> list[dict]:
        """关键词搜索: URL 直达搜索结果页(真实用户分享链接也走此路径),
        滚动加载并收割 v2 search/notes 响应."""
        page = self.session.page
        # 先访问首页暖机: 建立会话/Cookie, 避免冷启动直接进搜索页触发慢加载或校验
        await self._goto_retry(page, XHS_HOME)
        await self.b.pause(1.0, 2.5)
        q = self._attach_stream(page, SEARCH_API)  # 先挂监听, 再导航

        url = (f"{XHS_HOME}/search_result?keyword={quote(keyword)}"
               f"&source=web_search_result_notes")
        await self._goto_retry(page, url)
        await self.b.pause(1.5, 3.5)

        if await self._check_captcha(page):
            raise ApiHarvestError("captcha_page", is_risk=True)

        notes: dict[str, dict] = {}
        scroll_round = 0
        max_rounds = 12
        while len(notes) < max_notes and scroll_round < max_rounds:
            result = await self._next(q, timeout=15.0)
            if not result["ok"]:
                raise ApiHarvestError(result["signal"],
                                      is_risk=result["is_risk"])
            items = self._parse_search_items(result["data"])
            before = len(notes)
            for it in items:
                if it["note_id"] and it["note_id"] not in notes:
                    notes[it["note_id"]] = it
            got_new = len(notes) - before > 0

            if len(notes) >= max_notes:
                break
            if not got_new and scroll_round >= 2:
                break  # 到底了
            await self.b.human_scroll(page, rounds=1)
            await self.b.pause(1.0, 2.5)
            scroll_round += 1

        result = list(notes.values())[:max_notes]
        log.info("搜索 [%s] 收割 %d 条笔记卡片 (%d 轮滚动)",
                 keyword, len(result), scroll_round)
        return result

    # ---------- 详情 ----------

    async def note_detail(self, note: dict) -> dict | None:
        """打开笔记详情页, 从 DOM 提取标题+正文(搜索列表已不返回标题)."""
        page = await self.session.new_page()
        try:
            url = f"{XHS_HOME}/explore/{note['note_id']}"
            if note.get("xsec_token"):
                url += (f"?xsec_token={quote(note['xsec_token'])}"
                        f"&xsec_source=pc_search")
            await self.b.mouse_move_jitter(page)
            await self._goto_retry(page, url)
            await asyncio.sleep(4.0)  # 等待 SPA 渲染

            if await self._check_captcha(page):
                raise ApiHarvestError("captcha_page", is_risk=True)

            content = await self._extract_content(page)
            # 阅读停留(时长与内容字数正相关) — 真人读完才关
            await self.b.dwell_for_content(content)
            merged = dict(note)
            merged["desc"] = content
            # 标题用首行启发式(详情页 #detail-desc 通常含标题+正文)
            lines = [ln.strip() for ln in content.split("\n") if ln.strip()]
            if lines and not note.get("title"):
                merged["title"] = lines[0]
            return merged
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def _extract_content(self, page) -> str:
        for sel in DETAIL_DESC_SELECTORS:
            try:
                loc = page.locator(sel)
                if await loc.count() > 0:
                    txt = (await loc.first.inner_text()).strip()
                    if txt:
                        return txt
            except Exception:
                continue
        return ""

    # ---------- 解析(防御式) ----------

    @staticmethod
    def _parse_search_items(data: dict) -> list[dict]:
        items = (data.get("items") if isinstance(data, dict) else None) or []
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            card = it.get("note_card") or {}
            note_id = it.get("id") or card.get("note_id") or ""
            if not note_id:
                continue
            user = card.get("user") or {}
            inter = card.get("interact_info") or {}
            cover = card.get("cover") or {}
            # 封面: v2 用 url_default / url_pre
            cover_url = (cover.get("url_default") or cover.get("url_pre")
                         or cover.get("url") or "")
            out.append({
                "note_id": str(note_id),
                "xsec_token": it.get("xsec_token", ""),
                "title": card.get("display_title") or "",  # v2 列表通常为空
                "desc": "",
                "note_type": card.get("type", "normal"),
                "user_id": str(user.get("user_id", "")),
                "user_nickname": user.get("nickname", ""),
                "liked_count": str(inter.get("liked_count", "") or ""),
                "comment_count": str(inter.get("comment_count", "") or ""),
                "liked_count_num": parse_count(inter.get("liked_count")),
                "url": f"{XHS_HOME}/explore/{note_id}",
                "media": [{"url": cover_url,
                           "width": cover.get("width", 0),
                           "height": cover.get("height", 0)}],
                "raw": {"model_type": it.get("model_type", "")},
            })
        return out
