"""
    MCP 工具（4 个）：tools/mcp_tools.py

    对应需求文档 3.4「MCP 工具」清单：
        search_menu_knowledge   菜品知识库检索（本地菜品库关键词检索）      → OrderAgent
        query_food_safety_law   食品安全法规知识库查询（内置法规库）        → ReviewAgent / SafetyAgent
        query_nutrition         菜品营养成分查询（内置营养表）              → OrderAgent / SafetyAgent
        web_search              联网搜索（餐饮趋势/竞品/反馈热点）         → ReviewAgent / SafetyAgent

    数据来源说明：全部基于 FoodAgent 内置知识库（tools/safety_knowledge.py、
        tools/menu_data.py），无需外部服务，保证自包含离线可用。

    异常兜底（对应 3.4.1 ④「MCP 工具」）：
        ① 知识库检索失败 → 降级为关键词搜索
        ② 联网搜索失败 → 返回缓存结果（TTL 24 小时）
        ③ 全部失败 → 返回内置静态知识 + 友好提示
"""
# 导入异步与缓存工具
import asyncio
import time
from typing import Any, Dict, List, Optional

# 导入 HTTP 客户端（联网搜索用）
import httpx

from base.config import config
from base.logger import logger


# ==================================================================
# MCP 工具 1：search_menu_knowledge —— 菜品知识库检索
# ==================================================================

