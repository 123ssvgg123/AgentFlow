"""
    ReviewAgent 图装配：agents/review/graph.py

    与 AGENT 结构对齐：graph 层只负责「把节点函数装配成状态图」，节点逻辑在 nodes.py。
    沿用 agents/base_agent.py 的 BaseAgent 基类，同时保留「未安装 langgraph 时降级为
    顺序执行器」的兜底能力。

    节点（7 个，与需求文档 3.3.3 对应）：
        collect_reviews / classify_sentiment / extract_topics / query_food_safety
        generate_report / check_negative / save_analysis

    处理意图：review_analysis / improvement_advice
"""
from typing import Any, Dict

from agents.base_agent import BaseAgent
from agents.review.nodes import (
    _MAX_REPORT_REVISION,
    check_negative,
    classify_sentiment,
    collect_reviews,
    extract_topics,
    generate_report,
    query_food_safety,
    save_analysis,
)


class ReviewAgent(BaseAgent):
    """评价分析 Agent（7 节点 LangGraph 状态图）"""

    name = "ReviewAgent"

    def define_nodes(self) -> Dict[str, Any]:
        return {
            "collect_reviews": collect_reviews,
            "classify_sentiment": classify_sentiment,
            "extract_topics": extract_topics,
            "query_food_safety": query_food_safety,
            "generate_report": generate_report,
            "check_negative": check_negative,
            "save_analysis": save_analysis,
        }

    def define_structure(self) -> Dict[str, Any]:
        return {
            "entry": "collect_reviews",
            "edges": [
                ("collect_reviews", "classify_sentiment"),    # 采集完自动分析情感
                ("classify_sentiment", "extract_topics"),     # 情感分析后提取主题
                ("query_food_safety", "generate_report"),     # 法规查询后生成含合规建议的报告
                # 修复（二次审计）：generate_report 原先没有出边，LangGraph 在报告生成后
                # 静默结束，check_negative 与 save_analysis 永远不执行（负面差评不会被
                # 标记、回复草稿不会生成、分析结果也不会入库），只能靠顺序执行器补跑。
                ("generate_report", "check_negative"),
            ],
            "conditional_edges": {
                # 涉及食品安全话题 → 先查法规；一般话题 → 直接生成报告
                "extract_topics": (
                    lambda s: "query_food_safety" if s.get("involves_food_safety") else "generate_report",
                    {"query_food_safety": "query_food_safety", "generate_report": "generate_report"},
                ),
                # 有严重差评且店长未确认 → 停在人工确认；否则保存。
                # 补出边后「要求修改」会形成 generate_report ↔ check_negative 回环，
                # 必须加上限（见 check_negative 里的 report_revision_count），
                # 否则 manager_request_revise 一直为真会无限重写报告。
                "check_negative": (
                    lambda s: "generate_report"
                    if (s.get("manager_request_revise")
                        and int(s.get("report_revision_count", 0)) < _MAX_REPORT_REVISION)
                    else "save_analysis",
                    {"generate_report": "generate_report", "save_analysis": "save_analysis"},
                ),
            },
        }

    def node_order(self) -> list:
        return ["collect_reviews", "classify_sentiment", "extract_topics",
                "query_food_safety", "generate_report", "check_negative", "save_analysis"]

    # ==================== 结果格式化 ====================

    @staticmethod
    def format_result(state: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "answer": state.get("full_report") or state.get("analysis_report", "评价分析完成"),
            "report": state.get("analysis_report", ""),
            "improvement": state.get("improvement_report", ""),
            "sentiment_stats": state.get("sentiment_stats", {}),
            "topic_stats": state.get("topic_stats", {}),
            "food_safety_info": state.get("food_safety_info", {}),
            "need_manager_review": state.get("need_manager_review", False),
            "reply_drafts": state.get("reply_drafts", []),
            "save_result": state.get("save_result", {}),
            "degraded_tools": state.get("degraded_tools", []),
            "errors": state.get("errors", []),
        }


# 全局单例
review_agent = ReviewAgent()
