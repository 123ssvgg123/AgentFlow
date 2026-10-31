"""
    轻量精排（Lexical Reranker）：tools/reranker.py

    背景（参考 D:/heima/AGENT 的 backend/core/reranker.py）：
        AGENT 项目用 `BAAI/bge-reranker-v2-m3`（CrossEncoder + torch）做
        「向量召回 10 条 → 精排 3 条 → 取 Top-1 分数做置信度」，并用
        0.75 作为「本地知识够不够用」的阈值，低置信转联网兜底。

    本项目的取舍：
        FoodAgent 的定位是「不装重型依赖也能跑」（见 README 第九节降级矩阵），
        引入 torch + 2GB 模型会让 Docker 镜像与冷启动时间失控。因此这里**只搬结构**：
            宽召回（关键词/规则） → 轻量精排（本模块） → 置信度阈值 → 低置信转联网
        精排本身用纯 Python 的可解释词法打分（关键词覆盖率 + 密度 + 头部命中），
        既不需要模型文件，也能给出「本轮检索到底有多可信」的量化值。

    设计约束：
        · 纯函数、无全局状态、无 I/O，方便单测与并发调用
        · 分数恒在 [0, 1]，0.75 以上视为「高置信」（与 AGENT 的阈值对齐）
        · 排序稳定：同分按原始顺序，保证结果可复现
"""
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence, Tuple, Union

# 高置信阈值：与 AGENT 项目 core/reranker.py 的 0.75 保持一致，
# 让「低置信 → 联网兜底」这条链路的判断标准可以跨项目复用。
RERANK_HIGH_CONFIDENCE = 0.75

# 单条文本参与打分的最大长度：长文档后半段与查询的相关性通常已被噪声稀释，
# 截断既省算力，也避免「长文靠堆关键词刷高分」。
RERANK_MAX_INPUT_CHARS = 512

# 中文虚词/口语词：这些词几乎出现在任何句子里，计入覆盖率只会让所有候选同分
_STOP_CHARS = set("的了是在和有我要点一个份吗呢吧呀啊请帮给推荐查询关于怎么什么哪些如何可以需要今天我们你他她它们这那")

_WORD_RE = re.compile(r"[a-zA-Z0-9]+")
# 连续的汉字串（中英混排时先按非汉字切开）
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")

DocumentLike = Union[str, Dict[str, Any]]


@dataclass
class RankedDocument:
    """一条精排结果（对应 AGENT 里的 RankedDocument）"""
    content: str
    score: float
    original_index: int
    metadata: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        """展开成字典，便于直接塞进 HTTP 响应或 State"""
        merged = dict(self.metadata)
        merged["_rerank_score"] = self.score
        merged["_rerank_index"] = self.original_index
        return merged


def _tokenize(text: str) -> List[str]:
    """
    轻量分词：英文/数字按词，中文按「单字（去虚词）+ 双字 bigram」。

    为什么用 bigram：中文没有空格，纯单字匹配会把「牛肉」和「牛奶」都算命中「牛」；
    加 bigram 后「牛肉」这个组合词能显著拉开分差，而双字组合的召回率也足够。
    """
    text = (text or "").lower()
    if not text:
        return []

    tokens: List[str] = _WORD_RE.findall(text)
    for segment in _CJK_RE.findall(text):
        tokens.extend(ch for ch in segment if ch not in _STOP_CHARS)
        tokens.extend(segment[i:i + 2] for i in range(len(segment) - 1))
    return tokens


