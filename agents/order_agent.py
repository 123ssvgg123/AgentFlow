"""
    兼容性 re-export：agents/order_agent.py

    OrderAgent 已按 AGENT 结构拆分到 agents/order/ 子包：
        state.py / prompts.py / nodes.py / graph.py

    本文件仅做向后兼容的 re-export，保证既有调用方
    （api/routes.py、scripts/*、agents/orchestrator.py 等）
    `from agents.order_agent import OrderAgent, order_agent, _MAX_RECOMMEND_RETRY` 无需改动。
"""
from agents.order.graph import OrderAgent, order_agent
from agents.order.nodes import _MAX_RECOMMEND_RETRY

__all__ = ["OrderAgent", "order_agent", "_MAX_RECOMMEND_RETRY"]
