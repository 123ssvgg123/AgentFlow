"""
    ReviewAgent 节点函数：agents/review/nodes.py

    与 AGENT 结构对齐：节点实现为「纯函数」——接收 state dict，返回增量 dict，
    由 graph.py 负责装配进 LangGraph 状态图（或降级顺序执行器）。

    节点清单（7 个，与需求文档 3.3.3 对应）：
        collect_reviews / classify_sentiment / extract_topics / query_food_safety
        generate_report / check_negative / save_analysis
"""
import asyncio
from collections import Counter
from datetime import date, timedelta
from typing import Any, Dict, List

from agents.base_agent import node
from agents.review.prompts import NEGATIVE_REPLY
from base.logger import logger
from memory.store import get_store
from tools.external_api import get_review_data, get_store_info
from tools.safety_knowledge import ALLERGEN_MANAGEMENT_GUIDE
from tools.llm_tools import llm_generate_improvement, llm_sentiment_analysis
from tools.mcp_tools import query_food_safety_law

# 报告重写次数上限：店长「要求修改」会形成 generate_report ↔ check_negative 回环，
# 必须设上限，否则 manager_request_revise 长期为真时报告会被无限重写。
_MAX_REPORT_REVISION = 1

# 关键主题分类（用于 extract_topics 的规则统计）
TOPIC_KEYWORDS = {
    "口味": ["好吃", "难吃", "味道", "口味", "太咸", "太油", "偏淡", "正宗", "麻", "辣"],
    "服务": ["服务", "态度", "服务员", "热情", "不耐烦", "差", "周到"],
    "环境": ["环境", "干净", "整洁", "吵", "装修", "氛围", "卫生"],
    "价格": ["价格", "贵", "便宜", "实惠", "性价比", "划算", "不值"],
    "食材": ["食材", "新鲜", "不新鲜", "变质", "异味", "品质"],
    "等位时间": ["等位", "排队", "等了", "多久", "上菜慢", "太慢"],
    "分量": ["分量", "份量", "少", "多", "量足"],
    "食品安全": ["异物", "头发", "苍蝇", "蟑螂", "拉肚子", "腹泻", "中毒", "过期", "发霉"],
}


# ==================== 节点 1：collect_reviews ====================

@node("从多平台（美团/大众点评/抖音）采集最新评价")
async def collect_reviews(state: Dict[str, Any]) -> Dict[str, Any]:
    logger.info("[ReviewAgent] 节点 1/7 collect_reviews —— 采集多平台评价")
    tenant_id = state.get("tenant_id", "")

    # 并行加载评价与门店信息
    reviews_task = get_review_data(tenant_id, limit=50,
                                   date_range=(date.today() - timedelta(days=7), date.today()))
    store_task = get_store_info(tenant_id)
    reviews_data, store_info = await asyncio.gather(reviews_task, store_task, return_exceptions=True)

    if isinstance(reviews_data, Exception):
        logger.warning(f"[ReviewAgent] 评价采集失败: {reviews_data}")
        state.setdefault("degraded_tools", []).append("get_review_data")
        reviews = []
    else:
        reviews = reviews_data.get("reviews", [])
        if reviews_data.get("degraded"):
            state.setdefault("degraded_tools", []).append("get_review_data")

    if isinstance(store_info, Exception):
        store_info = {"store_id": tenant_id, "store_name": "本门店"}

    logger.info(f"[ReviewAgent] 采集到 {len(reviews)} 条评价")
    return {
        "reviews": reviews,
        "review_count": len(reviews),
        "store_info": store_info,
        "current_stage": "reviews_collected",
    }


# ==================== 节点 2：classify_sentiment ====================

