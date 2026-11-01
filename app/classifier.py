"""规则引擎内容识别 — 关键词/正则/情感/相关度

分层识别架构(第一性原理: 识别是"信号提取", 廉价信号先算, 昂贵信号按需算):
  L1 规则引擎(本地, 0成本): 关键词命中/正则/情感词典/相关度打分
  L2 LLM 深度分析(可选, 按需): 仅对 L1 判定为"疑似相关"的笔记做
     语义级分类与摘要 — 控制推理成本
规则配置在 keywords.yaml, 运营人员可直接维护, 无需改代码。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Rule:
    name: str
    include: list = field(default_factory=list)
    exclude: list = field(default_factory=list)
    regex: list = field(default_factory=list)
    category: str = "other"
    sentiment: str = ""          # 强制情感标签(如 complaint 规则固定 negative)
    alert: bool = False          # 命中即告警
    weight: float = 10.0         # 每命中一个关键词贡献的相关度


# 内置情感词典(轻量; LLM 开启时由 LLM 接管细粒度情感)
NEGATIVE_WORDS = {
    "过敏", "假货", "投诉", "维权", "举报", "骗", "垃圾", "难用", "踩雷",
    "翻车", "退货", "退款", "勒耳朵", "闷", "异味", "起雾", "失效", "智商税",
    "别买", "避雷", "差评",
}
POSITIVE_WORDS = {
    "好用", "回购", "推荐", "真香", "舒服", "透气", "性价比", "良心",
    "种草", "安利", "专业", "大牌", "放心",
}


@dataclass
class AnalysisResult:
    matched_rules: list = field(default_factory=list)
    relevance: float = 0.0
    category: str = "other"
    sentiment: str = "neutral"
    is_alert: bool = False
    analyzed_by: str = "rules"
    summary: str = ""


class RuleClassifier:
    def __init__(self, rules_cfg: list[dict] | None = None,
                 base_keywords: list | None = None):
        self.rules: list[Rule] = []
        self.base_keywords: list[str] = [k for k in (base_keywords or []) if k]
        for rc in rules_cfg or []:
            self.rules.append(Rule(
                name=rc.get("name", "unnamed"),
                include=rc.get("include", []) or [],
                exclude=rc.get("exclude", []) or [],
                regex=[r for r in (rc.get("regex", []) or []) if r],
                category=rc.get("category", "other"),
                sentiment=rc.get("sentiment", ""),
                alert=bool(rc.get("alert", False)),
                weight=float(rc.get("weight", 10.0)),
            ))

    # ---------- 单条识别 ----------

    def classify(self, note: dict) -> AnalysisResult:
        text = " ".join([
            note.get("title", "") or "",
            note.get("desc", "") or "",
        ])
        result = AnalysisResult()

        # 情感: 词典粗判
        neg_hits = [w for w in NEGATIVE_WORDS if w in text]
        pos_hits = [w for w in POSITIVE_WORDS if w in text]
        if neg_hits and not pos_hits:
            result.sentiment = "negative"
        elif pos_hits and not neg_hits:
            result.sentiment = "positive"

        # 规则命中
        for rule in self.rules:
            hit, score = self._match_rule(rule, text)
            if hit:
                result.matched_rules.append(rule.name)
                result.relevance = min(100.0, result.relevance + score)
                if rule.category and result.category == "other":
                    result.category = rule.category
                if rule.sentiment:
                    result.sentiment = rule.sentiment
                if rule.alert:
                    result.is_alert = True

        # 负面情感自动升级为告警(舆情守门)
        if result.sentiment == "negative" and result.relevance >= 30:
            result.is_alert = True

        # 兜底: 无规则命中但含基础关注词的给基础相关分
        if not result.matched_rules and \
                any(kw in text for kw in self.base_keywords):
            result.relevance = max(result.relevance, 20.0)
        return result

    def _match_rule(self, rule: Rule, text: str) -> tuple[bool, float]:
        # 排除词一票否决
        for ex in rule.exclude:
            if ex and ex in text:
                return False, 0.0
        score = 0.0
        hit = False
        for kw in rule.include:
            if kw and kw in text:
                hit = True
                score += rule.weight
        for pattern in rule.regex:
            try:
                if re.search(pattern, text):
                    hit = True
                    score += rule.weight * 1.5  # 正则命中信息量更高
            except re.error:
                continue
        return hit, min(score, 80.0)
