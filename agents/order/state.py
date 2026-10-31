"""
    OrderAgent 专属状态：agents/order/state.py

    与 AGENT 结构对齐：每个 Agent 独立维护自己的 State TypedDict，
    从共享的 BaseState（agents/state.py）继承隔离标识与会话字段。

    说明：LangGraph 侧仍以 dict 为运行时载体（见 agents/base_agent.py），
    此处 TypedDict 用于结构化文档与类型提示。
"""
from typing import Any, Dict, List

from agents.state import BaseState


class OrderState(BaseState, total=False):
    """OrderAgent（智能点餐）专属状态字段"""

    # parse_order 节点产出
    customer_needs: Dict[str, Any]     # 结构化需求
    # recommend_dishes 节点产出
    recommended_dishes: List[Dict[str, Any]]
    recommendation_notes: str
    # check_allergens 节点产出
    allergen_result: Dict[str, Any]
    allergen_risk: str                 # none / medium / high / unknown
    # confirm_allergen_notice 节点产出（需求 3.8 Q8 口头告知留痕）
    allergen_notice: Dict[str, Any]    # 告知话术 + 隔离措施 + 是否已确认
    allergen_notice_pending: bool      # True = 尚未确认，需服务员操作
    # check_nutrition 节点产出
    nutrition_info: Dict[str, Any]
    # calculate_price 节点产出
    price_detail: Dict[str, Any]
    # validate_order 节点产出
    order_valid: bool
    validation_issues: List[str]
    all_in_stock: bool
    needs_manual_review: bool          # 校验失败且重试耗尽 → 转人工（不下单）
    # generate_summary 节点产出
    order_summary: str
    # submit_order 节点产出
    order_id: str
    submit_result: Dict[str, Any]
    # 订单状态机（memory/order_state.py）
    order_status: str                  # created / submitted / manual_review / cancelled
    submit_blocked: bool               # 状态机拒绝提交（如终态重复提交）