async def search_menu_knowledge(query: str, top_k: int = 5,
                                filters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """
    在菜品知识库中检索（按口味/食材/做法匹配）。

    实现：本地菜品库关键词搜索（tools/menu_data.py），自包含离线可用。

    参数：
        query:   检索语句
        top_k:   返回条数
        filters: 过滤条件 {category, is_vegetarian, max_price, exclude_allergens}
    返回：
        菜品列表 [{dish_name, category, price, allergens, calories, ...}]
    """
    filters = filters or {}
    logger.info(f"[search_menu_knowledge] 检索「{query}」top_k={top_k} filters={filters}")

    # 本地菜品库关键词搜索
    return _keyword_search_menu(query, top_k, filters)


def _keyword_search_menu(query: str, top_k: int, filters: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    关键词搜索降级版：在内置菜品库中按名称/类别/口味标签匹配。
    对应 3.4.1 ④「检索失败时降级为关键词搜索」。

    参考 AGENT 的「召回 → 精排」两阶段：关键词只负责**宽召回**（3 倍候选），
    真正的排序交给 tools/reranker.py 的轻量精排，避免「命中一个字就排第一」。
    """
    from tools.menu_data import MENU_DB
    from tools.reranker import rerank_dicts

    keywords = [k for k in str(query).replace("，", " ").replace(",", " ").split() if k]
    scored = []
    for dish in MENU_DB:
        score = 0
        text = f"{dish['dish_name']} {dish.get('category','')} {' '.join(dish.get('tags', []))}"
        for kw in keywords:
            if kw and kw in text:
                score += 3
        # 过滤条件
        if filters.get("category") and dish.get("category") != filters["category"]:
            continue
        if filters.get("is_vegetarian") and not dish.get("is_vegetarian"):
            continue
        if filters.get("max_price") and dish.get("price", 0) > filters["max_price"]:
            continue
        if filters.get("exclude_allergens"):
            if any(a in dish.get("allergens", []) for a in filters["exclude_allergens"]):
                continue
        # 无关键词命中时给基础分（保证查询"招牌菜"能返回结果）
        scored.append((score + float(dish.get("sales", 0)) / 1000.0, dish))

    scored.sort(key=lambda x: x[0], reverse=True)
    recall = [dict(d, source="local_keyword_db") for _, d in scored[: max(top_k * 3, top_k)]]
    results, confidence = rerank_dicts(
        query, recall, top_k=top_k, text_keys=("dish_name", "category", "tags"),
    )
    logger.info(f"[search_menu_knowledge] 召回 {len(recall)} → 精排 {len(results)} 道菜"
                f"（Top-1 相似度 {confidence:.2f}）")
    return results


# ==================================================================
# MCP 工具 2：query_food_safety_law —— 食品安全法规查询
# ==================================================================

async def query_food_safety_law(query: str) -> Dict[str, Any]:
    """
    查询食品安全法规知识库（食品安全法/过敏原标识规范等）。

    实现：基于 FoodAgent 内置法规库（tools/safety_knowledge.py）关键词检索，
        自包含离线可用。
    """
    logger.info(f"[query_food_safety_law] 查询法规：{query}")
    return _builtin_law_lookup(query)


def _builtin_law_lookup(query: str) -> Dict[str, Any]:
    """
    内置法条库检索（双向关键词匹配），离线可用。

    参考 AGENT 的「召回 → 精排 → 置信度」：关键词只做召回，随后用
    tools/reranker.py 精排；置信度由「命中条数 + 精排分」共同决定，
    供 SafetyAgent 判断要不要走联网兜底。
    """
    from tools.reranker import rerank_dicts
    from tools.safety_knowledge import FOOD_SAFETY_LAWS

    q_kws = _split_keywords(query)
    hits = []
    for law in FOOD_SAFETY_LAWS:
        text = f"{law['title']} {law['content']}"
        # 双向匹配：查询词出现在条文里，或条文关键词出现在查询里（后者覆盖「留样」「冷藏」等无标点中文句）
        if (any(kw in text for kw in q_kws)
                or any(kw in query for kw in law.get('keywords', []))
                or not query):
            hits.append(law)

    ranked, rerank_score = rerank_dicts(
        query, hits, top_k=5, text_keys=("title", "content", "keywords"),
    )
    # 置信度：命中条数（覆盖面）+ 精排 Top-1（相关度）
    confidence = min(1.0, 0.55 + 0.10 * len(ranked) + 0.15 * rerank_score)

    return {
        "laws": ranked,
        "confidence": round(confidence, 4),
        "answer": "（内置知识库）相关食品安全法规条款已为你列出，建议以正式标准文本为准。",
        "source_system": "builtin",
        "degraded": False,
    }


# ==================================================================
# MCP 工具 3：query_nutrition —— 菜品营养成分查询
# ==================================================================

async def query_nutrition(dish_name: str, serving_size: str = "1份") -> Dict[str, Any]:
    """
    查询菜品营养成分（热量/蛋白质/脂肪/碳水化合物/钠等）。

    实现：基于 FoodAgent 内置营养表（tools/menu_data.py）+ 按菜品类别估算，
        自包含离线可用。
    """
    logger.info(f"[query_nutrition] 查询「{dish_name}」{serving_size} 的营养成分")
    return _builtin_nutrition(dish_name, serving_size)


def _builtin_nutrition(dish_name: str, serving_size: str) -> Dict[str, Any]:
    """内置营养表降级查询：命中直接返回，未命中按菜品类别估算"""
    from tools.menu_data import NUTRITION_DB, estimate_nutrition

    if dish_name in NUTRITION_DB:
        data = dict(NUTRITION_DB[dish_name])
        note = "内置营养表"
    else:
        data = estimate_nutrition(dish_name)
        note = "未收录，按菜品类别估算"

    # 健康建议
    advice = []
    if data.get("calories", 0) > 500:
        advice.append("热量偏高，建议适量食用或搭配清淡菜品")
    if data.get("sodium", 0) > 800:
        advice.append("钠含量偏高，高血压人群需注意")
    if data.get("fat", 0) > 25:
        advice.append("脂肪含量较高，减脂人群建议控制")
    if not advice:
        advice.append("营养结构较为均衡")

    return {
        "dish_name": dish_name,
        "serving_size": serving_size,
        "nutrition": data,
        "advice": advice,
        "note": note,
        "source_system": "builtin",
        "degraded": False,
    }


# ==================================================================
# MCP 工具 4：web_search —— 联网搜索
# ==================================================================

# 搜索结果缓存：{query: (timestamp, results)}
_SEARCH_CACHE: Dict[str, tuple] = {}


async def web_search(query: str, max_results: int = 5) -> Dict[str, Any]:
    """
    联网搜索（查餐饮趋势、竞品动态、顾客反馈热点等）。

    异常兜底（对应 3.4.1 ④）：
        ① 请求失败 → 重试 1 次
        ② 仍失败 → 返回缓存结果（TTL 24 小时）
        ③ 无缓存 → 返回友好提示 + 空结果（不阻塞流程）
    """
    logger.info(f"[web_search] 搜索：{query}（max={max_results}）")

    cache_key = f"{query}::{max_results}"
    # 命中未过期缓存直接返回
    if cache_key in _SEARCH_CACHE:
        ts, cached = _SEARCH_CACHE[cache_key]
        if time.time() - ts < config.WEB_SEARCH_CACHE_TTL:
            return {**cached, "from_cache": True}

    for attempt in range(2):  # 重试 1 次
        try:
            results = await _do_search(query, max_results)
            payload = {
                "query": query,
                "results": results,
                "from_cache": False,
                "degraded": False,
            }
            _SEARCH_CACHE[cache_key] = (time.time(), payload)
            return payload
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[web_search] 第 {attempt + 1} 次失败: {exc}")
            if attempt == 0:
                await asyncio.sleep(0.5)

    # 降级：过期缓存或空结果
    if cache_key in _SEARCH_CACHE:
        _, cached = _SEARCH_CACHE[cache_key]
        return {**cached, "from_cache": True, "degraded": True,
                "degrade_reason": "实时搜索失败，返回缓存结果"}

    return {
        "query": query,
        "results": [],
        "from_cache": False,
        "degraded": True,
        "degrade_reason": "联网搜索不可用，暂无结果",
        "message": "当前网络搜索服务不可用，建议稍后重试或查阅内部知识库。",
    }


async def _do_search(query: str, max_results: int) -> List[Dict[str, Any]]:
    """
    实际搜索实现。
    说明：本项目不绑定特定搜索服务商（可由架构师在 Spec 中锁定）。
        此处使用 DuckDuckGo 的公开 HTML 端点做示例实现（无需 API Key）；
        如需接入商业搜索 API，只需替换本函数实现，接口签名不变。
    """
    url = "https://html.duckduckgo.com/html/"
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        resp = await client.post(url, data={"q": query},
                                 headers={"User-Agent": "Mozilla/5.0 (compatible; FoodAgent/1.0)"})
        resp.raise_for_status()
        # 轻量解析：抽取结果标题与链接（避免引入 bs4 依赖）
        import re as _re
        html = resp.text
        titles = _re.findall(r'class="result__a"[^>]*>(.*?)</a>', html, _re.DOTALL)
        links = _re.findall(r'class="result__url"[^>]*>\s*(.*?)\s*</a>', html, _re.DOTALL)

    results = []
    for i, title in enumerate(titles[:max_results]):
        clean_title = _re.sub(r"<[^>]+>", "", title).strip()
        link = links[i].strip() if i < len(links) else ""
        results.append({"title": clean_title, "url": link, "snippet": ""})
    return results


# ==================================================================
# 内部工具函数
# ==================================================================

def _split_keywords(text: str) -> List[str]:
    """把查询拆成关键词（用于内置库的粗匹配）"""
    import re as _re
    return [w for w in _re.split(r"[\s，,。？?、；;]+", text or "") if len(w) >= 2]


# ==================== 工具注册表 ====================
MCP_TOOLS = {
    "search_menu_knowledge": search_menu_knowledge,
    "query_food_safety_law": query_food_safety_law,
    "query_nutrition": query_nutrition,
    "web_search": web_search,
}
