"""
    外部 API 封装（3 个）：tools/external_api.py

    对应需求文档 3.4「外部 API 封装」清单：
        get_order_detail            查询订单详情（对接甲方 POS 系统）        → OrderAgent / KitchenAgent
        get_ingredient_inventory    查询食材库存（对接食材管理系统）          → KitchenAgent
        get_review_data             采集多平台评价（对接美团/大众点评/抖音）  → ReviewAgent

    对接三步骤（需求文档 3.4.2 ③）：
        1. 写 Python 异步函数：用 httpx 异步调用甲方 API
        2. 字段映射 + 格式转换：甲方字段名（ord_id/dish_cd）→ LLM 友好字段（订单号/菜品名称）
        3. 在 LangGraph 节点函数内调用，供 LLM 通过 Function Call 触发

    鉴权：OAuth2.0 Bearer Token + X-Store-Key 门店签名

    异常兜底（对应 3.4.1 ④）：
        ① 重试 1 次 → ② 切换备用数据源（本地 Mock） → ③ 返回友好提示
        由于本环境无甲方真实 POS/KDS 系统，Mock 降级即为主数据源，
        但完整保留了真实调用的代码路径（配置 PARTNER_API_BASE 后即走真实接口）。
"""
# 导入异步与时间工具
import asyncio
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx

from base.config import config
from base.logger import logger

# ==================================================================
# 字段映射表（甲方字段名 → LLM 友好字段名）
# 需求文档 3.4.2：甲方字段名对 LLM 不友好，必须做映射
# ==================================================================
ORDER_STATUS_MAP = {0: "待确认", 1: "已确认", 2: "制作中", 3: "已完成", 4: "已取消"}
DISH_CATEGORY_MAP = {"01": "凉菜", "02": "热菜", "03": "汤品",
                     "04": "主食", "05": "甜品", "06": "饮品"}
PLATFORM_MAP = {"mt": "美团", "dp": "大众点评", "dy": "抖音"}

# 甲方 API 基础地址（未配置时走 Mock 兜底）
PARTNER_API_BASE = os.getenv("PARTNER_API_BASE", "").rstrip("/")
STORE_KEY = os.getenv("PARTNER_STORE_KEY", "mock_store_key")
ACCESS_TOKEN = os.getenv("PARTNER_ACCESS_TOKEN", "")


async def get_access_token() -> str:
    """
    获取 OAuth2.0 Bearer Token（对应需求文档示例中的 get_access_token）。
    真实环境：POST /oauth/token 换取；Mock 环境返回占位令牌。
    """
    if ACCESS_TOKEN:
        return ACCESS_TOKEN
    if not PARTNER_API_BASE:
        return "mock_token"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(f"{PARTNER_API_BASE}/oauth/token",
                                     data={"grant_type": "client_credentials", "store_key": STORE_KEY})
            resp.raise_for_status()
            return resp.json().get("access_token", "")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[鉴权] 获取 token 失败，降级为 Mock: {exc}")
        return "mock_token"


# ==================================================================
# 外部 API 1：get_order_detail —— 查询订单详情（对接 POS 系统）
# ==================================================================

async def get_order_detail(order_id: str) -> Dict[str, Any]:
    """
    查询订单详情（对接甲方 POS 系统）。

    甲方 API：GET {base}/v1/orders/{order_id}
    鉴权：OAuth2.0 Bearer Token + StoreKey 签名

    返回（字段已映射为中文友好名）：
        {订单号, 门店名称, 桌号, 人数, 下单时间, 状态, 菜品列表, 总金额, 会员等级, 特殊要求}
    """
    logger.info(f"[get_order_detail] 查询订单 {order_id}")

    # ① 优先走真实甲方 API
    if PARTNER_API_BASE:
        for attempt in range(2):
            try:
                token = await get_access_token()
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(
                        f"{PARTNER_API_BASE}/v1/orders/{order_id}",
                        headers={"Authorization": f"Bearer {token}", "X-Store-Key": STORE_KEY},
                    )
                    resp.raise_for_status()
                    return _map_order(resp.json())
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[get_order_detail] 第 {attempt + 1} 次失败: {exc}")
                if attempt == 0:
                    await asyncio.sleep(0.5)

    # ② 降级：本地 Mock（无甲方系统时的备用数据源）
    return _mock_order(order_id)


