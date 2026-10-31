"""
    KitchenAgent 专属状态：agents/kitchen/state.py

    与 AGENT 结构对齐：每个 Agent 独立维护自己的 State TypedDict，
    从共享的 BaseState（agents/state.py）继承隔离标识与会话字段。
"""
from typing import Any, Dict, List

from agents.state import BaseState


class KitchenState(BaseState, total=False):
    """KitchenAgent（后厨调度）专属状态字段"""

    # load_orders 节点产出
    pending_orders: List[Dict[str, Any]]
    store_info: Dict[str, Any]
    order_count: int
    # check_inventory 节点产出
    inventory: Dict[str, Any]
    inventory_result: Dict[str, Any]
    # generate_tasks 节点产出
    kitchen_tasks: List[Dict[str, Any]]
    # schedule_tasks 节点产出
    scheduled_orders: List[Dict[str, Any]]
    adjustment_applied: bool
    # 排除清单（两个维度，避免语义混淆）
    excluded_dishes: List[str]           # 菜品名：售罄/不可做
    excluded_ingredients: List[str]      # 食材名：缺料，需剔除用到该食材的菜品
    # check_priority 节点产出（Human-in-the-Loop）
    need_human_confirm: bool
    priority_adjustment: str
    # assign_stations 节点产出
    station_assignment: Dict[str, Any]
    # dispatch_orders 节点产出
    dispatch_result: Dict[str, Any]
