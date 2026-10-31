"""
    SafetyAgent（食安知识问答）子包：agents/safety/

    与 AGENT 结构对齐，按职责拆分：
        state.py     SafetyAgent 专属 State TypedDict
        prompts.py   节点级提示词（分类 / 答案生成 / 兜底）
        nodes.py     节点函数（纯函数：读 state → 返回增量 dict）
        graph.py     SafetyAgent 图组装（BaseAgent 子类）+ 全局单例
"""
from agents.safety.graph import SafetyAgent, safety_agent

__all__ = ["SafetyAgent", "safety_agent"]