def _map_order(raw: Dict[str, Any]) -> Dict[str, Any]:
    """字段映射：甲方字段名 → LLM 友好字段名（需求文档示例的核心工作）"""
    return {
        "订单号": raw.get("ord_id", ""),
        "门店名称": raw.get("store_name", ""),
        "桌号": raw.get("table_no", ""),
        "人数": raw.get("guest_count", 0),
        "下单时间": raw.get("ord_time", ""),
        "状态": ORDER_STATUS_MAP.get(raw.get("ord_status", 0), "未知"),
        "菜品列表": [
            {
                "菜品名称": d.get("dish_nm", ""),
                "类别": DISH_CATEGORY_MAP.get(d.get("dish_cat", "02"), "其他"),
                "单价": d.get("unit_price", 0),
                "数量": d.get("qty", 1),
                "备注": d.get("remark", ""),
                "过敏原标注": d.get("allergen_tags", []),
            }
            for d in raw.get("dishes", [])
        ],
        "总金额": f"¥{raw.get('total_amt', 0)}",
        "会员等级": raw.get("member_level", "普通"),
        "特殊要求": raw.get("special_req", "无"),
        "degraded": False,
        "source_system": "partner_pos",
    }


def _mock_order(order_id: str) -> Dict[str, Any]:
    """Mock 订单（备用数据源）：模拟甲方 POS 返回结构"""
    from tools.menu_data import MENU_DB

    # 依据订单号哈希生成稳定的 Mock 数据（保证同一订单号多次查询结果一致）
    seed = sum(ord(c) for c in str(order_id))
    guest = seed % 8 + 2
    dishes = [MENU_DB[seed % len(MENU_DB)], MENU_DB[(seed + 3) % len(MENU_DB)]]
    total = sum(d["price"] * 2 for d in dishes)

    # 模拟「过敏原订单」：订单号含 "al" 时带过敏原
    allergen_tags = ["花生"] if "al" in str(order_id).lower() else []

    return {
        "订单号": order_id,
        "门店名称": "北京朝阳旗舰店",
        "桌号": f"A{seed % 20 + 1:02d}",
        "人数": guest,
        "下单时间": (datetime.now() - timedelta(minutes=seed % 20 + 3)).strftime("%Y-%m-%d %H:%M:%S"),
        "状态": "制作中",
        "菜品列表": [
            {
                "菜品名称": d["dish_name"],
                "类别": d["category"],
                "单价": d["price"],
                "数量": 1,
                "备注": "少油少盐" if seed % 3 == 0 else "",
                "过敏原标注": d.get("allergens", []),
            }
            for d in dishes
        ],
        "总金额": f"¥{total}",
        "会员等级": ["普通", "银卡", "金卡", "钻石"][seed % 4],
        "特殊要求": "花生过敏，请单独处理" if allergen_tags else "无",
        "allergens": allergen_tags,
        "degraded": True,
        "degrade_reason": "甲方 POS 系统未接入，使用 Mock 数据源",
        "source_system": "mock",
    }


async def list_pending_orders(store_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    """
    查询待处理订单列表（KitchenAgent 的 load_orders 节点使用）。
    真实环境应调 GET /v1/stores/{store_id}/orders?status=pending
    """
    logger.info(f"[list_pending_orders] 门店 {store_id} 待处理订单（限 {limit}）")

    if PARTNER_API_BASE:
        try:
            token = await get_access_token()
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{PARTNER_API_BASE}/v1/stores/{store_id}/orders",
                    params={"status": "pending", "limit": limit},
                    headers={"Authorization": f"Bearer {token}", "X-Store-Key": STORE_KEY},
                )
                resp.raise_for_status()
                return [_map_order(o) for o in resp.json().get("data", [])]
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[list_pending_orders] 失败，降级为 Mock: {exc}")

    return _mock_pending_orders(store_id, limit)


def _mock_pending_orders(store_id: str, limit: int) -> List[Dict[str, Any]]:
    """Mock 待处理订单列表（模拟高峰期排队场景）"""
    from tools.menu_data import MENU_DB

    orders = []
    for i in range(min(limit, 15)):
        seed = (sum(ord(c) for c in store_id) + i * 7)
        dish = MENU_DB[seed % len(MENU_DB)]
        cooked = 8 + (seed % 20)
        special = i % 4 == 0
        orders.append({
            "订单号": f"ORD{store_id[-3:]}{1000 + i}",
            "桌号": f"{'ABC'[i % 3]}{i + 1:02d}",
            "菜品数": seed % 4 + 1,
            "菜品列表": [{"菜品名称": dish["dish_name"], "数量": 1, "单价": dish["price"]}],
            "下单时间": (datetime.now() - timedelta(minutes=seed % 25 + 2)).isoformat(),
            "order_time": (datetime.now() - timedelta(minutes=seed % 25 + 2)).isoformat(),
            "estimated_minutes": cooked,
            "member_level": ["普通", "普通", "金卡", "钻石", "普通"][seed % 5],
            "allergens": ["花生"] if special else [],
            "special_requests": ["花生过敏"] if special else (["少油少盐"] if i % 3 == 0 else []),
            "degraded": True,
            "source_system": "mock",
        })
    return orders


