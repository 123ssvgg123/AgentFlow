"""
    OrderAgent 图装配：agents/order/graph.py

    与 AGENT 结构对齐：graph 层只负责「把节点函数装配成状态图」，节点逻辑在 nodes.py。
    沿用 agents/base_agent.py 的 BaseAgent 基类，同时保留「未安装 langgraph 时降级为
    顺序执行器」的兜底能力。

    节点（10 个，与需求文档 3.3.1 对应）：
        parse_order / recommend_dishes / check_allergens / confirm_allergen_notice
        check_nutrition / calculate_price / validate_order / require_manual_review
        generate_summary / submit_order

    处理意图：dish_recommend / smart_order / allergen_query
"""
from typing import Any, Dict

from agents.base_agent import BaseAgent
from agents.order.nodes import (
    _MAX_RECOMMEND_RETRY,
    calculate_price,
    check_allergens,
    check_nutrition,
    confirm_allergen_notice,
    generate_summary,
    parse_order,
    recommend_dishes,
    require_manual_review,
    submit_order,
    validate_order,
)


class OrderAgent(BaseAgent):
    """智能点餐 Agent（10 节点 LangGraph 状态图）"""

    name = "OrderAgent"

    # ==================== 节点定义（10 个） ====================

    def define_nodes(self) -> Dict[str, Any]:
        return {
            "parse_order": parse_order,
            "recommend_dishes": recommend_dishes,
            "check_allergens": check_allergens,
            "confirm_allergen_notice": confirm_allergen_notice,
            "check_nutrition": check_nutrition,
            "calculate_price": calculate_price,
            "validate_order": validate_order,
            "generate_summary": generate_summary,
            "require_manual_review": require_manual_review,
            "submit_order": submit_order,
        }

    def define_structure(self) -> Dict[str, Any]:
        return {
            "entry": "parse_order",
            # 固定边（需求 3.3.1）
            "edges": [
                ("parse_order", "recommend_dishes"),          # 解析完需求直接进入推荐
                ("recommend_dishes", "check_allergens"),      # 推荐完毕后自动检测过敏原
                # 修复（二次审计）：原先条件边把 confirm_allergen_notice 直接接到 validate_order，
                # 跳过了 check_nutrition 与 calculate_price，导致「有过敏原风险」的订单
                # 最终没有营养分析与价格明细（price_detail/nutrition_info 为空）。
                # 口头告知只是插在过敏原检测与营养分析之间的一个合规节点，
                # 其出口必须是 check_nutrition，而不是跳过两个业务节点。
                ("confirm_allergen_notice", "check_nutrition"),
                ("check_nutrition", "calculate_price"),       # 营养分析后自动计算价格
                ("calculate_price", "validate_order"),        # 价格确认后校验订单完整性
                ("generate_summary", "submit_order"),         # 确认后提交到 POS 和后厨
            ],
            # 条件边（需求 3.3.1）
            "conditional_edges": {
                # 有过敏原风险 → 先做「口头告知确认留痕」再校验（标记特殊处理）；
                # 无风险 → 直接查营养
                "check_allergens": (
                    lambda s: "confirm_allergen_notice" if s.get("allergen_risk") in ("high", "medium")
                    else "check_nutrition",
                    {"confirm_allergen_notice": "confirm_allergen_notice",
                     "check_nutrition": "check_nutrition"},
                ),
                # 告知确认完成后 → 回到主流程做营养分析（见上方固定边）；
                # 原先这里把出口写成 validate_order，会漏掉营养与价格两个节点，已删除。
                # 校验通过 → 生成摘要；
                # 不通过且未超重试上限 → 回到推荐换菜；
                # 不通过且已达上限 → 转人工介入（关键修复：不再硬提交不可下单的订单）
                "validate_order": (
                    lambda s: "generate_summary" if s.get("order_valid")
                    else ("recommend_dishes"
                          if int(s.get("recommend_retry_count", 0)) <= _MAX_RECOMMEND_RETRY
                          else "require_manual_review"),
                    {"generate_summary": "generate_summary",
                     "recommend_dishes": "recommend_dishes",
                     "require_manual_review": "require_manual_review"},
                ),
            },
        }

    def node_order(self) -> list:
        return ["parse_order", "recommend_dishes", "check_allergens",
                "confirm_allergen_notice", "check_nutrition", "calculate_price",
                "validate_order", "generate_summary", "require_manual_review",
                "submit_order"]

    # ==================== 结果格式化 ====================

    @staticmethod
    def format_result(state: Dict[str, Any]) -> Dict[str, Any]:
        """把 State 格式化为 API 返回结构"""
        return {
            "answer": state.get("order_summary", "订单处理完成"),
            "order_id": state.get("order_id", ""),
            "dishes": state.get("recommended_dishes", []),
            "price": state.get("price_detail", {}),
            "allergen": state.get("allergen_result", {}),
            "allergen_risk": state.get("allergen_risk", "none"),
            "nutrition": state.get("nutrition_info", {}),
            "degraded_tools": state.get("degraded_tools", []),
            "errors": state.get("errors", []),
        }


# 全局单例（进程内复用，避免重复构建图）
order_agent = OrderAgent()
