"""内容去重: MD5 精确去重 + 两段式近似去重(SimHash 预筛 + Jaccard 确认)

对抗性视角: 同一营销内容会被矩阵账号反复发布(洗稿/搬运)。精确哈希
抓不住改几个字的变体; 纯 SimHash 对短文本(小红书笔记普遍 < 200 字)
方差过大 — 改 2 个字海明距离就可能到 10+, 固定小阈值(如 3)只适用于
长文档。因此采用两级策略:
  1) SimHash 海明距离 <= 20: 廉价预筛(整数异或, 微秒级), 排除绝大多数
     无关文本(无关文本期望距离约 32)
  2) 分词 Jaccard >= 0.7: 精确确认, 对文本长度天然稳健
"""

from __future__ import annotations

import hashlib
import re

# SimHash 预筛阈值: 64 位指纹, 无关文本期望海明距离 ~32, 同文变体 < 20
SIMHASH_PREFILTER = 20
# Jaccard 确认阈值: 共享分词比例
JACCARD_CONFIRM = 0.7


def normalize(text: str) -> str:
    """归一化: 去空白/标点/表情, 转小写 — 只留语义载体."""
    text = text or ""
    text = re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE)
    return text.lower()


def exact_hash(title: str, desc: str = "") -> str:
    return hashlib.md5(
        (normalize(title) + "|" + normalize(desc)).encode("utf-8")
    ).hexdigest()


def _tokens(text: str) -> list[str]:
    """简易中文 bigram + 英文单词分词(无需外部分词依赖)."""
    text = normalize(text)
    if not text:
        return []
    words = re.findall(r"[a-z0-9]+", text)
    han = re.sub(r"[a-z0-9]", "", text)
    bigrams = [han[i:i + 2] for i in range(len(han) - 1)]
    return words + bigrams


def simhash(text: str, hashbits: int = 64) -> int:
    tokens = _tokens(text)
    if not tokens:
        return 0
    weights = [0] * hashbits
    for tok in tokens:
        h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(),
                 16) & ((1 << hashbits) - 1)
        for i in range(hashbits):
            if h & (1 << i):
                weights[i] += 1
            else:
                weights[i] -= 1
    fingerprint = 0
    for i in range(hashbits):
        if weights[i] > 0:
            fingerprint |= (1 << i)
    return fingerprint


def hamming_distance(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def simhash_prefilter(sim_a: str | int, sim_b: str | int,
                      threshold: int = SIMHASH_PREFILTER) -> bool:
    """预筛: 两指纹是否可能为同文变体(宽阈值, 只排除明显无关)."""
    try:
        ia = int(sim_a, 16) if isinstance(sim_a, str) else sim_a
        ib = int(sim_b, 16) if isinstance(sim_b, str) else sim_b
        return hamming_distance(ia, ib) <= threshold
    except (ValueError, TypeError):
        return False


def jaccard(text_a: str, text_b: str) -> float:
    ta, tb = set(_tokens(text_a)), set(_tokens(text_b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def is_near_duplicate(text_a: str, text_b: str,
                      jaccard_threshold: float = JACCARD_CONFIRM) -> bool:
    """两段式判定: 是否为同文变体(洗稿/搬运)."""
    return jaccard(text_a, text_b) >= jaccard_threshold
