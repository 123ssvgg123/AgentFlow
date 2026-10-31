"""
    KitchenAgent 图装配：agents/kitchen/graph.py

    与 AGENT 结构对齐：graph 层只负责「把节点函数装配成状态图」，节点逻辑在 nodes.py。
    沿用 agents/base_agent.py 的 BaseAgent 基类，同时保留「未安装 langgraph 时降级为
    顺序执行器」的兜底能力。

    节点（7 个，与需求文档 3.3.2 对应）：
        load_orders / check_inventory / generate_tasks / schedule_tasks
        check_priority / assign_stations / dispatch_orders

    处理意图：kitchen_dispatch / inventory_check / order_priority
"""
from typing import Any, Dict

from agents.base_agent import BaseAgent
from agents.kitchen.nodes import (
    _MAX_INVENTORY_RETRY,
    assign_stations,
    check_inventory,
    check_priority,
    dispatch_orders,
    generate_tasks,
    load_orders,
    schedule_tasks,
)


class KitchenAgent(BaseAgent):
    """后厨调度 Agent（7 节点 LangGraph 状态图）"""

    name = "KitchenAgent"

    def define_nodes(self) -> Dict[str, Any]:
        return {
            "load_orders": load_orders,
            "check_inventory": check_inventory,
            "generate_tasks": generate_tasks,
            "schedule_tasks": schedule_tasks,
            "check_priority": check_priority,
            "assign_stations": assign_stations,
            "dispatch_orders": dispatch_orders,
        }

    def define_structure(self) -> Dict[str, Any]:
        return {
            "entry": "load_orders",
            "edges": [
                # 修复（二次审计）：入口节点 load_orders 原先没有任何出边。
                # LangGraph 对「无出边节点」视为正常结束（不报错），整张图在
                # load_orders 之后静默终止，只能靠 BaseAgent._reached_terminal
                # 检测到 stage 不是 dispatched 再降级重跑一遍顺序执行器。
                # 结果每次后厨调度都执行两遍，且两遍计算会相互覆盖状态。
                ("load_orders", "check_inventory"),        # 加载订单后立即核对库存
                ("generate_tasks", "schedule_tasks"),      # 任务生成后进入排产
                ("schedule_tasks", "check_priority"),      # 排产后判断是否需要人工调整
                ("assign_stations", "dispatch_orders"),    # 分配完成后下发到 KDS
            ],
            "conditional_edges": {
                # 库存充足 → 生成任务；
                # 库存不足 → 重新加载订单（标记缺料），但设有重试上限：
                #   修复：原先「不足就回 load_orders」是回退到图入口节点，
                #   LangGraph 无法从非入口节点跳回入口，导致整张图在 check_inventory
                #   之后静默终止。现在改为：缺料时先回 load_orders 重载一次，
                #   若重载后仍不足，则带着缺料预警继续往下走（不阻断出餐）。
                "check_inventory": (
                    lambda s: "generate_tasks"
                    if (s.get("inventory_sufficient")
                        or int(s.get("inventory_retry", 0)) >= _MAX_INVENTORY_RETRY)
                    else "load_orders",
                    {"generate_tasks": "generate_tasks", "load_orders": "load_orders"},
                ),
                # 主管调整了优先级 → 回到 schedule_tasks 重新排产；
                # 否则直接分配工位。
                # 修复：原先两支都指向 assign_stations，"回 schedule_tasks 重排"
                #       这条回环从未真正生效。
                "check_priority": (
                    lambda s: "schedule_tasks"
                    if (s.get("priority_adjustment") and not s.get("adjustment_applied"))
                    else "assign_stations",
                    {"schedule_tasks": "schedule_tasks", "assign_stations": "assign_stations"},
                ),
            },
        }

    def node_order(self) -> list:
        return ["load_orders", "check_inventory", "generate_tasks", "schedule_tasks",
                "check_priority", "assign_stations", "dispatch_orders"]

    # ==================== 结果格式化 ====================

    @staticmethod
    def format_result(state: Dict[str, Any]) -> Dict[str, Any]:
        """把 State 格式化为 API 返回结构（含排产看板 Markdown）"""
        scheduled = state.get("scheduled_orders", [])
        assignment = state.get("station_assignment", {})
        inventory = state.get("inventory_result", {})

        lines = [f"## 后厨排产方案", "",
                 f"当前排队 **{state.get('order_count', 0)}** 单", ""]
        if scheduled:
            lines.append("| 优先级 | 桌号 | 菜品数 | 特殊要求 | 等待 | 建议工位 |")
            lines.append("|--------|------|--------|----------|------|----------|")
            station_map = {}
            for station, info in assignment.get("stations", {}).items():
                for t in info.get("tasks", []):
                    station_map[t.get("order_id")] = station
            for o in scheduled[:15]:
                level = o.get("priority_level", "P3")
                mark = {"P1": "P1 高", "P2": "P2 中", "P3": "P3 低"}.get(level, level)
                special = "、".join(o.get("special_requests", [])) or "无"
                lines.append(f"| {mark} | {o.get('桌号', '')} | {len(o.get('菜品列表', []))} | "
                             f"{special} | {o.get('waited_minutes', 0)}分钟 | "
                             f"{station_map.get(o.get('订单号'), '-')} |")

        # 食材预警
        shortages = inventory.get("shortages", [])
        warnings = inventory.get("warning_items", [])
        if shortages:
            lines += ["", "> 食材预警：库存不足 —— " +
                      "、".join(f"{s['ingredient']}（缺 {s['shortage']}）" for s in shortages)]
        elif warnings:
            lines += ["", "> 食材提醒：以下食材库存偏低 —— " +
                      "、".join(w.get("ingredient", "") for w in warnings[:5])]

        if state.get("need_human_confirm"):
            lines += ["", f"> 需要您确认：{'；'.join(state.get('confirm_reason', []))}",
                      "> 请回复「确认」继续下发，或告知需要调整的订单（如「把 B12 提到最前」）。"]

        overloaded = assignment.get("overloaded", [])
        if overloaded:
            lines += ["", f"> 工位负载提示：{'、'.join(overloaded)} 已超出建议产能，建议增派人手。"]

        return {
            "answer": "\n".join(lines),
            "order_count": state.get("order_count", 0),
            "scheduled_orders": scheduled,
            "assignment": assignment,
            "inventory": inventory,
            "need_human_confirm": state.get("need_human_confirm", False),
            "confirm_reason": state.get("confirm_reason", []),
            "dispatch": state.get("dispatch_result", {}),
            "degraded_tools": state.get("degraded_tools", []),
            "errors": state.get("errors", []),
        }


# 全局单例
kitchen_agent = KitchenAgent()
