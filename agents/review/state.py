"""
    ReviewAgent 专属状态：agents/review/state.py

    与 AGENT 结构对齐：每个 Agent 独立维护自己的 State TypedDict，
    从共享的 BaseState（agents/state.py）继承隔离标识与会话字段。
"""
from typing import Any, Dict, List

from agents.state import BaseState


class ReviewState(BaseState, total=False):
    """ReviewAgent（评价分析）专属状态字段"""

    # collect_reviews 节点产出
    reviews: List[Dict[str, Any]]
    review_count: int
    # classify_sentiment 节点产出
    sentiment_results: List[Dict[str, Any]]
    sentiment_stats: Dict[str, Any]
    # extract_topics 节点产出
    topic_stats: Dict[str, Any]
    # query_food_safety 节点产出
    food_safety_info: Dict[str, Any]
    involves_food_safety: bool
    # generate_report 节点产出
    analysis_report: str
    improvement_report: str
    # check_negative 节点产出（Human-in-the-Loop）
    need_manager_review: bool
    negative_reviews: List[Dict[str, Any]]
    reply_drafts: List[Dict[str, Any]]
    # save_analysis 节点产出
    save_result: Dict[str, Any]
