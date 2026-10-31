"""
    SafetyAgent 节点函数：agents/safety/nodes.py

    与 AGENT 结构对齐：节点实现为「纯函数」——接收 state dict，返回增量 dict，
    由 graph.py 负责装配进 LangGraph 状态图（或降级顺序执行器）。

    节点清单（9 个，食安知识问答 RAG 流程）：
        classify_query / retrieve_knowledge / check_confidence / web_search
        generate_answer / generate_fallback / generate_general / enqueue_pending / save_memory
"""
from typing import Any, Dict

from agents.base_agent import node
from agents.safety.prompts import (
    CLASSIFY_QUERY,
    GENERATE_ANSWER,
    GENERATE_FALLBACK,
    GENERATE_GENERAL,
)
from base.config import config
from base.logger import logger
from tools.mcp_tools import query_food_safety_law, query_nutrition
from tools.mcp_tools import web_search as _web_search_tool
from tools.reranker import RERANK_HIGH_CONFIDENCE
from tools.safety_knowledge import ALLERGEN_MANAGEMENT_GUIDE

# 问题类型关键词（LLM 不可用时的规则兜底）
_REGULATION_KW = ["法规", "标准", "规范", "国标", "gb", "消毒", "留样", "冷藏", "冷冻",
                  "温度", "保质期", "追溯", "台账", "处罚", "合规", "事故", "报告", "采购", "验收"]
_NUTRITION_KW = ["营养", "热量", "卡路里", "蛋白质", "脂肪", "碳水", "钠", "健康",
                 "低脂", "低卡", "成分", "减肥", "膳食", "维生素"]
_ALLERGEN_KW = ["过敏", "致敏", "忌口", "麸质", "花生", "坚果", "蛋类", "乳制品",
                "甲壳", "海鲜过敏", "能吃"]


def _classify_by_rules(query: str) -> Dict[str, Any]:
    """规则分类兜底：按关键词判断问题类型"""
    text = (query or "").lower()
    if any(k in text for k in _ALLERGEN_KW):
        return {"query_type": "allergen", "confidence": 0.6}
    if any(k in text for k in _REGULATION_KW):
        return {"query_type": "regulation", "confidence": 0.6}
    if any(k in text for k in _NUTRITION_KW):
        return {"query_type": "nutrition", "confidence": 0.6}
    return {"query_type": "general", "confidence": 0.3}


# ==================== 节点 1：classify_query ====================