@node("对评价进行情感分类（好评/中评/差评 + 情感强度）")
async def classify_sentiment(state: Dict[str, Any]) -> Dict[str, Any]:
    logger.info("[ReviewAgent] 节点 2/7 classify_sentiment —— 情感分析")
    reviews = state.get("reviews", [])

    # 并行分析（每批最多 20 条，控制并发与成本）
    tasks = [llm_sentiment_analysis(r.get("content", ""), r.get("platform", "unknown"))
             for r in reviews[:20]]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    sentiment_results = []
    for review, res in zip(reviews[:20], results):
        if isinstance(res, Exception):
            logger.warning(f"[ReviewAgent] 情感分析失败: {res}")
            res = {"sentiment": "neutral", "score": 0.0, "degraded": True}
        if res.get("degraded"):
            state.setdefault("degraded_tools", []).append("llm_sentiment_analysis")

        merged = {
            **review,
            "sentiment": res.get("sentiment", "neutral"),
            "sentiment_score": res.get("score", 0.0),
            "emotions": res.get("emotions", []),
            "is_food_safety": res.get("is_food_safety", False),
            "severity": res.get("severity", "low"),
            "keywords": res.get("keywords", []),
        }
        sentiment_results.append(merged)

    # 统计
    counter = Counter(r["sentiment"] for r in sentiment_results)
    total = len(sentiment_results) or 1
    stats = {
        "total": len(sentiment_results),
        "positive": counter.get("positive", 0),
        "neutral": counter.get("neutral", 0),
        "negative": counter.get("negative", 0),
        "positive_ratio": round(counter.get("positive", 0) / total, 3),
        "negative_ratio": round(counter.get("negative", 0) / total, 3),
        "avg_score": round(
            sum(r["sentiment_score"] for r in sentiment_results) / total, 2),
        "food_safety_issues": sum(1 for r in sentiment_results if r["is_food_safety"]),
    }

    logger.info(f"[ReviewAgent] 情感统计：好评 {stats['positive']} / 中评 {stats['neutral']} "
                f"/ 差评 {stats['negative']}，食品安全问题 {stats['food_safety_issues']} 条")

    return {
        "sentiment_results": sentiment_results,
        "sentiment_stats": stats,
        "current_stage": "sentiment_classified",
    }


# ==================== 节点 3：extract_topics ====================

@node("提取评价关键主题（口味/服务/环境/价格/食材/等位时间）")
async def extract_topics(state: Dict[str, Any]) -> Dict[str, Any]:
    logger.info("[ReviewAgent] 节点 3/7 extract_topics —— 提取关键主题")
    results = state.get("sentiment_results", [])

    topic_counter = Counter()
    negative_topic_counter = Counter()
    food_safety_related = False

    for review in results:
        content = review.get("content", "")
        matched = []
        for topic, keywords in TOPIC_KEYWORDS.items():
            if any(kw in content for kw in keywords):
                matched.append(topic)
                topic_counter[topic] += 1
                if review.get("sentiment") == "negative":
                    negative_topic_counter[topic] += 1
                if topic == "食品安全":
                    food_safety_related = True
        review["topics"] = matched
        if review.get("is_food_safety"):
            food_safety_related = True

    # 排名
    top_positive_topics = [{"topic": t, "count": c} for t, c in topic_counter.most_common()]
    top_negative_topics = [{"topic": t, "count": c} for t, c in negative_topic_counter.most_common()]

    involves_fs = food_safety_related or state.get("sentiment_stats", {}).get("food_safety_issues", 0) > 0
    if involves_fs:
        logger.warning("[ReviewAgent] 评价涉及食品安全问题，将联动内置食安法规库")

    return {
        "topic_stats": {
            "all_topics": top_positive_topics,
            "top_negative_topics": top_negative_topics,
            "top_positive_topics": top_positive_topics[:5],
        },
        "involves_food_safety": involves_fs,
        "current_stage": "topics_extracted",
    }


# ==================== 节点 4：query_food_safety（查询内置食安法规） ====================

