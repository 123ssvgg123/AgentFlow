"""
    兼容性 re-export：agents/safety_agent.py

    SafetyAgent 已按 AGENT 结构拆分到 agents/safety/ 子包：
        state.py / prompts.py / nodes.py / graph.py

    本文件仅做向后兼容的 re-export，保证既有调用方
    （api/routes.py、agents/orchestrator.py 等）
    `from agents.safety_agent import SafetyAgent, safety_agent` 无需改动。
"""
from agents.safety.graph import SafetyAgent, safety_agent

__all__ = ["SafetyAgent", "safety_agent"]
