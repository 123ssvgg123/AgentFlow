"""
    LLM 工具（4 个）：tools/llm_tools.py

    对应需求文档 3.4「LLM 工具」清单：
        llm_classify_intent       解析顾客点餐需求（人数/口味/过敏原/预算）  → OrderAgent
        llm_recommend_dishes      根据需求智能推荐菜品组合                    → OrderAgent
        llm_sentiment_analysis    评价情感分析（好评/中评/差评 + 强度）       → ReviewAgent
        llm_generate_improvement  根据分析结果生成改进建议报告                → ReviewAgent

    「为什么 LLM 工具用 LLM？」
        意图识别、菜品推荐、情感分析、改进建议——都需要「理解」自然语言。
        规则搞不定"我们几个人想吃点清淡的，有两位不吃辣，预算人均 100 左右"这类模糊需求。

    异常兜底（对应 3.4.1 ④）：
        主模型不可用 / 超时 → 降级为规则引擎（规则版同样返回结构化结果，标注 degraded=True）
"""
# 导入正则（规则降级分支使用）
import re
# 导入类型标注
from typing import Any, Dict, List, Optional

from base.config import config
from base.exceptions import LLMUnavailableError
from base.logger import logger
# LLM 客户端与提示词模板
from prompts.templates import Prompts
from tools.llm_client import llm_client


# ==================================================================
# LLM 工具 1：llm_classify_intent —— 解析顾客点餐需求
# ==================================================================

