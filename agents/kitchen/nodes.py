"""
    KitchenAgent 节点函数：agents/kitchen/nodes.py

    与 AGENT 结构对齐：节点实现为「纯函数」——接收 state dict，返回增量 dict，
    由 graph.py 负责装配进 LangGraph 状态图（或降级顺序执行器）。

    节点清单（7 个，与需求文档 3.3.2 对应）：
        load_orders / check_inventory / generate_tasks / schedule_tasks
        check_priority / assign_stations / dispatch_orders
"""
import asyncio
from datetime import datetime
from typing import Any, Dict, List

from agents.base_agent import node
from agents.kitchen.prompts import KITCHEN_PARSE_ORDER
from base.config import config
from base.logger import logger
from memory.order_state import STATUS_DISPATCHED as ORDER_STATUS_DISPATCHED
from memory.store import get_store
from tools.external_api import (get_ingredient_inventory, get_store_info, list_pending_orders,
                                sync_kitchen_display)
from tools.rule_tools import (get_recipe_book, inventory_calculation, rank_orders)

# 库存不足时「重载订单」的最大次数（防止 check_inventory ↔ load_orders 死循环）
_MAX_INVENTORY_RETRY = 1
# 后厨工位默认产能（每工位每批可承接的任务数）
_DEFAULT_STATION_CAPACITY = 5


# ==================== 节点 1：load_orders ====================

@node("加载当前待处理订单列表（含优先级、下单时间、特殊要求）")
async def load_orders(state: Dict[str, Any]) -> Dict[str, Any]:
    logger.info("[KitchenAgent] 节点 1/7 load_orders —— 加载待处理订单")
    tenant_id = state.get("tenant_id", "")
    excluded_dishes = state.get("excluded_dishes", [])          # 菜品维度（售罄/不可做）
    excluded_ingredients = state.get("excluded_ingredients", [])  # 食材维度（缺料）

    # 并行加载订单与门店信息
    orders_task = list_pending_orders(tenant_id, limit=20)
    store_task = get_store_info(tenant_id)
    orders, store_info = await asyncio.gather(orders_task, store_task, return_exceptions=True)

    if isinstance(orders, Exception):
        logger.warning(f"[KitchenAgent] 订单加载失败: {orders}")
        state.setdefault("degraded_tools", []).append("get_order_detail")
        orders = []
    if isinstance(store_info, Exception):
        logger.warning(f"[KitchenAgent] 门店信息加载失败: {store_info}")
        store_info = {"store_id": tenant_id, "stations": config.KITCHEN_STATIONS,
                      "capacity_per_station": 5}
    if isinstance(orders, list) and orders and orders[0].get("degraded"):
        state.setdefault("degraded_tools", []).append("list_pending_orders")

    # 排除已标记不可做的订单（库存不足时的回环处理）
    # 修复：原先把「食材名」也塞进 excluded_dishes 并整单剔除，
    #       会把不含该问题菜品的无关订单一起干掉。现在拆成两个维度：
    #         · excluded_dishes      → 按菜品名精确剔除该菜
    #         · excluded_ingredients → 按食材名剔除「用到该食材的菜品」
    if excluded_dishes or excluded_ingredients:
        before = len(orders)
        recipe_book = get_recipe_book()
        cleaned = []
        for o in orders:
            keeps = []
            for d in o.get("菜品列表", []):
                name = d.get("菜品名称") or d.get("dish_name", "")
                if name in excluded_dishes:
                    continue
                if excluded_ingredients:
                    used = {r.get("ingredient") for r in recipe_book.get(name, [])}
                    if used & set(excluded_ingredients):
                        continue
                keeps.append(d)
            if keeps:
                o = {**o, "菜品列表": keeps}
                cleaned.append(o)
        orders = cleaned
        logger.info(f"[KitchenAgent] 按排除清单过滤：{before} → {len(orders)} 单"
                    f"（排除菜品 {excluded_dishes}，排除食材 {excluded_ingredients}）")

    logger.info(f"[KitchenAgent] 加载到 {len(orders)} 个待处理订单")
    return {
        "pending_orders": orders,
        "store_info": store_info,
        "order_count": len(orders),
        "inventory_retry": int(state.get("inventory_retry", 0) or 0) + 1,
        "current_stage": "orders_loaded",
    }


