"""
    SafetyAgent 专属状态：agents/safety/state.py

    与 AGENT 结构对齐：每个 Agent 独立维护自己的 State TypedDict，
    从共享的 BaseState（agents/state.py）继承隔离标识与会话字段。
"""
from typing import Any, Dict, List

from agents.state import BaseState


class SafetyState(BaseState, total=False):
    """SafetyAgent（食安知识问答）专属状态字段"""

    # classify_query 节点产出
    query_type: str                 # regulation / nutrition / allergen / general
    query_confidence: float
    # retrieve_knowledge 节点产出
    retrieved_knowledge: Dict[str, Any]
    retrieval_hit: bool
    # check_confidence 节点产出
    high_confidence: bool
    # web_search 节点产出
    web_results: Dict[str, Any]
    # generate_* 节点产出
    answer: str
    rescuable: bool
    # enqueue_pending 节点产出
    pending: bool
    pending_reason: str
    # save_memory 节点产出
    answer_sources: List[str]