# ==================================================================
# 外部 API 2：get_ingredient_inventory —— 查询食材库存
# ==================================================================

async def get_ingredient_inventory(store_id: str,
                                   ingredient_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    查询食材库存（对接甲方食材管理系统）。

    甲方 API：GET {base}/v1/stores/{store_id}/inventory
    返回：{食材名: 库存量} + 低库存预警列表
    """
    logger.info(f"[get_ingredient_inventory] 门店 {store_id} 库存查询 {ingredient_ids or '全部'}")

    if PARTNER_API_BASE:
        for attempt in range(2):
            try:
                token = await get_access_token()
                params = {"ingredients": ",".join(ingredient_ids)} if ingredient_ids else {}
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(
                        f"{PARTNER_API_BASE}/v1/stores/{store_id}/inventory",
                        params=params,
                        headers={"Authorization": f"Bearer {token}", "X-Store-Key": STORE_KEY},
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    return {
                        "inventory": data.get("inventory", {}),
                        "low_stock": data.get("low_stock", []),
                        "degraded": False,
                        "source_system": "partner_inventory",
                    }
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[get_ingredient_inventory] 第 {attempt + 1} 次失败: {exc}")
                if attempt == 0:
                    await asyncio.sleep(0.5)

    return _mock_inventory(store_id, ingredient_ids)


def _mock_inventory(store_id: str, ingredient_ids: Optional[List[str]]) -> Dict[str, Any]:
    """Mock 库存数据（备用数据源）"""
    base_inventory = {
        "鸡胸肉": 500, "花生": 50, "干辣椒": 200, "豆腐": 800, "牛肉末": 300,
        "鲈鱼": 12, "葱": 150, "姜": 120, "鸭肉": 30, "甜面酱": 80,
        "西兰花": 200, "木耳": 60, "胡萝卜": 300, "时令蔬菜": 400, "蒜": 100,
        "番茄": 350, "鸡蛋": 400, "大米": 2000, "芒果": 8, "乳制品": 100,
    }
    if ingredient_ids:
        inventory = {k: v for k, v in base_inventory.items() if k in ingredient_ids}
    else:
        inventory = base_inventory

    low_stock = [{"ingredient": k, "available": v, "note": "库存偏低，建议补货"}
                 for k, v in inventory.items() if v <= 30]

    return {
        "inventory": inventory,
        "low_stock": low_stock,
        "degraded": True,
        "degrade_reason": "食材管理系统未接入，使用 Mock 数据源",
        "source_system": "mock",
    }


# ==================================================================
# 外部 API 3：get_review_data —— 采集多平台评价
# ==================================================================

async def get_review_data(store_id: str, platform: Optional[str] = None,
                          date_range: Optional[tuple] = None,
                          limit: int = 50) -> Dict[str, Any]:
    """
    采集多平台评价数据（对接美团/大众点评/抖音 API）。

    甲方 API：GET {base}/v1/stores/{store_id}/reviews?platform=xx&days=7
    返回：评价列表（已统一字段格式，标注来源平台）
    """
    logger.info(f"[get_review_data] 门店 {store_id} 平台={platform or '全部'} 条数={limit}")

    if PARTNER_API_BASE:
        try:
            token = await get_access_token()
            params: Dict[str, Any] = {"limit": limit}
            if platform:
                params["platform"] = platform
            if date_range:
                params["start"] = str(date_range[0])
                params["end"] = str(date_range[1])
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{PARTNER_API_BASE}/v1/stores/{store_id}/reviews",
                    params=params,
                    headers={"Authorization": f"Bearer {token}", "X-Store-Key": STORE_KEY},
                )
                resp.raise_for_status()
                reviews = [_map_review(r) for r in resp.json().get("data", [])]
                return {"reviews": reviews, "total": len(reviews),
                        "degraded": False, "source_system": "partner_review"}
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[get_review_data] 失败，降级为 Mock: {exc}")

    return _mock_reviews(store_id, platform, limit)


def _map_review(raw: Dict[str, Any]) -> Dict[str, Any]:
    """评价字段映射：各平台格式 → 统一格式（需求文档 Q5：统一映射为 5 分制）"""
    return {
        "review_id": raw.get("review_id", ""),
        "platform": PLATFORM_MAP.get(raw.get("platform", ""), raw.get("platform", "未知")),
        "rating": raw.get("rating", 5),
        "content": raw.get("content", ""),
        "date": raw.get("review_date", ""),
        "dish_mentioned": raw.get("dish_mentioned", []),
    }


def _mock_reviews(store_id: str, platform: Optional[str], limit: int) -> Dict[str, Any]:
    """
    Mock 评价数据：覆盖好评/中评/差评 + 食品安全问题，便于演示 ReviewAgent。

    修复：原先三家平台共用同一批样本，导致「跨平台对比」分析毫无意义。
        现在每个平台有各自独立的问题倾向：
            mt（美团）—— 偏向「出餐速度 / 分量」
            dp（大众点评）—— 偏向「口味 / 环境 / 卫生」
            dy（抖音）—— 偏向「服务态度 / 性价比 / 网红打卡」
    """
    platform_samples = {
        "mt": [
            ("招牌烤鸭太好吃了，皮脆肉嫩，服务也很热情，下次还来！", 5),
            ("菜品味道一般般，服务还行，等位等了 40 分钟有点久。", 3),
            ("宫保鸡丁有点咸了，花生倒是很香。整体还不错。", 4),
            ("上菜太慢了，等了快一个小时，菜都凉了。", 2),
            ("分量偏少，价格偏贵，感觉不太值。", 2),
            ("外卖包装很用心，汤一点没洒，送得也快。", 5),
            ("米饭有点夹生，希望改进一下。", 2),
            ("点了三个菜，只上齐两个，催了两次才补齐。", 2),
        ],
        "dp": [
            ("在汤里发现了一根头发，太恶心了，要求给个说法！", 1),
            ("环境干净整洁，性价比很高，推荐给朋友了。", 5),
            ("清蒸鲈鱼很新鲜，火候正好，厨师手艺不错。", 5),
            ("麻婆豆腐不够麻，口味偏淡，不是正宗的川菜。", 3),
            ("包间有股油烟味，通风做得不太好。", 3),
            ("菜品摆盘精致，适合拍照，口味也在线。", 5),
            ("桌椅有油渍没擦干净，卫生细节要注意。", 2),
            ("招牌菜名不副实，口味和宣传差得比较远。", 2),
        ],
        "dy": [
            ("服务员态度很差，问个菜都不耐烦，体验很差。", 1),
            ("杨枝甘露很好喝，甜度适中，芒果很新鲜。", 5),
            ("朋友聚餐来的，整体满意，就是停车不太方便。", 4),
            ("排队两小时才吃上，网红店名过其实了。", 2),
            ("团购套餐性价比很高，四个菜才 128。", 5),
            ("店员主动帮我们拍了合影，服务超预期。", 5),
            ("结账时发现多收了一套餐具费，沟通后才退。", 2),
            ("店里太吵了，说话都要喊，不太适合聊天。", 3),
        ],
    }

    all_samples = []
    for plat_key, samples in platform_samples.items():
        for content, rating in samples:
            all_samples.append((content, rating, plat_key))

    # 指定平台 → 只返回该平台样本；未指定 → 混合返回
    if platform:
        target = PLATFORM_MAP.get(platform, platform)
        picked = [(c, r, p) for c, r, p in all_samples if PLATFORM_MAP.get(p, p) == target]
    else:
        # 混合：三家平台轮流取，保证覆盖面
        picked = []
        cursors = {p: 0 for p in platform_samples}
        while len(picked) < 24 and any(cursors[p] < len(platform_samples[p]) for p in platform_samples):
            for p in platform_samples:
                if cursors[p] < len(platform_samples[p]):
                    c, r = platform_samples[p][cursors[p]]
                    picked.append((c, r, p))
                    cursors[p] += 1

    reviews = []
    for i, (content, rating, plat) in enumerate(picked[:limit]):
        reviews.append({
            "review_id": f"{store_id}_rev_{i:04d}",
            "platform": PLATFORM_MAP.get(plat, plat),
            "rating": rating,
            "content": content,
            "date": (datetime.now() - timedelta(days=i % 7)).strftime("%Y-%m-%d"),
            "dish_mentioned": [],
        })
    return {
        "reviews": reviews,
        "total": len(reviews),
        "degraded": True,
        "degrade_reason": "评价聚合平台未接入，使用 Mock 数据源",
        "source_system": "mock",
    }


# ==================================================================
# 外部 API 4（辅助，非需求 15 工具之一）：下发后厨 KDS
# 说明：sync_kitchen_display / sync_pos_order 在节点内被调用，
#      这里提供统一实现，避免节点内散落 HTTP 代码。
# ==================================================================

async def sync_kitchen_display(store_id: str, tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """下发任务到后厨 KDS 显示屏（对接 KDS 系统的 WebSocket + REST）"""
    logger.info(f"[sync_kitchen_display] 门店 {store_id} 下发 {len(tasks)} 条后厨任务")

    if PARTNER_API_BASE:
        try:
            token = await get_access_token()
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{PARTNER_API_BASE}/v1/stores/{store_id}/kds/tasks",
                    json={"tasks": tasks},
                    headers={"Authorization": f"Bearer {token}", "X-Store-Key": STORE_KEY},
                )
                resp.raise_for_status()
                return {"dispatched": True, "count": len(tasks),
                        "degraded": False, "source_system": "partner_kds"}
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[sync_kitchen_display] 失败，降级为 Mock: {exc}")

    return {
        "dispatched": True,
        "count": len(tasks),
        "task_ids": [f"KDS-{store_id[-3:]}-{i:03d}" for i in range(len(tasks))],
        "degraded": True,
        "degrade_reason": "KDS 系统未接入，已模拟下发",
        "source_system": "mock",
    }


async def sync_pos_order(store_id: str, order: Dict[str, Any]) -> Dict[str, Any]:
    """提交订单到 POS 系统"""
    logger.info(f"[sync_pos_order] 门店 {store_id} 提交订单 {order.get('订单号', '')}")

    if PARTNER_API_BASE:
        try:
            token = await get_access_token()
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{PARTNER_API_BASE}/v1/stores/{store_id}/orders",
                    json=order,
                    headers={"Authorization": f"Bearer {token}", "X-Store-Key": STORE_KEY},
                )
                resp.raise_for_status()
                return {**resp.json(), "degraded": False, "source_system": "partner_pos"}
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[sync_pos_order] 失败，降级为 Mock: {exc}")

    return {
        "订单号": order.get("订单号") or f"ORD{datetime.now().strftime('%H%M%S')}",
        "status": "submitted",
        "message": "订单已提交（Mock 模式）",
        "degraded": True,
        "degrade_reason": "POS 系统未接入，已模拟提交",
        "source_system": "mock",
    }


async def get_store_info(store_id: str) -> Dict[str, Any]:
    """查询门店信息（含产能配置，KitchenAgent 排产依据）"""
    logger.info(f"[get_store_info] 查询门店 {store_id}")

    if PARTNER_API_BASE:
        try:
            token = await get_access_token()
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{PARTNER_API_BASE}/v1/stores/{store_id}",
                    headers={"Authorization": f"Bearer {token}", "X-Store-Key": STORE_KEY},
                )
                resp.raise_for_status()
                return {**resp.json(), "degraded": False, "source_system": "partner"}
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[get_store_info] 失败，降级为 Mock: {exc}")

    return {
        "store_id": store_id,
        "store_name": "北京朝阳旗舰店",
        "city": "北京",
        "stations": config.KITCHEN_STATIONS,
        "capacity_per_station": 5,      # 每工位同时处理订单上限
        "peak_hours": ["11:30-13:00", "17:30-19:00"],
        "degraded": True,
        "degrade_reason": "门店系统未接入，使用 Mock 数据源",
        "source_system": "mock",
    }


# ==================== 工具注册表（需求 15 工具中的 3 个外部 API） ====================
EXTERNAL_TOOLS = {
    "get_order_detail": get_order_detail,
    "get_ingredient_inventory": get_ingredient_inventory,
    "get_review_data": get_review_data,
}


if __name__ == '__main__':
    async def _selftest():
        print("=" * 60)
        print("外部 API 封装自检（Mock 模式）")
        print("=" * 60)
        order = await get_order_detail("ORD1001")
        print("订单详情：", order["订单号"], order["状态"], order["总金额"])
        inv = await get_ingredient_inventory("store_001")
        print("库存条数：", len(inv["inventory"]), "低库存：", len(inv["low_stock"]))
        rev = await get_review_data("store_001", limit=5)
        print("评价条数：", rev["total"])
        pending = await list_pending_orders("store_001")
        print("待处理订单：", len(pending))

    asyncio.run(_selftest())