# ==================== 节点 2：check_inventory ====================

@node("检查食材库存是否充足（对比订单食材需求与当前库存）")
async def check_inventory(state: Dict[str, Any]) -> Dict[str, Any]:
    logger.info("[KitchenAgent] 节点 2/7 check_inventory —— 检查食材库存")
    tenant_id = state.get("tenant_id", "")
    orders = state.get("pending_orders", [])

    # 汇总所有订单的菜品需求
    all_dishes: List[Dict[str, Any]] = []
    for order in orders:
        for dish in order.get("菜品列表", []):
            all_dishes.append({
                "dish_name": dish.get("菜品名称") or dish.get("dish_name", ""),
                "quantity": dish.get("数量", dish.get("quantity", 1)),
            })

    # 查库存
    inv = await get_ingredient_inventory(tenant_id)
    if isinstance(inv, Exception):
        logger.warning(f"[KitchenAgent] 库存查询失败: {inv}")
        state.setdefault("degraded_tools", []).append("get_ingredient_inventory")
        inv = {"inventory": {}, "low_stock": []}
    elif inv.get("degraded"):
        state.setdefault("degraded_tools", []).append("get_ingredient_inventory")

    calc = inventory_calculation(all_dishes, inv.get("inventory", {}))
    sufficient = calc.get("sufficient", True)

    if not sufficient:
        retry = int(state.get("inventory_retry", 0) or 0)
        logger.warning(f"[KitchenAgent] 库存不足，缺料："
                       f"{[s['ingredient'] for s in calc.get('shortages', [])]}")
        # 标记缺料食材，供 load_orders 重新加载时剔除「用到该食材的菜品」
        # 修复：写入 excluded_ingredients（食材维度），不再污染 excluded_dishes（菜品维度）
        sold_out = {s["ingredient"] for s in calc.get("shortages", [])}
        state["excluded_ingredients"] = list(
            set(state.get("excluded_ingredients") or []) | sold_out)
        if retry > _MAX_INVENTORY_RETRY:
            # 已重载过仍未凑齐库存 → 不再回环，带预警继续出餐（避免整图卡死）
            logger.warning(f"[KitchenAgent] 库存重载 {retry} 次仍不足，"
                           f"携带缺料预警继续排产（不阻断出餐）")

    return {
        "inventory": inv,
        "inventory_result": calc,
        "inventory_sufficient": sufficient,
        "current_stage": "inventory_checked",
    }


# ==================== 节点 3：generate_tasks ====================

@node("生成后厨制作任务单（每道菜的制作步骤、预计耗时、食材用量）")
async def generate_tasks(state: Dict[str, Any]) -> Dict[str, Any]:
    logger.info("[KitchenAgent] 节点 3/7 generate_tasks —— 生成制作任务单")
    orders = state.get("pending_orders", [])
    store_info = state.get("store_info", {})
    stations = store_info.get("stations") or config.KITCHEN_STATIONS

    # 先用 LLM 生成任务（可选的精细化），失败则用规则生成
    tasks = await _generate_tasks_by_llm(orders, stations)
    if tasks is None:
        state.setdefault("degraded_tools", []).append("llm_classify_intent(task_parse)")
        tasks = _generate_tasks_by_rule(orders, stations)

    logger.info(f"[KitchenAgent] 生成 {len(tasks)} 条后厨任务")
    return {"kitchen_tasks": tasks, "current_stage": "tasks_generated"}


async def _generate_tasks_by_llm(orders: list, stations: list) -> List[Dict[str, Any]]:
    """用 LLM 生成任务单（结构化输出）"""
    try:
        import json as _json
        from tools.llm_client import llm_client

        prompt = KITCHEN_PARSE_ORDER.format(
            orders=_json.dumps(orders[:10], ensure_ascii=False)[:3000],
            stations="、".join(stations),
        )
        result = await llm_client.chat_json(
            [{"role": "system", "content": "你是后厨生产计划专家，只输出 JSON。"},
             {"role": "user", "content": prompt}],
            temperature=0.2, default=None,
        )
        if isinstance(result, dict) and result.get("tasks"):
            return result["tasks"]
    except Exception as exc:  # noqa: BLE001
        logger.info(f"[KitchenAgent] LLM 生成任务单失败，降级为规则: {exc}")
    return None


