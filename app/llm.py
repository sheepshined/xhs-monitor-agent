"""LLM 深度分析层(可选) — OpenAI 兼容接口

借鉴 TrendRadar 的 LiteLLM 思路: 只依赖 OpenAI 兼容 chat/completions
协议(DeepSeek / Qwen / GLM / Kimi 均兼容), 不绑定特定 SDK。
仅对规则层判定为疑似相关的笔记做批量语义分析, 控制成本;
LLM 不可用时自动降级回规则层结果(可用性优先, 借鉴 TrendRadar
"AI 筛选失败自动回退关键词" 的设计)。
"""

from __future__ import annotations

import json
import logging
import os

import httpx

from .classifier import AnalysisResult
from .config import LLMConfig

log = logging.getLogger("llm")

_SYSTEM_PROMPT = """你是小红书内容监控Agent的内容分析模块。对输入的笔记做结构化分析, 输出严格的 JSON 数组, 每个元素:
{"note_id": "输入原样返回", "relevance": 0-100整数(与监控主题的相关度), "category": "brand_mention|complaint|competitor|industry_trend|other", "sentiment": "positive|neutral|negative", "summary": "30字内中文摘要", "is_alert": true|false}
判定标准: is_alert 为 true 当且仅当(涉及品牌负面舆情 或 竞品重要动态 或 高相关度>=70)。
只输出 JSON 数组, 不要任何其他文字。"""


class LLMAnalyzer:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self.api_key = os.environ.get(cfg.api_key_env, "")

    @property
    def available(self) -> bool:
        return bool(self.cfg.enabled and self.api_key)

    async def analyze_batch(self, notes: list[dict],
                            topic: str = "") -> dict[str, AnalysisResult]:
        """批量分析, 返回 {note_id: AnalysisResult}. 失败返回空 dict(降级)."""
        if not self.available or not notes:
            return {}
        results: dict[str, AnalysisResult] = {}
        # 分批, 防超长
        for i in range(0, len(notes), self.cfg.batch_size):
            batch = notes[i:i + self.cfg.batch_size]
            try:
                parsed = await self._call(batch, topic)
            except Exception as e:
                log.warning("LLM 批次分析失败, 本批降级规则结果: %s", e)
                return results
            for item in parsed:
                nid = str(item.get("note_id", ""))
                if nid:
                    results[nid] = AnalysisResult(
                        relevance=float(item.get("relevance", 0)),
                        category=str(item.get("category", "other")),
                        sentiment=str(item.get("sentiment", "neutral")),
                        is_alert=bool(item.get("is_alert", False)),
                        summary=str(item.get("summary", "")),
                        analyzed_by="llm",
                    )
        return results

    async def chat_json(self, system: str, user: str) -> dict:
        """通用 JSON 对话(供监督层使用), 返回解析后的 dict; 失败抛异常."""
        if not self.available:
            return {}
        async with httpx.AsyncClient(timeout=self.cfg.timeout_seconds) as client:
            resp = await client.post(
                f"{self.cfg.base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.cfg.model,
                    "temperature": 0.2,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                },
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            content = content.strip()
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            return json.loads(content)

    async def _call(self, batch: list[dict], topic: str) -> list[dict]:
        lines = []
        for n in batch:
            text = (n.get("title", "") + " " + n.get("desc", ""))[:500]
            lines.append(f'note_id={n["note_id"]}; 作者={n.get("user_nickname", "")};'
                         f' 文本={text}')
        user_prompt = (f"监控主题: {topic or '劳动防护/呼吸防护用品'}\n"
                       f"笔记列表:\n" + "\n".join(lines))

        async with httpx.AsyncClient(timeout=self.cfg.timeout_seconds) as client:
            resp = await client.post(
                f"{self.cfg.base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.cfg.model,
                    "temperature": self.cfg.temperature,
                    "messages": [
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                },
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            # 容错: 剥掉可能的 ```json 围栏
            content = content.strip()
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            return json.loads(content)
