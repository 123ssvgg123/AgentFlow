"""
    兼容性 re-export：agents/kitchen_agent.py

    KitchenAgent 已按 AGENT 结构拆分到 agents/kitchen/ 子包：
        state.py / prompts.py / nodes.py / graph.py

    本文件仅做向后兼容的 re-export，保证既有调用方
    `from agents.kitchen_agent import KitchenAgent, kitchen_agent` 无需改动。
"""
from agents.kitchen.graph import KitchenAgent, kitchen_agent

__all__ = ["KitchenAgent", "kitchen_agent"]