async def llm_classify_intent(order_text: str, context: str = "") -> Dict[str, Any]:
    """
    用 LLM 识别顾客点餐意图，输出结构化需求。

    参数：
        order_text: 顾客原始输入文本
        context:    上下文（门店偏好、历史对话等），可为空
    返回：
        {guest_count, taste_preferences, dietary_restrictions, allergens,
         budget_per_person, budget_total, occasion, special_requests, mentioned_dishes}
    """
    logger.info(f"[llm_classify_intent] 解析需求: {order_text[:60]}")
    try:
        prompt = Prompts.PARSE_ORDER.format(order_text=order_text, context=context or "无")
        result = await llm_client.chat_json(
            [
                {"role": "system", "content": "你是点餐需求解析专家，只输出 JSON。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            default=None,
        )
        if isinstance(result, dict):
            result.setdefault("degraded", False)
            return _normalize_needs(result)
        raise LLMUnavailableError("LLM 返回结构非法", tool="llm_classify_intent")
    except LLMUnavailableError as exc:
        logger.warning(f"[llm_classify_intent] 降级为规则解析: {exc.message}")
        return _rule_parse_order(order_text)


def _normalize_needs(data: Dict[str, Any]) -> Dict[str, Any]:
    """规整 LLM 返回字段，保证类型与默认值稳定（防止 LLM 返回字符串数字等）"""
    def _as_list(v: Any) -> List[str]:
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        if isinstance(v, str) and v.strip():
            return [x.strip() for x in re.split(r"[、,，/;；]", v) if x.strip()]
        return []

    def _as_num(v: Any, default: float = 0) -> float:
        try:
            return float(re.sub(r"[^\d.]", "", str(v)) or default)
        except (ValueError, TypeError):
            return default

    return {
        "guest_count": int(_as_num(data.get("guest_count", 0))),
        "taste_preferences": _as_list(data.get("taste_preferences")),
        "dietary_restrictions": _as_list(data.get("dietary_restrictions")),
        "allergens": _as_list(data.get("allergens")),
        "budget_per_person": _as_num(data.get("budget_per_person", 0)),
        "budget_total": _as_num(data.get("budget_total", 0)),
        "occasion": str(data.get("occasion", "") or "日常用餐"),
        "special_requests": _as_list(data.get("special_requests")),
        "mentioned_dishes": _as_list(data.get("mentioned_dishes")),
        "degraded": data.get("degraded", False),
    }


def _rule_parse_order(order_text: str) -> Dict[str, Any]:
    """
    规则降级版需求解析：用正则抽取人数、预算、过敏原、饮食限制。
    虽不如 LLM 精细，但保证 LLM 不可用时系统仍然可用（零模型依赖）。
    """
    text = order_text or ""
    # 人数：如「10人」「两位」「3 个人」
    guest_count = 0
    m = re.search(r"(\d+)\s*(?:人|位|个)", text)
    if m:
        guest_count = int(m.group(1))
    else:
        cn_num = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
                  "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
        m = re.search(r"([一两二三四五六七八九十])\s*(?:人|位|个)", text)
        if m:
            guest_count = cn_num.get(m.group(1), 0)

    # 预算：如「人均 100」「预算 500」
    budget_per_person, budget_total = 0.0, 0.0
    m = re.search(r"人均\s*(\d+)", text)
    if m:
        budget_per_person = float(m.group(1))
    m = re.search(r"预算\s*(\d+)", text)
    if m:
        budget_total = float(m.group(1))

    # 过敏原：与预设过敏原库做包含匹配
    allergens = [a for a in config.ALLERGEN_LIBRARY if a in text]

    # 饮食限制
    restriction_keywords = {
        "素食": "素食", "吃素": "素食", "清真": "清真", "低卡": "低卡",
        "低脂": "低脂", "低糖": "低糖", "无麸质": "无麸质", "减肥": "低卡",
    }
    dietary = list({v for k, v in restriction_keywords.items() if k in text})

    # 口味
    taste_keywords = ["清淡", "麻辣", "香辣", "酸甜", "咸鲜", "辛辣", "不辣", "微辣"]
    tastes = [t for t in taste_keywords if t in text]

    # 点名菜品（粗略：书名号或引号内容）
    mentioned = re.findall(r'[《"]([^》"]+)[》"]', text)
    mentioned = [x for x in mentioned if x]

    logger.info(f"[规则降级] 解析结果 人数={guest_count} 过敏原={allergens} 限制={dietary}")
    return {
        "guest_count": guest_count,
        "taste_preferences": tastes,
        "dietary_restrictions": dietary,
        "allergens": allergens,
        "budget_per_person": budget_per_person,
        "budget_total": budget_total,
        "occasion": "日常用餐",
        "special_requests": [],
        "mentioned_dishes": mentioned,
        "degraded": True,
        "degrade_reason": "LLM 不可用，已降级为规则解析",
    }


# ==================================================================
# LLM 工具 2：llm_recommend_dishes —— 菜品组合推荐
# ==================================================================

async def llm_recommend_dishes(customer_needs: Dict[str, Any],
                               menu_data: List[Dict[str, Any]],
                               top_k: Optional[int] = None) -> Dict[str, Any]:
    """
    根据顾客需求智能推荐菜品组合。

    参数：
        customer_needs: llm_classify_intent 的输出
        menu_data:      候选菜品库（来自 search_menu_knowledge / 外部菜单接口）
        top_k:          推荐数量
    返回：
        {dishes: [...], combination_notes, vegetarian_arrangement}
    """
    top_k = top_k or config.DEFAULT_RECOMMEND_TOP_K
    logger.info(f"[llm_recommend_dishes] 候选菜品 {len(menu_data)} 道，目标推荐 {top_k} 道")

    # 前置硬过滤：含顾客过敏原的菜品直接排除（食品安全红线，不交给 LLM 判断）
    safe_menu = _filter_menu_by_allergens(menu_data, customer_needs.get("allergens", []))

    if not safe_menu:
        return {
            "dishes": [], "combination_notes": "候选菜品均含顾客声明的过敏原，无安全可选菜品",
            "vegetarian_arrangement": "", "degraded": False,
            "allergen_excluded": [],
        }

    try:
        prompt = Prompts.RECOMMEND_DISHES.format(
            customer_needs=_brief(customer_needs),
            menu_data=_brief(safe_menu),
            top_k=top_k,
        )
        result = await llm_client.chat_json(
            [
                {"role": "system", "content": "你是餐饮搭配顾问，只输出 JSON，禁止编造菜品库以外的菜品。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.4,
            default=None,
        )
        if isinstance(result, dict) and result.get("dishes"):
            # 二次校验：剔除 LLM 幻觉出的菜品（不在候选库中的一律丢弃）
            result["dishes"] = _validate_recommended(result["dishes"], safe_menu)
            result["degraded"] = False
            result.setdefault("combination_notes", "")
            result.setdefault("vegetarian_arrangement", "")
            return result
        raise LLMUnavailableError("LLM 未返回有效菜品", tool="llm_recommend_dishes")
    except LLMUnavailableError as exc:
        logger.warning(f"[llm_recommend_dishes] 降级为按销量/匹配度排序: {exc.message}")
        return _rule_recommend(safe_menu, customer_needs, top_k)


def _filter_menu_by_allergens(menu: List[Dict[str, Any]], allergens: List[str]) -> List[Dict[str, Any]]:
    """从候选菜单中剔除含顾客过敏原的菜品（宁可少推荐，绝不推荐风险菜）"""
    if not allergens:
        return list(menu)
    safe = []
    for dish in menu:
        dish_allergens = dish.get("allergens", []) or []
        if any(a in dish_allergens for a in allergens):
            logger.info(f"[过敏原过滤] 排除菜品「{dish.get('dish_name')}」——含 {allergens}")
            continue
        safe.append(dish)
    return safe


def _validate_recommended(dishes: List[Dict], menu: List[Dict]) -> List[Dict]:
    """校验 LLM 推荐结果：只保留确实存在于候选库中的菜品（防幻觉）"""
    name_set = {d.get("dish_name", "") for d in menu}
    valid = [d for d in dishes if d.get("dish_name") in name_set]
    dropped = len(dishes) - len(valid)
    if dropped > 0:
        logger.warning(f"[幻觉拦截] 丢弃 {dropped} 道不在菜品库中的推荐")
    return valid


def _rule_recommend(menu: List[Dict], needs: Dict[str, Any], top_k: int) -> Dict[str, Any]:
    """
    规则降级推荐：按「是否满足饮食限制 > 销量 > 价格适配」排序后取前 top_k。
    保证 LLM 不可用时仍能给出可用推荐。
    """
    restrictions = needs.get("dietary_restrictions", [])
    budget = needs.get("budget_per_person", 0) or 0
    guest = max(needs.get("guest_count", 0), 1)

    def score(dish: Dict[str, Any]) -> float:
        s = float(dish.get("sales", 0) or 0) / 100.0
        # 素食需求：素菜加权
        if "素食" in restrictions and dish.get("is_vegetarian"):
            s += 10
        if "低卡" in restrictions and (dish.get("calories") or 9999) <= 300:
            s += 6
        # 预算适配
        if budget and guest:
            per_dish_budget = (budget * guest) / max(top_k, 1)
            if dish.get("price", 0) <= per_dish_budget:
                s += 3
        return s

    ranked = sorted(menu, key=score, reverse=True)[:top_k]
    dishes = [{
        "dish_name": d.get("dish_name"),
        "category": d.get("category", "热菜"),
        "price": d.get("price", 0),
        "quantity": 1,
        "reason": "热销推荐" if not d.get("is_vegetarian") else "素食优选",
        "allergens": d.get("allergens", []),
        "is_vegetarian": d.get("is_vegetarian", False),
        "calories": d.get("calories", 0),
    } for d in ranked]

    return {
        "dishes": dishes,
        "combination_notes": "（降级模式）已按饮食限制与销量排序推荐，未做口味组合优化",
        "vegetarian_arrangement": "已单独标注素食菜品" if "素食" in restrictions else "",
        "degraded": True,
        "degrade_reason": "LLM 不可用，已降级为规则推荐",
    }


# ==================================================================
# LLM 工具 3：llm_sentiment_analysis —— 评价情感分析
# ==================================================================

async def llm_sentiment_analysis(review_text: str, platform: str = "unknown") -> Dict[str, Any]:
    """
    对顾客评价做情感分析。

    返回：{sentiment, score, emotions, keywords, is_food_safety, severity}
    """
    logger.info(f"[llm_sentiment_analysis] 平台={platform} 文本长度={len(review_text or '')}")
    try:
        prompt = Prompts.SENTIMENT_ANALYSIS.format(review_text=review_text, platform=platform)
        result = await llm_client.chat_json(
            [
                {"role": "system", "content": "你是餐饮评价分析专家，只输出 JSON。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            default=None,
        )
        if isinstance(result, dict) and result.get("sentiment"):
            result["degraded"] = False
            result.setdefault("score", 0.0)
            result.setdefault("emotions", [])
            result.setdefault("keywords", [])
            result.setdefault("is_food_safety", False)
            result.setdefault("severity", "low")
            return result
        raise LLMUnavailableError("LLM 未返回有效情感结果", tool="llm_sentiment_analysis")
    except LLMUnavailableError as exc:
        logger.warning(f"[llm_sentiment_analysis] 降级为词典情感分析: {exc.message}")
        return _rule_sentiment(review_text)


def _rule_sentiment(text: str) -> Dict[str, Any]:
    """
    词典法情感分析降级：
        餐饮语境特别处理——「一般般」「凑合」等中立词在餐饮评价中偏负面（需求文档 Q4 明确要求）。
    """
    text = text or ""
    # 餐饮语境负面词（含口语化表达）
    negative_words = ["难吃", "难以下咽", "一般般", "凑合", "还行", "就那样", "失望", "差评",
                      "太咸", "太油", "不新鲜", "变质", "异物", "拉肚子", "腹泻", "等太久",
                      "服务差", "态度差", "贵", "坑", "恶心", "投诉", "臭", "坏"]
    # 餐饮语境正面词
    positive_words = ["好吃", "美味", "推荐", "满意", "不错", "很棒", "赞", "喜欢",
                      "新鲜", "实惠", "热情", "干净", "划算", "回头", "必点", "惊艳"]
    # 食品安全关键词（触发合规流程）
    fs_keywords = ["异物", "变质", "不新鲜", "发霉", "苍蝇", "蟑螂", "头发", "拉肚子",
                   "腹泻", "中毒", "过期", "臭", "腐败"]

    neg_hits = [w for w in negative_words if w in text]
    pos_hits = [w for w in positive_words if w in text]
    fs_hits = [w for w in fs_keywords if w in text]

    # 情感判定：负面词权重高于正面（餐饮差评影响更大）
    neg_score = len(neg_hits) * 1.5
    pos_score = len(pos_hits) * 1.0

    if fs_hits or neg_score > pos_score:
        sentiment = "negative"
        score = -min(1.0, 0.3 + neg_score * 0.15)
    elif pos_score > neg_score:
        sentiment = "positive"
        score = min(1.0, 0.3 + pos_score * 0.15)
    else:
        sentiment = "neutral"
        score = 0.0

    # 严重度：有食品安全关键词 → high
    severity = "high" if fs_hits else ("medium" if sentiment == "negative" else "low")

    return {
        "sentiment": sentiment,
        "score": round(score, 2),
        "emotions": (["失望"] if sentiment == "negative" else ["满意"] if sentiment == "positive" else ["中性"]),
        "keywords": neg_hits + pos_hits,
        "is_food_safety": bool(fs_hits),
        "severity": severity,
        "degraded": True,
        "degrade_reason": "LLM 不可用，已降级为词典情感分析",
    }


# ==================================================================
# LLM 工具 4：llm_generate_improvement —— 改进建议生成
# ==================================================================

async def llm_generate_improvement(analysis_data: Dict[str, Any],
                                   store_info: Dict[str, Any]) -> Dict[str, Any]:
    """
    根据评价分析结果生成改进建议报告（Markdown 文本）。

    返回：{report: str, priority_actions: [...]}
    """
    logger.info("[llm_generate_improvement] 生成改进建议报告")
    try:
        prompt = Prompts.GENERATE_IMPROVEMENT.format(
            store_info=_brief(store_info),
            analysis_data=_brief(analysis_data),
        )
        report = await llm_client.chat(
            [
                {"role": "system", "content": "你是连锁餐饮运营顾问，输出专业、可落地的改进建议。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.5,
        )
        return {
            "report": report,
            "priority_actions": _extract_actions(report),
            "degraded": False,
        }
    except LLMUnavailableError as exc:
        logger.warning(f"[llm_generate_improvement] 降级为模板报告: {exc.message}")
        return _rule_improvement(analysis_data, store_info)


def _extract_actions(report: str) -> List[str]:
    """从报告中抽取行动项（以「-」或数字开头的行）"""
    actions = []
    for line in (report or "").splitlines():
        s = line.strip()
        if s.startswith(("- ", "* ")) or re.match(r"^\d+[.、]", s):
            actions.append(re.sub(r"^[-*\d.、\s]+", "", s))
    return actions[:8]


def _rule_improvement(analysis: Dict[str, Any], store_info: Dict[str, Any]) -> Dict[str, Any]:
    """规则降级改进建议：基于评价分析的结构化数据生成模板化报告"""
    store_name = store_info.get("store_name", "本门店")
    negative_ratio = analysis.get("negative_ratio", 0)
    top_topics = analysis.get("top_negative_topics", [])
    fs_issues = analysis.get("food_safety_issues", 0)

    lines = [
        f"# {store_name} 评价分析改进报告（降级模板）",
        "",
        "## 一、数据概览",
        f"- 差评占比：{negative_ratio:.1%}",
        f"- 食品安全类问题：{fs_issues} 条",
        "",
        "## 二、关键问题排名",
    ]
    if top_topics:
        for i, topic in enumerate(top_topics[:5], 1):
            name = topic.get("topic", "未知") if isinstance(topic, dict) else str(topic)
            count = topic.get("count", 0) if isinstance(topic, dict) else 0
            lines.append(f"{i}. {name}（提及 {count} 次）")
    else:
        lines.append("暂无足够数据")

    lines += [
        "",
        "## 三、改进建议",
        "- 针对高频问题，安排专人每日复盘并形成闭环记录",
        "- 加强员工培训，重点覆盖服务用语与出餐时效",
        "- 建立顾客反馈快速响应机制，差评 2 小时内响应",
    ]
    if fs_issues > 0:
        lines += [
            "",
            "## 四、食品安全整改（强制项）",
            "- 立即启动食品安全内部核查，按《食品安全法》要求留存记录",
            f"- 整改留痕日志保存 {config.COMPLIANCE_LOG_YEARS} 年备查",
        ]

    report = "\n".join(lines)
    return {
        "report": report,
        "priority_actions": [ln for ln in lines if ln.startswith("- ")],
        "degraded": True,
        "degrade_reason": "LLM 不可用，已降级为模板报告",
    }


# ==================================================================
# 内部工具函数
# ==================================================================

def _brief(data: Any, max_items: int = 30) -> str:
    """把结构化数据压缩为提示词友好的字符串（限制长度，控制 token 成本）"""
    import json as _json
    if isinstance(data, list):
        data = data[:max_items]
    text = _json.dumps(data, ensure_ascii=False)
    return text[:4000]


# ==================== 工具注册表（供 Agent 按名调用） ====================
LLM_TOOLS = {
    "llm_classify_intent": llm_classify_intent,
    "llm_recommend_dishes": llm_recommend_dishes,
    "llm_sentiment_analysis": llm_sentiment_analysis,
    "llm_generate_improvement": llm_generate_improvement,
}
