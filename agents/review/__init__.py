"""
    ReviewAgent（评价分析）子包：agents/review/

    与 AGENT 结构对齐，按职责拆分：
        state.py     ReviewAgent 专属 State TypedDict
        prompts.py   节点级提示词（NEGATIVE_REPLY）
        nodes.py     节点函数（纯函数：读 state → 返回增量 dict）
        graph.py     ReviewAgent 图组装（BaseAgent 子类）+ 全局单例
"""
from agents.review.graph import ReviewAgent, review_agent

__all__ = ["ReviewAgent", "review_agent"]