@node("分类食安问题类型（法规/营养/过敏原/通用）")
async def classify_query(state: Dict[str, Any]) -> Dict[str, Any]:
    logger.info("[SafetyAgent] 节点 1/9 classify_query —— 分类问题类型")
    query = state.get("user_input", "")

    result = None
    try:
        from tools.llm_client import llm_client
        result = await llm_client.chat_json(
            [{"role": "system", "content": "你是食安咨询分类助手，只输出 JSON。"},
             {"role": "user", "content": CLASSIFY_QUERY.format(query=query)}],
            temperature=0.1, default=None,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[SafetyAgent] LLM 分类失败，降级规则: {exc}")

    valid_types = ("regulation", "nutrition", "allergen", "general")
    if not isinstance(result, dict) or result.get("query_type") not in valid_types:
        result = _classify_by_rules(query)
        state.setdefault("degraded_tools", []).append("safety_classify_query")

    return {
        "query_type": result.get("query_type", "general"),
        "query_confidence": result.get("confidence", 0.5),
        "current_stage": "classified",
    }


# ==================== 节点 2：retrieve_knowledge ====================

@node("按问题类型检索本地食安/营养知识")
async def retrieve_knowledge(state: Dict[str, Any]) -> Dict[str, Any]:
    logger.info("[SafetyAgent] 节点 2/9 retrieve_knowledge —— 检索本地知识")
    query = state.get("user_input", "")
    qtype = state.get("query_type", "general")

    knowledge: Dict[str, Any] = {}
    hit = False
    # 检索置信度（0~1）：由各知识源返回，供 check_confidence 判断要不要联网兜底。
    # 参考 AGENT 的 bge-reranker：Top-1 分数 >= 0.75 才认为本地知识够用。
    confidence = 0.0

    if qtype == "regulation":
        law = await query_food_safety_law(query)
        knowledge = {"law": law}
        hit = bool(law.get("laws"))
        confidence = float(law.get("confidence", 0.0) or 0.0)
        if law.get("degraded"):
            state.setdefault("degraded_tools", []).append("query_food_safety_law")
    elif qtype == "allergen":
        law = await query_food_safety_law(query)
        knowledge = {"law": law, "allergen_guide": ALLERGEN_MANAGEMENT_GUIDE}
        hit = True  # 过敏原类必有内置规范可答
        # 内置《过敏原管理规范》是确定性知识，给高置信直接作答
        confidence = max(0.9, float(law.get("confidence", 0.0) or 0.0))
        if law.get("degraded"):
            state.setdefault("degraded_tools", []).append("query_food_safety_law")
    elif qtype == "nutrition":
        dish = _extract_dish(query)
        nutrition = await query_nutrition(dish or query)
        knowledge = {"nutrition": nutrition}
        hit = bool(nutrition.get("nutrition"))
        # 命中内置营养表 0.92；未命中靠类别估算，置信度压低以触发联网补充
        exact = hit and not str(nutrition.get("note", "")).startswith("未收录")
        confidence = 0.92 if exact else (0.40 if hit else 0.0)
        if nutrition.get("degraded"):
            state.setdefault("degraded_tools", []).append("query_nutrition")

    logger.info(f"[SafetyAgent] 检索完成 type={qtype} hit={hit} confidence={confidence:.2f}")
    return {
        "retrieved_knowledge": knowledge,
        "retrieval_hit": hit,
        "retrieval_confidence": round(confidence, 4),
        "current_stage": "retrieved",
    }


def _extract_dish(query: str) -> str:
    """从问题中抽取菜品名（用于营养查询），未命中返回空串"""
    from tools.menu_data import MENU_DB
    for dish in MENU_DB:
        name = dish.get("dish_name", "")
        if name and name in query:
            return name
    return ""


# ==================== 节点 3：check_confidence ====================

@node("判断检索是否命中（低置信走联网兜底）")
async def check_confidence(state: Dict[str, Any]) -> Dict[str, Any]:
    logger.info("[SafetyAgent] 节点 3/9 check_confidence —— 置信度判断")
    hit = bool(state.get("retrieval_hit", False))
    confidence = float(state.get("retrieval_confidence", 0.0) or 0.0)
    # 阈值与 AGENT 的 bge-reranker 对齐：0.75 以上才认为「本地知识够用」。
    # 低于阈值的走 web_search 补充；即使联网也失败，generate_fallback 仍会用
    # 已检索到的 knowledge 兜底，所以「低置信」不等于「答不出来」。
    high_confidence = hit and confidence >= RERANK_HIGH_CONFIDENCE
    logger.info(f"[SafetyAgent] 置信度 {confidence:.2f}（阈值 {RERANK_HIGH_CONFIDENCE}）"
                f" → {'直接作答' if high_confidence else '联网兜底'}")
    return {
        "high_confidence": high_confidence,
        "retrieval_confidence": round(confidence, 4),
        "current_stage": "confidence_checked",
    }


# ==================== 节点 4：web_search ====================

@node("联网兜底搜索")
async def web_search(state: Dict[str, Any]) -> Dict[str, Any]:
    logger.info("[SafetyAgent] 节点 4/9 web_search —— 联网兜底搜索")
    query = state.get("user_input", "")
    result = await _web_search_tool(query, max_results=3)
    if result.get("degraded"):
        state.setdefault("degraded_tools", []).append("web_search")
    return {
        "web_results": result,
        "current_stage": "web_searched",
    }


# ==================== 节点 5：generate_answer ====================

@node("基于检索知识生成专业答案（LLM）")
async def generate_answer(state: Dict[str, Any]) -> Dict[str, Any]:
    logger.info("[SafetyAgent] 节点 5/9 generate_answer —— 生成答案")
    query = state.get("user_input", "")
    knowledge = state.get("retrieved_knowledge", {})

    answer = ""
    try:
        from tools.llm_client import llm_client
        answer = await llm_client.chat(
            [{"role": "system", "content": "你是连锁餐饮的食品安全顾问。"},
             {"role": "user", "content": GENERATE_ANSWER.format(query=query, knowledge=_brief(knowledge))}],
            temperature=0.3, max_retries=0,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[SafetyAgent] LLM 生成失败，降级模板: {exc}")
        answer = _template_answer(query, knowledge)
        state.setdefault("degraded_tools", []).append("safety_generate_answer")

    return {"answer": answer, "current_stage": "answered"}


# ==================== 节点 6：generate_fallback ====================

@node("低置信度兜底生成（通用知识）")
async def generate_fallback(state: Dict[str, Any]) -> Dict[str, Any]:
    logger.info("[SafetyAgent] 节点 6/9 generate_fallback —— 兜底生成")
    query = state.get("user_input", "")
    knowledge = state.get("retrieved_knowledge", {})
    web = state.get("web_results", {})

    answer = ""
    try:
        from tools.llm_client import llm_client
        answer = await llm_client.chat(
            [{"role": "system", "content": "你是连锁餐饮的食品安全顾问。"},
             {"role": "user", "content": GENERATE_FALLBACK.format(
                 query=query, knowledge=_brief(knowledge) + "\n联网结果：" + _brief(web))}],
            temperature=0.3, max_retries=0,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[SafetyAgent] 兜底 LLM 失败: {exc}")
        answer = _template_answer(query, knowledge)
        state.setdefault("degraded_tools", []).append("safety_generate_fallback")

    # 兜底后是否仍可回答：有内置知识或有联网结果
    rescuable = bool(knowledge) or bool(web.get("results"))
    return {"answer": answer, "rescuable": rescuable, "current_stage": "fallback_generated"}


# ==================== 节点 7：generate_general ====================

@node("通用问题直答")
async def generate_general(state: Dict[str, Any]) -> Dict[str, Any]:
    logger.info("[SafetyAgent] 节点 7/9 generate_general —— 通用直答")
    query = state.get("user_input", "")

    answer = ""
    try:
        from tools.llm_client import llm_client
        answer = await llm_client.chat(
            [{"role": "system", "content": "你是连锁餐饮智能助手。"},
             {"role": "user", "content": GENERATE_GENERAL.format(query=query)}],
            temperature=0.3, max_retries=0,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[SafetyAgent] 通用直答 LLM 失败: {exc}")
        answer = f"抱歉，这个问题暂时无法回答，建议咨询店内服务人员或拨打 {config.SERVICE_PHONE}"
        state.setdefault("degraded_tools", []).append("safety_generate_general")

    return {"answer": answer, "current_stage": "answered"}


# ==================== 节点 8：enqueue_pending ====================

@node("无法回答时挂起转人工")
async def enqueue_pending(state: Dict[str, Any]) -> Dict[str, Any]:
    logger.warning("[SafetyAgent] 节点 8/9 enqueue_pending —— 转人工")
    query = state.get("user_input", "")
    answer = (
        f"您的问题「{query[:50]}」在内置知识库中暂未找到明确答案。\n"
        f"已为您转人工：可拨打咨询热线 {config.SERVICE_PHONE}，或咨询当地市场监管部门。"
    )
    return {
        "answer": answer,
        "pending": True,
        "pending_reason": "内置知识库未覆盖",
        "current_stage": "pending",
    }


# ==================== 节点 9：save_memory ====================

@node("落定最终答案（Checkpointer 自动持久化对话）")
async def save_memory(state: Dict[str, Any]) -> Dict[str, Any]:
    logger.info("[SafetyAgent] 节点 9/9 save_memory —— 落定答案")
    # 对话状态由 LangGraph Checkpointer 自动持久化，这里只做终态标记与来源整理
    knowledge = state.get("retrieved_knowledge", {})
    sources = []
    law = knowledge.get("law")
    if law and law.get("laws"):
        sources = [item.get("title", "") for item in law["laws"] if item.get("title")]
    return {
        "answer_sources": sources,
        "current_stage": "saved",
    }


# ==================== 内部工具函数 ====================

def _brief(data: Any, max_items: int = 10) -> str:
    """把结构化数据压缩为提示词友好的字符串（限制长度，控制 token 成本）"""
    import json as _json
    if isinstance(data, list):
        data = data[:max_items]
    return _json.dumps(data, ensure_ascii=False, default=str)[:4000]


def _template_answer(query: str, knowledge: Dict[str, Any]) -> str:
    """LLM 不可用时的模板答案（基于内置知识）"""
    law = knowledge.get("law")
    if law and law.get("laws"):
        lines = ["（降级模式）根据内置食品安全知识库，为你找到以下相关条款：", ""]
        for item in law["laws"][:3]:
            lines.append(f"- **{item.get('title', '')}**：{item.get('content', '')}")
        return "\n".join(lines)
    nutrition = knowledge.get("nutrition")
    if nutrition and nutrition.get("nutrition"):
        n = nutrition["nutrition"]
        return (f"（降级模式）该菜品营养成分（每份估算）：热量 {n.get('calories', 0)} kcal，"
                f"蛋白质 {n.get('protein', 0)} g，脂肪 {n.get('fat', 0)} g，"
                f"碳水化合物 {n.get('carb', 0)} g，钠 {n.get('sodium', 0)} mg。")
    return "（降级模式）已转人工处理，请咨询店内服务人员。"
