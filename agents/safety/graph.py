"""
    SafetyAgent 图装配：agents/safety/graph.py

    与 AGENT 结构对齐：graph 层只负责「把节点函数装配成状态图」，节点逻辑在 nodes.py。
    沿用 agents/base_agent.py 的 BaseAgent 基类，同时保留「未安装 langgraph 时降级为
    顺序执行器」的兜底能力。

    节点（9 个，食安知识问答 RAG 流程）：
        classify_query / retrieve_knowledge / check_confidence / web_search
        generate_answer / generate_fallback / generate_general / enqueue_pending / save_memory

    处理意图：food_safety_query / nutrition_query
"""
from typing import Any, Dict

from agents.base_agent import BaseAgent
from agents.safety.nodes import (
    check_confidence,
    classify_query,
    enqueue_pending,
    generate_answer,
    generate_fallback,
    generate_general,
    retrieve_knowledge,
    save_memory,
    web_search,
)


class SafetyAgent(BaseAgent):
    """食安知识问答 Agent（9 节点 LangGraph 状态图）"""

    name = "SafetyAgent"

    # ==================== 节点定义（9 个） ====================

    def define_nodes(self) -> Dict[str, Any]:
        return {
            "classify_query": classify_query,
            "retrieve_knowledge": retrieve_knowledge,
            "check_confidence": check_confidence,
            "web_search": web_search,
            "generate_answer": generate_answer,
            "generate_fallback": generate_fallback,
            "generate_general": generate_general,
            "enqueue_pending": enqueue_pending,
            "save_memory": save_memory,
        }

    def define_structure(self) -> Dict[str, Any]:
        return {
            "entry": "classify_query",
            "edges": [
                ("retrieve_knowledge", "check_confidence"),  # 检索完判断置信度
                ("web_search", "generate_fallback"),         # 联网后进入兜底生成
                ("generate_answer", "save_memory"),          # 高置信答案 → 落定
                ("generate_general", "save_memory"),         # 通用直答 → 落定
                ("enqueue_pending", "save_memory"),          # 转人工 → 落定
            ],
            "conditional_edges": {
                # 法规/营养/过敏原 → 检索；通用 → 直答
                "classify_query": (
                    lambda s: "generate_general" if s.get("query_type") == "general"
                    else "retrieve_knowledge",
                    {"retrieve_knowledge": "retrieve_knowledge",
                     "generate_general": "generate_general"},
                ),
                # 检索命中 → 生成答案；未命中 → 联网兜底
                "check_confidence": (
                    lambda s: "generate_answer" if s.get("high_confidence") else "web_search",
                    {"generate_answer": "generate_answer", "web_search": "web_search"},
                ),
                # 兜底后仍无可答 → 转人工
                "generate_fallback": (
                    lambda s: "save_memory" if s.get("rescuable") else "enqueue_pending",
                    {"save_memory": "save_memory", "enqueue_pending": "enqueue_pending"},
                ),
            },
        }

    def node_order(self) -> list:
        return ["classify_query", "retrieve_knowledge", "check_confidence",
                "web_search", "generate_answer", "generate_fallback",
                "generate_general", "enqueue_pending", "save_memory"]

    # ==================== 结果格式化 ====================

    @staticmethod
    def format_result(state: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "answer": state.get("answer", "食安问答完成"),
            "query_type": state.get("query_type", "general"),
            "answer_sources": state.get("answer_sources", []),
            "pending": state.get("pending", False),
            "degraded_tools": state.get("degraded_tools", []),
            "errors": state.get("errors", []),
        }


# 全局单例（进程内复用，避免重复构建图）
safety_agent = SafetyAgent()
