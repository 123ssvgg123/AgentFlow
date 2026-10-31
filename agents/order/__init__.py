"""
    OrderAgent（智能点餐）子包：agents/order/

    与 AGENT 结构对齐，按职责拆分：
        state.py     OrderAgent 专属 State TypedDict
        prompts.py   节点级提示词（ORDER_SUMMARY）
        nodes.py     节点函数（纯函数：读 state → 返回增量 dict）
        graph.py     OrderAgent 图组装（BaseAgent 子类）+ 全局单例
"""
from agents.order.graph import OrderAgent, order_agent
from agents.order.nodes import _MAX_RECOMMEND_RETRY

__all__ = ["OrderAgent", "order_agent", "_MAX_RECOMMEND_RETRY"]