@node("如涉及食品安全问题，查询相关法规和处理流程")
async def query_food_safety(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    调用 query_food_safety_law 工具，查询 FoodAgent 内置食品安全法规库。
    """
    logger.info("[ReviewAgent] 节点 4/7 query_food_safety —— 查询食品安全法规")
    negative_topics = state.get("topic_stats", {}).get("top_negative_topics", [])
    results = state.get("sentiment_results", [])

    # 构造查询：结合负面主题与食品安全关键词
    fs_reviews = [r.get("content", "") for r in results if r.get("is_food_safety")]
    query_parts = ["顾客评价中的食品安全问题处理"]
    if fs_reviews:
        query_parts.append(fs_reviews[0][:50])
    for topic in negative_topics[:3]:
        query_parts.append(topic.get("topic", ""))
    query = " ".join(query_parts)

    law_result = await query_food_safety_law(query)
    if isinstance(law_result, Exception):
        logger.warning(f"[ReviewAgent] 法规查询失败: {law_result}")
        law_result = {"laws": [], "answer": "法规知识库暂时不可用"}
        state.setdefault("degraded_tools", []).append("query_food_safety_law")
    elif law_result.get("degraded"):
        state.setdefault("degraded_tools", []).append("query_food_safety_law")

    # 附加过敏原管理规范（内部合规指引）
    law_result["allergen_management"] = ALLERGEN_MANAGEMENT_GUIDE
    logger.info(f"[ReviewAgent] 法规查询完成，来源：{law_result.get('source_system', 'unknown')}，"
                f"命中 {len(law_result.get('laws', []))} 条")

    return {
        "food_safety_info": law_result,
        "current_stage": "food_safety_queried",
    }


# ==================== 节点 5：generate_report ====================

@node("生成评价分析报告（好评率趋势+关键问题排名+改进建议）")
async def generate_report(state: Dict[str, Any]) -> Dict[str, Any]:
    logger.info("[ReviewAgent] 节点 5/7 generate_report —— 生成分析报告")
    stats = state.get("sentiment_stats", {})
    topics = state.get("topic_stats", {})
    fs_info = state.get("food_safety_info", {})
    store_info = state.get("store_info", {})

    # 改进建议：调 LLM 生成（失败降级为模板）
    analysis_data = {
        "好评率": stats.get("positive_ratio", 0),
        "差评占比": stats.get("negative_ratio", 0),
        "平均情感分": stats.get("avg_score", 0),
        "食品安全问题数": stats.get("food_safety_issues", 0),
        "关键负面主题": topics.get("top_negative_topics", []),
        "法规依据": fs_info.get("answer", "")[:500] if fs_info else "",
    }
    improvement = await llm_generate_improvement(analysis_data, store_info)
    if improvement.get("degraded"):
        state.setdefault("degraded_tools", []).append("llm_generate_improvement")

    report = improvement.get("report", "")

    # 前置拼装数据概览（保证报告首屏有量化数据）
    overview = [
        f"## {store_info.get('store_name', '本门店')} 评价分析报告",
        "",
        "### 一、数据概览",
        f"- 分析评价数：**{stats.get('total', 0)}** 条（近 7 天）",
        f"- 好评率：**{stats.get('positive_ratio', 0):.1%}**"
        f"（好评 {stats.get('positive', 0)} / 中评 {stats.get('neutral', 0)}"
        f" / 差评 {stats.get('negative', 0)}）",
        f"- 平均情感分：**{stats.get('avg_score', 0)}**",
    ]
    if stats.get("food_safety_issues", 0) > 0:
        overview.append(f"- 食品安全类问题：**{stats['food_safety_issues']}** 条（需重点处理）")

    negative_topics = topics.get("top_negative_topics", [])
    if negative_topics:
        overview += ["", "### 二、关键问题排名"]
        for i, t in enumerate(negative_topics[:5], 1):
            overview.append(f"{i}. **{t['topic']}** —— 负面提及 {t['count']} 次")

    analysis_report = "\n".join(overview)
    full_report = analysis_report + "\n\n" + report

    return {
        "analysis_report": analysis_report,
        "improvement_report": report,
        "full_report": full_report,
        "current_stage": "report_generated",
    }


# ==================== 节点 6：check_negative（Human-in-the-Loop） ====================

@node("判断是否有严重差评需要店长确认回复")
async def check_negative(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    需求文档 3.3.3 / Q6：
        差评 + 涉及食品安全/服务态度 → 触发 interrupt，等店长审核回复。
        店长确认 → 继续 save_analysis；要求修改 → 回到 generate_report。
    """
    logger.info("[ReviewAgent] 节点 6/7 check_negative —— 判断严重差评")
    results = state.get("sentiment_results", [])
    store_info = state.get("store_info", {})

    # 筛选需要人工确认的差评：负面 + (食品安全 或 严重度 high)
    negative_reviews = [
        r for r in results
        if r.get("sentiment") == "negative"
        and (r.get("is_food_safety") or r.get("severity") == "high"
             or r.get("sentiment_score", 0) < -0.6)
    ]

    # 草拟回复（供店长审核）
    reply_drafts = []
    for review in negative_reviews[:5]:
        draft = await _draft_reply(review, store_info, state)
        reply_drafts.append({
            "review_id": review.get("review_id"),
            "platform": review.get("platform"),
            "content": review.get("content"),
            "severity": review.get("severity"),
            "is_food_safety": review.get("is_food_safety"),
            "suggested_reply": draft,
            "status": "pending_manager_review",
        })

    need_review = len(negative_reviews) > 0

    # 若店长已确认（API 传入），不再中断
    if state.get("manager_confirmed"):
        logger.info("[ReviewAgent] 店长已确认差评回复，继续保存")
        need_review = False

    # 店长要求修改回复 → 标记重生成，条件边据此回到 generate_report
    revise_requested = bool(state.get("manager_request_revise"))
    if revise_requested:
        logger.warning("[ReviewAgent] 店长要求修改回复，回到 generate_report 重新生成")
        need_review = False   # 不重复中断，交给 generate_report 重写

    # Pipeline 模式：不中断，仅标记待确认
    pipeline_mode = bool(state.get("pipeline_mode"))

    if need_review:
        logger.warning(f"[ReviewAgent][Human-in-the-Loop] {len(negative_reviews)} 条严重差评"
                       f"需店长确认回复")
        if pipeline_mode:
            logger.info("[ReviewAgent] Pipeline 模式：跳过 interrupt，仅标记待确认")
        else:
            _try_interrupt({
                "type": "review_reply_confirm",
                "message": f"有 {len(negative_reviews)} 条严重差评需要您确认回复",
                "drafts": reply_drafts,
            })

    # 报告重写计数：条件边据此判断是否还允许「要求修改」再回 generate_report 一次。
    # 没有它，补上 generate_report → check_negative 出边后，只要
    # manager_request_revise 为真就会无限重写报告（直到 recursion_limit 抛错）。
    revision_count = int(state.get("report_revision_count", 0))
    if revise_requested:
        revision_count += 1

    return {
        "need_manager_review": need_review,
        "manager_request_revise": revise_requested,
        "report_revision_count": revision_count,
        "negative_reviews": negative_reviews,
        "reply_drafts": reply_drafts,
        "current_stage": "negative_checked",
    }


async def _draft_reply(review: Dict[str, Any], store_info: Dict[str, Any],
                       state: Dict[str, Any]) -> str:
    """起草差评回复（LLM 优先，规则模板兜底）"""
    try:
        from tools.llm_client import llm_client
        return await llm_client.chat(
            [{"role": "user", "content": NEGATIVE_REPLY.format(
                review_text=review.get("content", ""),
                sentiment=review.get("sentiment", "negative"),
                store_info=store_info)}],
            temperature=0.6, max_retries=0,
        )
    except Exception as exc:  # noqa: BLE001
        logger.info(f"[ReviewAgent] 回复草拟降级为模板: {exc}")
        # 规则模板兜底
        if review.get("is_food_safety"):
            return (f"非常抱歉给您带来不好的用餐体验。您反馈的问题我们高度重视，"
                    f"已第一时间启动内部核查，并将严格按食品安全规范整改。"
                    f"感谢您的监督，我们会主动与您联系妥善处理。")
        return (f"感谢您的反馈，对本次用餐体验不佳我们深表歉意。"
                f"我们已将您提到的「{review.get('topics', ['问题'])[0] if review.get('topics') else '问题'}」"
                f"反馈至门店负责人并安排改进，期待有机会为您提供更好的服务。")


def _try_interrupt(payload: Dict[str, Any]):
    """尝试调用 LangGraph interrupt（未运行时静默跳过）"""
    try:
        from langgraph.types import interrupt
        interrupt(payload)
    except ImportError:
        logger.info("[ReviewAgent] langgraph 未安装，中断信息由 API 层处理")
    except Exception as exc:  # noqa: BLE001
        logger.info(f"[ReviewAgent] 触发 interrupt 信号: {type(exc).__name__}")


# ==================== 节点 7：save_analysis ====================

@node("保存分析结果，同步回复到各评价平台")
async def save_analysis(state: Dict[str, Any]) -> Dict[str, Any]:
    logger.info("[ReviewAgent] 节点 7/7 save_analysis —— 保存分析结果")
    tenant_id = state.get("tenant_id", "")
    results = state.get("sentiment_results", [])
    replies = {d.get("review_id"): d.get("suggested_reply") for d in state.get("reply_drafts", [])}

    # 构造入库记录
    items = []
    for review in results:
        items.append({
            "review_id": review.get("review_id", ""),
            "platform": review.get("platform", ""),
            "rating": review.get("rating", 0),
            "sentiment": review.get("sentiment", "neutral"),
            "sentiment_score": review.get("sentiment_score", 0),
            "topics": review.get("topics", []),
            "summary": review.get("content", "")[:100],
            "suggested_reply": replies.get(review.get("review_id"), ""),
            "reply_status": "pending" if review.get("review_id") in replies else "no_reply_needed",
            "review_date": review.get("date") or date.today(),
        })

    saved = 0
    try:
        store = await get_store()
        saved = await store.save_review_analysis(tenant_id, items, state.get("user_id", ""))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ReviewAgent] 评价分析入库失败: {exc}")
        state.setdefault("errors", []).append(f"入库失败: {exc}")

    return {
        "save_result": {"saved": saved, "replies_ready": len(replies)},
        "current_stage": "saved",
    }