def _generate_tasks_by_rule(orders: list, stations: list) -> List[Dict[str, Any]]:
    """
    规则生成任务单（降级）：
    按菜品类别映射工位，按菜品复杂度估算耗时。
    """
    # 菜品类别 → 工位映射
    category_station = {
        "凉菜": "凉菜间", "热菜": "热菜间", "汤品": "热菜间",
        "主食": "面点间", "甜品": "面点间", "饮品": "出菜口",
    }
    # 每类菜品的基准制作耗时（分钟）
    category_minutes = {"凉菜": 5, "热菜": 12, "汤品": 8, "主食": 6, "甜品": 4, "饮品": 2}

    from tools.menu_data import get_dish
    tasks = []
    for order in orders:
        for dish in order.get("菜品列表", []):
            name = dish.get("菜品名称") or dish.get("dish_name", "")
            meta = get_dish(name) or {}
            category = dish.get("类别") or meta.get("category", "热菜")
            station = category_station.get(category, "热菜间")
            if station not in stations:
                station = stations[1] if len(stations) > 1 else stations[0]
            tasks.append({
                "order_id": order.get("订单号", ""),
                "table_no": order.get("桌号", ""),
                "dish_name": name,
                "quantity": dish.get("数量", dish.get("quantity", 1)),
                "station": station,
                "category": category,
                "estimated_minutes": category_minutes.get(category, 10),
                "special_note": "过敏原隔离处理" if order.get("allergens") else "",
                "priority_hint": order.get("member_level", "普通"),
            })
    return tasks


# ==================== 节点 4：schedule_tasks ====================

@node("排产调度（根据优先级、制作时间、后厨产能分配任务到工位）")
async def schedule_tasks(state: Dict[str, Any]) -> Dict[str, Any]:
    logger.info("[KitchenAgent] 节点 4/7 schedule_tasks —— 排产调度")
    orders = state.get("pending_orders", [])
    store_info = state.get("store_info", {})
    adjustment = state.get("priority_adjustment", "")

    # 主管调整优先级时，按调整意见重新排序（Human-in-the-Loop 回环）
    applied = bool(state.get("adjustment_applied"))
    if adjustment and not applied:
        logger.info(f"[KitchenAgent] 应用主管优先级调整：{adjustment}")
        orders = _apply_adjustment(orders, adjustment)
        applied = True   # 标记已应用，避免 check_priority 反复回环造成死循环

    # 规则引擎计算优先级并排序（5 维加权）
    ctx = {"now": datetime.now(), "store_info": store_info}
    scheduled = rank_orders(orders, ctx)

    logger.info("[KitchenAgent] 排产完成，前 3 名：" +
                "、".join(f"{o.get('桌号')}(P{o.get('priority_level','')[-1]}, {o.get('priority_score')}分)"
                          for o in scheduled[:3]))
    return {"scheduled_orders": scheduled, "adjustment_applied": applied,
            "current_stage": "scheduled"}


def _apply_adjustment(orders: list, adjustment: str) -> list:
    """应用主管的优先级调整（把指定订单/桌号提到最前）"""
    import re
    # 抽取调整意见中提到的桌号，如「把 B12 提到最前」
    targets = re.findall(r"[A-Z]\d{2}", adjustment)
    if not targets:
        return orders
    front = [o for o in orders if o.get("桌号") in targets]
    rest = [o for o in orders if o.get("桌号") not in targets]
    return front + rest


# ==================== 节点 5：check_priority（Human-in-the-Loop） ====================