def lexical_similarity(query: str, text: str) -> float:
    """
    查询与单条文本的相似度（0~1）。

    三部分加权：
        · 覆盖率 0.60 —— 查询词有多少被文本覆盖（最重要，避免「只命中一个词就高分」）
        · 密度   0.25 —— 命中词在文本中的出现频次（防止长文里偶然出现一次就上位）
        · 头部   0.15 —— 是否命中文本前 1/3（标题/摘要区命中通常更相关）
    """
    query_tokens = _tokenize(query)
    doc_tokens = _tokenize(text)
    if not query_tokens or not doc_tokens:
        return 0.0

    q_set = set(query_tokens)
    d_set = set(doc_tokens)
    matched = q_set & d_set
    if not matched:
        return 0.0

    coverage = len(matched) / len(q_set)
    hits = sum(doc_tokens.count(token) for token in matched)
    density = min(1.0, hits / max(len(doc_tokens), 1) * 3.0)

    head_tokens = set(doc_tokens[: max(1, len(doc_tokens) // 3)])
    head = 1.0 if (matched & head_tokens) else 0.0

    return round(min(1.0, 0.60 * coverage + 0.25 * density + 0.15 * head), 4)


def _document_text(document: DocumentLike, text_keys: Sequence[str]) -> str:
    """从字符串/字典里取出参与打分的文本（截断到 RERANK_MAX_INPUT_CHARS）"""
    if isinstance(document, str):
        raw = document
    else:
        parts: List[str] = []
        for key in text_keys:
            value = document.get(key)
            if isinstance(value, (list, tuple)):
                parts.append(" ".join(str(v) for v in value))
            elif value is not None:
                parts.append(str(value))
        raw = " ".join(parts)
    return raw[:RERANK_MAX_INPUT_CHARS]


def rerank(query: str, documents: Sequence[DocumentLike], top_k: int = 3,
           text_keys: Sequence[str] = ("content",)) -> Tuple[List[RankedDocument], float]:
    """
    精排主入口。

    参数：
        query:     用户问题
        documents: 召回结果（str 或 dict）
        top_k:     保留条数
        text_keys: dict 场景下参与打分的字段（按顺序拼接）
    返回：
        (排序后的 RankedDocument 列表, 置信度)
        置信度 = Top-1 分数；无候选时为 0.0（与 AGENT 的语义完全一致）
    """
    if not documents:
        return [], 0.0
    if isinstance(text_keys, str):  # 容错：允许传单个字段名
        text_keys = (text_keys,)

    ranked = [
        RankedDocument(
            content=_document_text(doc, text_keys),
            score=lexical_similarity(query, _document_text(doc, text_keys)),
            original_index=index,
            metadata=dict(doc) if isinstance(doc, dict) else {},
        )
        for index, doc in enumerate(documents)
    ]
    # 同分按原始顺序，保证结果可复现（sort 是稳定的，这里仍显式写出排序键）
    ranked.sort(key=lambda item: (-item.score, item.original_index))
    top = ranked[: max(1, top_k)]
    return top, (top[0].score if top else 0.0)


def rerank_dicts(query: str, documents: Sequence[Dict[str, Any]], top_k: int = 3,
                 text_keys: Sequence[str] = ("content",),
                 score_key: str = "_rerank_score") -> Tuple[List[Dict[str, Any]], float]:
    """
    rerank 的「字典进、字典出」版本，供 mcp_tools 直接使用。

    返回的每条记录保留原字段，并额外带上 `_rerank_score`（可解释、可断言）。
    """
    if not documents:
        return [], 0.0
    if isinstance(text_keys, str):
        text_keys = (text_keys,)

    scored: List[Dict[str, Any]] = []
    for index, doc in enumerate(documents):
        text = _document_text(doc, text_keys)
        item = dict(doc)
        item[score_key] = lexical_similarity(query, text)
        item["_rerank_index"] = index
        scored.append(item)

    scored.sort(key=lambda item: (-item[score_key], item["_rerank_index"]))
    top = scored[: max(1, top_k)]
    return top, (top[0][score_key] if top else 0.0)


def is_confident(confidence: float) -> bool:
    """置信度是否达到「本地知识够用」的阈值"""
    return float(confidence or 0.0) >= RERANK_HIGH_CONFIDENCE


if __name__ == "__main__":
    print("=" * 64)
    print("轻量精排自检（tools/reranker.py）")
    print("=" * 64)
    candidates = [
        {"dish_name": "宫保鸡丁", "category": "热菜", "tags": ["川菜", "微辣", "鸡肉"]},
        {"dish_name": "牛奶布丁", "category": "甜品", "tags": ["奶制品", "甜"]},
        {"dish_name": "番茄牛腩", "category": "热菜", "tags": ["炖菜", "牛肉", "番茄"]},
    ]
    ranked, confidence = rerank_dicts(
        "有没有牛肉类的热菜", candidates, top_k=2,
        text_keys=("dish_name", "category", "tags"),
    )
    for item in ranked:
        print(f"  {item['_rerank_score']:.3f}  {item['dish_name']}")
    print(f"置信度：{confidence:.3f}（阈值 {RERANK_HIGH_CONFIDENCE}）")
    assert ranked[0]["dish_name"] == "番茄牛腩", "牛腩应排在牛奶之前"
    assert 0.0 <= confidence <= 1.0
    print("自检通过")