@node("判断是否需要人工调整优先级（高峰期/VIP/投诉等特殊情况）")
async def check_priority(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    需求文档 3.3.2 / Q6：
        高峰期（午市 11:30-13:00）订单量激增时，自动排产可能不够合理。
        本节点触发 interrupt，把排产方案推送给后厨主管平板终端；
        主管确认后从断点继续（Checkpointer 保证 State 不丢失）。
    """
    logger.info("[KitchenAgent] 节点 5/7 check_priority —— 判断是否需要人工确认")
    order_count = state.get("order_count", 0)
    scheduled = state.get("scheduled_orders", [])
    now = datetime.now()

    # 触发条件：① 订单数超阈值 ② 午/晚高峰 ③ 存在投诉/VIP 特殊订单
    is_peak = (11 <= now.hour < 13) or (17 <= now.hour < 19)
    too_many = order_count > config.KITCHEN_INTERRUPT_THRESHOLD
    has_vip = any(o.get("member_level") in ("钻石", "黑卡") for o in scheduled)
    has_complaint = any("投诉" in str(o.get("special_requests", "")) for o in scheduled)

    need_confirm = (too_many and is_peak) or has_vip or has_complaint

    # 若宿主环境已通过 API 传入确认结果（人工确认后再次进入），则不再中断
    if state.get("human_confirmed"):
        logger.info("[KitchenAgent] 已收到主管确认，继续执行")
        need_confirm = False

    # 主管已给出调整意见并完成重排 → 视为已确认，不再重复请求确认
    if state.get("adjustment_applied") and state.get("priority_adjustment"):
        logger.info("[KitchenAgent] 已应用主管调整意见，视为确认完成")
        need_confirm = False

    # Pipeline 模式：一次请求内串行跑多个 Agent，无法中途停下等人输入。
    # 此时只记录「需要确认」，不触发 interrupt，由流水线结束后统一提示。
    pipeline_mode = bool(state.get("pipeline_mode"))

    if need_confirm:
        reason = []
        if too_many and is_peak:
            reason.append(f"高峰期 {order_count} 单排队超过阈值 {config.KITCHEN_INTERRUPT_THRESHOLD}")
        if has_vip:
            reason.append("含 VIP 订单")
        if has_complaint:
            reason.append("含投诉关联订单")
        logger.warning(f"[KitchenAgent][Human-in-the-Loop] 触发人工确认：{'；'.join(reason)}")

        if pipeline_mode:
            logger.info("[KitchenAgent] Pipeline 模式：跳过 interrupt，仅标记待确认")
        else:
            # LangGraph interrupt：暂停图执行，等待人类确认
            _try_interrupt({
                "type": "kitchen_priority_confirm",
                "message": "后厨排产方案需要您确认",
                "reason": "；".join(reason),
                "schedule": [
                    {"queue_position": o.get("queue_position"), "table_no": o.get("桌号"),
                     "priority_level": o.get("priority_level"), "priority_score": o.get("priority_score"),
                     "dish_count": len(o.get("菜品列表", [])), "special": o.get("special_requests", []),
                     "waited_minutes": o.get("waited_minutes"), "reason": o.get("reason")}
                    for o in scheduled[:15]
                ],
            })

    return {
        "need_human_confirm": need_confirm,
        "confirm_reason": reason if need_confirm else [],
        "current_stage": "priority_checked",
    }


def _try_interrupt(payload: Dict[str, Any]):
    """尝试调用 LangGraph 的 interrupt（未运行时静默跳过）"""
    try:
        from langgraph.types import interrupt
        interrupt(payload)  # 暂停图执行并向上抛出中断信号
    except ImportError:
        logger.info("[KitchenAgent] langgraph 未安装，中断信息由 API 层处理")
    except Exception as exc:  # noqa: BLE001 - interrupt 本身通过异常机制暂停
        logger.info(f"[KitchenAgent] 触发 interrupt 信号: {type(exc).__name__}")


# ==================== 节点 6：assign_stations ====================

@node("分配任务到具体工位（凉菜间/热菜间/面点间/出菜口）")
async def assign_stations(state: Dict[str, Any]) -> Dict[str, Any]:
    logger.info("[KitchenAgent] 节点 6/7 assign_stations —— 分配工位")
    tasks = state.get("kitchen_tasks", [])
    store_info = state.get("store_info", {})
    stations = store_info.get("stations") or config.KITCHEN_STATIONS
    capacity = int(store_info.get("capacity_per_station", 5) or 5)

    # 按工位聚合任务，做产能均衡（规则引擎式负载分配）
    station_load: Dict[str, List[Dict[str, Any]]] = {s: [] for s in stations}
    unassigned = []
    for task in tasks:
        station = task.get("station", "")
        if station in station_load:
            station_load[station].append(task)
        else:
            unassigned.append(task)

    # 超产能的任务重新分配到负载最轻的工位
    for task in unassigned:
        lightest = min(station_load, key=lambda s: len(station_load[s]))
        task["station"] = lightest
        task["reassigned"] = True
        station_load[lightest].append(task)

    # 计算各工位负载率与预计完成时间
    assignment = {}
    for station, station_tasks in station_load.items():
        total_minutes = sum(t.get("estimated_minutes", 0) for t in station_tasks)
        assignment[station] = {
            "task_count": len(station_tasks),
            "load_ratio": round(len(station_tasks) / capacity, 2) if capacity else 0,
            "overloaded": len(station_tasks) > capacity,
            "estimated_total_minutes": total_minutes,
            "tasks": [{"order_id": t.get("order_id"), "table_no": t.get("table_no"),
                       "dish_name": t.get("dish_name"), "special_note": t.get("special_note", "")}
                      for t in station_tasks],
        }

    overloaded = [s for s, v in assignment.items() if v["overloaded"]]
    if overloaded:
        logger.warning(f"[KitchenAgent] 工位超载：{overloaded}，建议增派人手")

    return {
        "station_assignment": {"stations": assignment, "overloaded": overloaded,
                               "capacity_per_station": capacity},
        "current_stage": "stations_assigned",
    }


# ==================== 节点 7：dispatch_orders ====================

@node("下发任务到后厨 KDS 显示屏，开始制作")
async def dispatch_orders(state: Dict[str, Any]) -> Dict[str, Any]:
    logger.info("[KitchenAgent] 节点 7/7 dispatch_orders —— 下发到 KDS")
    tenant_id = state.get("tenant_id", "")
    assignment = state.get("station_assignment", {}).get("stations", {})

    # 构造 KDS 任务（按优先级顺序）
    kds_tasks = []
    for station, info in assignment.items():
        for task in info.get("tasks", []):
            kds_tasks.append({**task, "station": station})

    result = await sync_kitchen_display(tenant_id, kds_tasks)
    if isinstance(result, Exception):
        logger.warning(f"[KitchenAgent] KDS 下发失败: {result}")
        state.setdefault("degraded_tools", []).append("sync_kitchen_display")
        result = {"dispatched": False, "error": str(result)}
    elif result.get("degraded"):
        state.setdefault("degraded_tools", []).append("sync_kitchen_display")

    # 写回食材消耗记录（供备料预测，需求 3.6 ③）
    # 修复并发覆盖：改用 add_ingredient_usage（同一门店锁内读-合-写），
    # 同一天多桌下发时用量累加而不是相互覆盖。
    try:
        store = await get_store()
        usage = state.get("inventory_result", {}).get("required", {})
        if usage:
            from datetime import date
            await store.add_ingredient_usage(tenant_id, date.today(), usage,
                                             order_count=state.get("order_count", 0))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[KitchenAgent] 食材消耗记录写入失败: {exc}")

    # ---- 状态机：submitted → dispatched（逐单跃迁，带合法性校验）----
    await _mark_dispatched(state, tenant_id, kds_tasks)

    return {"dispatch_result": result, "current_stage": "dispatched"}


# ==================== 订单状态机辅助 ====================

async def _mark_dispatched(state: Dict[str, Any], tenant_id: str,
                           kds_tasks: List[Dict[str, Any]]) -> None:
    """
    对本次下发的每个订单做 submitted → dispatched 跃迁校验。

    只处理「已存在于状态机中的订单」（即来自 submit_order 的真实订单）；
    演示/直连后厨接口 的场景下订单不在状态机中，则跳过，不阻断排产。
    """
    order_ids = {t.get("order_id") for t in kds_tasks if t.get("order_id")}
    if not order_ids:
        return
    try:
        store = await get_store()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[KitchenAgent] 订单状态机不可用: {exc}")
        return

    for oid in order_ids:
        try:
            current = await store.load_order_status(tenant_id, oid)
            if current is None:
                continue  # 非状态机内订单（演示数据），跳过
            ok, detail = await store.transition_order_status(
                tenant_id, oid, ORDER_STATUS_DISPATCHED,
                operator="KitchenAgent", user_id=state.get("user_id", ""),
                reason="任务已下发后厨 KDS",
                meta={"station_count": len(state.get("station_assignment", {}).get("stations", {}))},
            )
            if not ok:
                logger.warning(f"[KitchenAgent] 订单 {oid} 下发状态跃迁被拒绝: {detail}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[KitchenAgent] 订单 {oid} 状态跃迁异常: {exc}")
