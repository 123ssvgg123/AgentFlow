"""
    OrderAgent 节点函数：agents/order/nodes.py

    与 AGENT 结构对齐：节点实现为「纯函数」——接收 state dict，返回增量 dict，
    由 graph.py 负责把它们装配进 LangGraph 状态图（或降级顺序执行器）。

    节点清单（10 个，与需求文档 3.3.1 对应）：
        parse_order / recommend_dishes / check_allergens / confirm_allergen_notice
        check_nutrition / calculate_price / validate_order / require_manual_review
        generate_summary / submit_order
"""
import asyncio
import uuid
from datetime import datetime as _dt
from typing import Any, Dict, List, Optional

from agents.base_agent import node
from agents.order.prompts import ORDER_SUMMARY
from base.config import config
from base.logger import logger
from memory.order_state import STATUS_CREATED as ORDER_STATUS_CREATED
from memory.order_state import STATUS_MANUAL_REVIEW as ORDER_STATUS_MANUAL_REVIEW
from memory.order_state import STATUS_SUBMITTED as ORDER_STATUS_SUBMITTED
from memory.store import get_store
from tools.external_api import sync_pos_order
from tools.llm_tools import llm_classify_intent, llm_recommend_dishes
from tools.mcp_tools import query_nutrition, search_menu_knowledge
from tools.rule_tools import batch_allergen_detection, price_calculation

# 推荐-校验回环的最大重试次数（防止菜品库为空等异常场景下死循环）
_MAX_RECOMMEND_RETRY = 2


# ==================== 节点 1：parse_order ====================

@node("解析顾客点餐需求（人数/口味偏好/过敏原/预算等）")
async def parse_order(state: Dict[str, Any]) -> Dict[str, Any]:
    logger.info("[OrderAgent] 节点 1/10 parse_order —— 解析顾客点餐需求")
    user_input = state.get("user_input", "")

    # 构造上下文：注入门店偏好与顾客画像（长期记忆，需求 3.6 ④ 读取时机）
    prefs = state.get("store_preferences") or {}
    history = state.get("customer_history") or {}
    context = f"门店热门菜品：{prefs.get('popular_dishes', [])}；顾客历史偏好：{history}"
    if isinstance(state.get("messages"), list) and state["messages"]:
        context += f"；对话轮数：{len(state['messages'])}"

    needs = await llm_classify_intent(user_input, context)
    if needs.get("degraded"):
        state.setdefault("degraded_tools", []).append("llm_classify_intent")

    # 与调用方显式传入的 customer_needs 合并。
    # 背景：POST /api/orders 会把 dishes / guest_count / table_no / allergens 以结构化方式传进来，
    # 而 LLM 解析只盯自然语言——若直接以 LLM 结果整体替换，结构化传入的过敏原会被清空（安全字段丢失）。
    # 策略：LLM 解析出的非空字段优先，其余字段回落到调用方传入值。
    incoming = state.get("customer_needs")
    if not isinstance(incoming, dict):
        incoming = {}
    if incoming:
        for k, v in incoming.items():
            if k == "allergens":
                continue
            if needs.get(k) in (None, "", [], {}):
                needs[k] = v
        logger.info(f"[OrderAgent] 已合并调用方传入的字段：{sorted(incoming.keys())}")

    # 过敏原只增不减：显式传入 ∪ LLM 解析 ∪ 顾客画像（安全字段不允许被任何一方覆盖丢失）
    allergens = set(incoming.get("allergens") or []) | set(needs.get("allergens") or [])
    profile_allergens = history.get("allergens") if isinstance(history, dict) else None
    if profile_allergens:
        allergens |= set(profile_allergens)
        needs["allergens_from_memory"] = True
        logger.info(f"[OrderAgent] 从顾客画像补充过敏原：{profile_allergens}")
    if allergens:
        needs["allergens"] = sorted(allergens)
        logger.info(f"[OrderAgent] 本次生效过敏原：{needs['allergens']}")

    return {"customer_needs": needs, "current_stage": "parsed"}


# ==================== 节点 2：recommend_dishes ====================

@node("根据顾客需求智能推荐菜品组合（素食/低卡/招牌等）")
async def recommend_dishes(state: Dict[str, Any]) -> Dict[str, Any]:
    logger.info("[OrderAgent] 节点 2/10 recommend_dishes —— 智能推荐菜品")
    needs = state.get("customer_needs", {})
    excluded = state.get("excluded_dishes", [])  # 校验失败后需排除的售罄菜品

    # 并行检索菜品知识库（对应需求 7.1「节点并行 asyncio.gather」）
    queries = ["招牌菜 推荐"]
    if "素食" in needs.get("dietary_restrictions", []):
        queries.append("素食 菜品")
    if "低卡" in needs.get("dietary_restrictions", []):
        queries.append("低卡 清淡 菜品")
    if needs.get("taste_preferences"):
        queries.append(" ".join(needs["taste_preferences"]) + " 菜品")

    # asyncio.gather 并行检索多个查询
    tasks = [search_menu_knowledge(q, top_k=10,
                                   filters={"exclude_allergens": needs.get("allergens", [])})
             for q in queries]
    gathered = await asyncio.gather(*tasks, return_exceptions=True)

    # 合并去重候选菜品
    menu_map: Dict[str, Dict[str, Any]] = {}
    for result in gathered:
        if isinstance(result, Exception):
            logger.warning(f"[OrderAgent] 菜品检索部分失败: {result}")
            state.setdefault("degraded_tools", []).append("search_menu_knowledge")
            continue
        for dish in result or []:
            name = dish.get("dish_name")
            if name and name not in excluded:
                menu_map.setdefault(name, dish)

    # 兜底：若检索结果不足以推荐，直接取本地菜品库
    if len(menu_map) < 5:
        from tools.menu_data import MENU_DB
        for dish in MENU_DB:
            if dish["dish_name"] not in excluded:
                menu_map.setdefault(dish["dish_name"], dish)

    # 极端兜底：若排除后仍无候选（例如全部被排除），重置排除列表，避免死循环
    if not menu_map and excluded:
        logger.warning("[OrderAgent] 候选菜品被全部排除，重置排除列表以保证可下单")
        excluded = []
        from tools.menu_data import MENU_DB
        for dish in MENU_DB:
            menu_map.setdefault(dish["dish_name"], dish)

    menu_data = list(menu_map.values())
    logger.info(f"[OrderAgent] 候选菜品 {len(menu_data)} 道（已排除 {len(excluded)} 道售罄菜）")

    result = await llm_recommend_dishes(needs, menu_data)
    if result.get("degraded"):
        state.setdefault("degraded_tools", []).append("llm_recommend_dishes")

    return {
        "recommended_dishes": result.get("dishes", []),
        "recommendation_notes": result.get("combination_notes", ""),
        "vegetarian_arrangement": result.get("vegetarian_arrangement", ""),
        "excluded_dishes": excluded,
        "current_stage": "recommended",
    }


# ==================== 节点 3：check_allergens（食品安全门禁） ====================

@node("检测订单中的过敏原，触发食品安全流程")
async def check_allergens(state: Dict[str, Any]) -> Dict[str, Any]:
    logger.info("[OrderAgent] 节点 3/10 check_allergens —— 过敏原检测（食品安全门禁）")
    needs = state.get("customer_needs", {})
    dishes = state.get("recommended_dishes", [])
    declared = needs.get("allergens", [])
    # 修复（二次审计）：合规留痕需要订单号，但原实现要到 submit_order 才生成，
    # 导致检测留痕的 order_id 为空。这里统一预生成，后续节点复用同一个号。
    order_id = state.get("order_id") or _new_order_id(state.get("tenant_id", ""))

    # 规则引擎批量检测（100% 准确，不交给 LLM）
    result = batch_allergen_detection(dishes, declared)
    risk = result.get("order_risk_level", "none")

    if risk in ("high", "medium"):
        logger.warning(f"[OrderAgent][食品安全] 检出过敏原 {result['allergens_found']}，"
                       f"风险等级 {risk}，触发食品安全审查流程")
    if result.get("data_missing_dishes"):
        logger.warning(f"[OrderAgent] 以下菜品缺少配料数据，建议人工确认：{result['data_missing_dishes']}")

    # 合规留痕（需求 3.8 Q8：留痕日志保存 3 年）
    if declared:
        try:
            store = await get_store()
            await store.save_compliance_log(
                tenant_id=state.get("tenant_id", ""),
                log={
                    "order_id": order_id,
                    "declared_allergens": declared,
                    "detected_allergens": result.get("allergens_found", []),
                    "recommended_dishes": [d.get("dish_name") for d in dishes],
                    "isolation_action": "后厨单独工位制作，专用器具，出餐前复核" if result.get("has_allergen") else "无",
                    "action": result["compliance_log"]["action"],
                },
                user_id=state.get("user_id", ""),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[OrderAgent] 合规留痕写入失败: {exc}")
            state.setdefault("errors", []).append(f"合规留痕失败: {exc}")

        # 过滤掉真正含过敏原的菜品（食品安全红线：宁可少推荐）
        if result.get("has_allergen"):
            risky = set(result.get("risky_dishes", []))
            safe_dishes = [d for d in dishes if d.get("dish_name") not in risky]
            if safe_dishes:
                logger.info(f"[OrderAgent] 已剔除 {len(dishes) - len(safe_dishes)} 道含过敏原菜品")
                dishes = safe_dishes

    return {
        "order_id": order_id,          # 预生成的订单号，下游节点复用
        "allergen_result": result,
        "allergen_risk": risk,
        "recommended_dishes": dishes,
        "current_stage": "allergen_checked",
    }


# ==================== 节点 3.5：confirm_allergen_notice（口头告知留痕） ====================

@node("过敏原风险订单：生成口头告知话术并留痕（可追溯）")
async def confirm_allergen_notice(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    补全的关键合规环节（需求 3.8 Q8）：

        原实现只把「系统检测到过敏原」写进 compliance_log，
        但法规要求的是「**口头告知顾客并得到知悉确认**」这一动作本身要留痕。
        本节点负责：
            1) 生成标准告知话术（含过敏原清单 + 隔离措施 + 请勿食用提示）
            2) 把话术放进 state，供服务员/前端朗读
            3) 若本次调用已带人工确认标记（human_confirmed / allergen_notice_confirmed），
               则直接落库留痕；否则标记 pending 待确认

    说明：本节点不做 interrupt（避免与 Pipeline 人工确认点冲突），
          只负责「生成话术 + 条件留痕」，确认动作由 API 层透传。
    """
    logger.info("[OrderAgent] 节点 4/10 confirm_allergen_notice —— 过敏原口头告知留痕")
    tenant_id = state.get("tenant_id", "")
    # 修复（二次审计）：原实现用 "(待生成)" 占位，而 order_id 要到 submit_order 才生成，
    # 导致下方「已确认 → 落库留痕」的条件（order_id 非空且不以 "(" 开头）永远为假，
    # 合规留痕从未真正落库。现在在告知节点就预生成订单号并回写 state，
    # submit_order 复用同一个号，保证「告知留痕」与「订单提交」指向同一订单。
    order_id = state.get("order_id") or _new_order_id(tenant_id)
    needs = state.get("customer_needs", {})
    allergen = state.get("allergen_result", {})
    dishes = state.get("recommended_dishes", [])

    declared = needs.get("allergens", []) or []
    detected = allergen.get("allergens_found", []) or []
    union = list(dict.fromkeys([*declared, *detected]))
    risky = [r["dish_name"] for r in allergen.get("dish_results", []) if r.get("has_allergen")]
    isolation = "后厨单独工位制作、专用器具、出餐前专人复核"

    notice = (
        f"【过敏原告知】您的订单涉及过敏原：{'、'.join(union)}"
        f"{'，涉及菜品：' + '、'.join(risky) if risky else ''}。"
        f"后厨将按「{isolation}」处理。请您确认知悉并确保相关顾客不食用，谢谢配合。"
    )

    confirmed = bool(state.get("human_confirmed") or state.get("allergen_notice_confirmed"))
    payload = {
        "notice_content": notice,
        "declared_allergens": union,
        "isolated_dishes": risky,
        "isolation_action": isolation,
        "notice_confirmed": confirmed,
    }

    if confirmed and order_id:
        try:
            store = await get_store()
            stored = await store.confirm_allergen_notice(
                tenant_id, order_id,
                confirmed_by=state.get("user_id", "") or state.get("confirmed_by", ""),
                channel=state.get("notice_channel", "verbal"),
                notice_content=notice,
                customer_ack=True,
                user_id=state.get("user_id", ""),
            )
            payload["notified_at"] = str(stored.get("confirmed_at", ""))
            logger.info(f"[OrderAgent][合规留痕] 订单 {order_id} 口头告知已确认并留痕")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[OrderAgent] 口头告知留痕失败: {exc}")
            state.setdefault("errors", []).append(f"告知留痕失败: {exc}")
    else:
        logger.info("[OrderAgent] 过敏原告知话术已生成，等待服务员确认（未留痕）")

    return {
        "order_id": order_id,          # 预生成的订单号，下游 submit_order 复用
        "allergen_notice": payload,
        "allergen_notice_pending": not confirmed,
        "current_stage": "allergen_notice_ready",
    }


# ==================== 节点 4：check_nutrition ====================

@node("计算订单营养成分（热量/蛋白质/脂肪等），给出健康建议")
async def check_nutrition(state: Dict[str, Any]) -> Dict[str, Any]:
    logger.info("[OrderAgent] 节点 5/10 check_nutrition —— 营养成分分析")
    dishes = state.get("recommended_dishes", [])

    # 并行查询每道菜的营养成分
    tasks = [query_nutrition(d.get("dish_name", "")) for d in dishes[:8]]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    totals = {"calories": 0.0, "protein": 0.0, "fat": 0.0, "carb": 0.0, "sodium": 0.0}
    detail = []
    for dish, res in zip(dishes[:8], results):
        if isinstance(res, Exception):
            logger.warning(f"[OrderAgent] 营养查询失败: {res}")
            state.setdefault("degraded_tools", []).append("query_nutrition")
            nutrition = {}
        else:
            nutrition = res.get("nutrition") or {}
            if res.get("degraded"):
                state.setdefault("degraded_tools", []).append("query_nutrition")
            # 若返回的是 detail 文本（内置营养表未命中路径），用估算兜底
            if not nutrition:
                from tools.menu_data import estimate_nutrition
                nutrition = estimate_nutrition(dish.get("dish_name", ""))
                # 修复：估算值必须打标，否则摘要里的营养数字真假难辨
                nutrition["estimated"] = True
                state.setdefault("degraded_tools", []).append("query_nutrition(estimated)")

        qty = int(dish.get("quantity", 1) or 1)
        for key in totals:
            totals[key] += float(nutrition.get(key, 0) or 0) * qty
        detail.append({"dish_name": dish.get("dish_name"), "nutrition": nutrition})

    totals = {k: round(v, 1) for k, v in totals.items()}

    # 生成健康建议
    advice = []
    if totals["calories"] > 1000:
        advice.append("本餐热量偏高，建议增加蔬菜比例")
    if totals["sodium"] > 2000:
        advice.append("钠摄入偏高，建议搭配低盐菜品")
    if totals["fat"] > 60:
        advice.append("脂肪含量较高，减脂人群请适量")
    if not advice:
        advice.append("荤素搭配合理，营养结构较为均衡")

    return {
        "nutrition_info": {"total": totals, "detail": detail, "advice": advice},
        "current_stage": "nutrition_checked",
    }


# ==================== 节点 5：calculate_price ====================

@node("计算订单总价（含折扣、套餐优惠、会员价等）")
async def calculate_price(state: Dict[str, Any]) -> Dict[str, Any]:
    logger.info("[OrderAgent] 节点 6/10 calculate_price —— 计算订单总价")
    dishes = state.get("recommended_dishes", [])
    needs = state.get("customer_needs", {})
    prefs = state.get("store_preferences", {})

    # 会员等级：顾客画像优先，其次订单，最后默认
    history = state.get("customer_history") or {}
    member_level = (history.get("member_level") if isinstance(history, dict) else None) or "普通"

    # 促销：按门店平均客单价给示例优惠（真实场景由 POS 系统下发）
    promotions = []
    subtotal_est = sum(float(d.get("price", 0)) * int(d.get("quantity", 1) or 1) for d in dishes)
    if subtotal_est >= 300:
        promotions.append({"name": "满300减30", "type": "minus", "value": 30, "min_amount": 300})
    if needs.get("occasion") in ("家庭聚餐", "朋友聚会", "商务宴请"):
        promotions.append({"name": "聚餐9折", "type": "percent", "value": 0.10, "min_amount": 0})

    price = price_calculation(
        dishes=dishes, member_level=member_level, promotions=promotions,
        guest_count=int(needs.get("guest_count", 0) or 0),
    )
    logger.info(f"[OrderAgent] 订单总价 {price['display']['total']}（会员 {member_level}）")
    return {"price_detail": price, "current_stage": "priced"}


# ==================== 节点 6：validate_order ====================

@node("校验订单完整性（菜品数量/库存/可制作性）")
async def validate_order(state: Dict[str, Any]) -> Dict[str, Any]:
    logger.info("[OrderAgent] 节点 7/10 validate_order —— 校验订单完整性")
    dishes = state.get("recommended_dishes", [])
    issues = []

    # 校验 1：菜品不能为空
    if not dishes:
        issues.append("推荐菜品为空，无法下单")

    # 校验 2：库存可制作性（调食材库存 + 规则计算）
    all_in_stock = True
    shortage_dishes: List[str] = []
    try:
        from tools.external_api import get_ingredient_inventory
        from tools.rule_tools import inventory_calculation
        inv = await get_ingredient_inventory(state.get("tenant_id", ""))
        calc = inventory_calculation(dishes, inv.get("inventory", {}))
        if not calc.get("sufficient"):
            all_in_stock = False
            sold_out = [s["ingredient"] for s in calc.get("shortages", [])]
            issues.append(f"以下食材库存不足：{'、'.join(sold_out)}")
            # 记录受影响的菜品，供下一轮推荐时排除
            shortage_dishes = [r.get("dish_name") for r in calc.get("dish_requirements", [])
                               if r.get("shortage") and r.get("dish_name")]
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[OrderAgent] 库存校验失败（跳过）: {exc}")

    # 校验 3：过敏原复检（关键修复）
    # 换菜回环后 recommend_dishes 会重新出菜，而 check_allergens 只在首次执行。
    # 这里对「当前这批菜」重新跑一次规则引擎，避免换菜后引入新过敏原而无人察觉。
    declared = (state.get("customer_needs", {}) or {}).get("allergens", [])
    if declared:
        from tools.rule_tools import batch_allergen_detection
        # 补齐过敏原数据：优先用菜品自带字段，缺失时回查本地菜品库
        # （否则 LLM 推荐出的精简菜品对象会因缺 allergens 字段而全判 unknown）
        enriched = []
        for d in dishes:
            if not (d.get("allergens") or d.get("ingredients")):
                from tools.menu_data import get_dish
                meta = get_dish(d.get("dish_name", "")) or {}
                d = {**d, "allergens": meta.get("allergens", []),
                     "ingredients": meta.get("allergens", [])}
            enriched.append(d)

        recheck = batch_allergen_detection(enriched, declared)
        recheck_risk = recheck.get("order_risk_level", "none")
        if recheck_risk != state.get("allergen_risk"):
            logger.warning(f"[OrderAgent] 过敏原复检风险等级变化："
                           f"{state.get('allergen_risk')} → {recheck_risk}")
            state["allergen_risk"] = recheck_risk
            state["allergen_result"] = recheck
        risky = recheck.get("risky_dishes", [])
        if risky:
            # 检出过敏原冲突：不阻断，但必须在摘要中强制警示
            logger.warning(f"[OrderAgent] 复检发现过敏原冲突菜品：{risky}")
            state["allergen_high_risk_dishes"] = risky

    valid = not issues
    retry = int(state.get("recommend_retry_count", 0) or 0)
    if not valid:
        # 记录需排除的菜品（库存不足 / 首道菜异常），供 recommend_dishes 下轮换菜
        to_exclude = shortage_dishes or [d.get("dish_name") for d in dishes[:1]]
        state["excluded_dishes"] = list({*(state.get("excluded_dishes") or []), *to_exclude})
        retry += 1
        if retry <= _MAX_RECOMMEND_RETRY:
            logger.warning(f"[OrderAgent] 订单校验不通过（第 {retry} 次）：{issues}，回到推荐节点换菜"
                           f"（排除 {to_exclude}）")
        else:
            # 已达重试上限：不再回环，由条件边路由到 require_manual_review 终态节点（拦截下单）
            logger.warning(f"[OrderAgent] 订单校验不通过且已达重试上限 {_MAX_RECOMMEND_RETRY}：{issues}，"
                           f"转人工介入（下单被拦截）")

    return {
        "order_valid": valid,
        "validation_issues": issues,
        "all_in_stock": all_in_stock,
        "excluded_dishes": state.get("excluded_dishes", []),
        "recommend_retry_count": retry,
        "needs_manual_review": (not valid and retry > _MAX_RECOMMEND_RETRY),
        # 复检结果需显式回传，否则节点返回值不带该字段（state 已更新但调用方看不到）
        "allergen_risk": state.get("allergen_risk", "none"),
        "allergen_result": state.get("allergen_result", {}),
        "allergen_high_risk_dishes": state.get("allergen_high_risk_dishes", []),
        "current_stage": "validated",
    }


# ==================== 节点 6.5：require_manual_review（校验失败兜底） ====================

@node("校验未通过且已达重试上限，转人工介入（不下单）")
async def require_manual_review(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    关键修复：原先 validate_order 重试耗尽后仍会硬走到 submit_order，
    导致「校验不通过的订单被真实提交」。此处作为终态节点拦截：
        · 不下单、不写 POS/KDS
        · 输出待服务员处理的订单草稿 + 问题清单
        · 置 needs_manual_review 标记，API 层据此提示人工介入
    """
    logger.warning("[OrderAgent] 节点 8/10 require_manual_review —— 转人工介入（不下单）")
    dishes = state.get("recommended_dishes", [])
    issues = state.get("validation_issues", [])
    price = state.get("price_detail", {})
    allergen = state.get("allergen_result", {})

    lines = ["## 订单需人工处理", "",
             "系统已尝试自动换菜 2 次仍未满足下单条件，**订单未提交**，请服务员介入处理。", ""]
    if issues:
        lines.append("**未通过原因：**")
        for item in issues:
            lines.append(f"- {item}")
        lines.append("")

    if dishes:
        lines.append("**当前候选菜品（仅供参考）：**")
        lines.append("| 菜品 | 数量 | 单价 |")
        lines.append("|------|------|------|")
        for d in dishes:
            p = float(d.get("price", 0) or 0)
            lines.append(f"| {d.get('dish_name', '')} | {d.get('quantity', 1)} | ¥{p} |")
        display = price.get("display", {})
        if display.get("total"):
            lines.append("")
            lines.append(f"**预估合计**：{display.get('total')}（未提交）")

    if allergen.get("has_allergen"):
        found = "、".join(allergen.get("allergens_found", []))
        lines += ["", f"> {config.ALLERGEN_ALERT_PREFIX} 检出过敏原：**{found}**，"
                      f"人工处理时务必重点确认。"]

    lines += ["", "> 可选操作：更换菜品后重新下单、拆分为多单、或联系食材管理补货。"]
    summary = "\n".join(lines)

    # 状态机：created → manual_review（终态分支，坚决不下单）
    order_id = state.get("order_id") or _new_order_id(state.get("tenant_id", ""))
    await _record_status(state, order_id, ORDER_STATUS_MANUAL_REVIEW,
                         reason="校验超限转人工：" + ("; ".join(issues) or "未说明"),
                         meta={"issue_count": len(issues),
                               "dish_count": len(dishes)})

    return {
        "order_id": order_id,
        "order_status": ORDER_STATUS_MANUAL_REVIEW,
        "order_summary": summary,
        "needs_manual_review": True,
        "order_valid": False,
        "submit_result": {"pos": {"submitted": False, "reason": "manual_review_required"},
                          "kds": {"dispatched": False, "reason": "manual_review_required"}},
        "current_stage": "manual_review_required",
    }


# ==================== 节点 7：generate_summary ====================

@node("生成订单确认摘要（菜品清单+价格+过敏原提示+营养建议）")
async def generate_summary(state: Dict[str, Any]) -> Dict[str, Any]:
    logger.info("[OrderAgent] 节点 9/10 generate_summary —— 生成订单确认摘要")
    dishes = state.get("recommended_dishes", [])
    price = state.get("price_detail", {})
    nutrition = state.get("nutrition_info", {})
    allergen = state.get("allergen_result", {})
    needs = state.get("customer_needs", {})

    lines = ["## 订单确认", ""]

    # 菜品表格
    lines.append("| 菜品 | 数量 | 单价 | 小计 | 说明 |")
    lines.append("|------|------|------|------|------|")
    for d in dishes:
        name = d.get("dish_name", "")
        qty = d.get("quantity", 1)
        p = float(d.get("price", 0) or 0)
        tag = "素食" if d.get("is_vegetarian") else "荤菜"
        reason = d.get("reason", "")
        lines.append(f"| {name} | {qty} | ¥{p} | ¥{round(p * qty, 2)} | {tag}·{reason} |")

    lines.append("")
    display = price.get("display", {})
    lines.append(f"**小计**：{display.get('subtotal', '-')}　"
                 f"**优惠**：{display.get('discount', '-')}　"
                 f"**合计**：{display.get('total', '-')}")
    if needs.get("guest_count"):
        lines.append(f"**人均**：{display.get('per_person', '-')}（{needs['guest_count']} 人）")

    # 过敏原警示（强制醒目，需求 3.4「模型输出合规规范」）
    if allergen.get("has_allergen"):
        found = "、".join(allergen.get("allergens_found", []))
        lines.append("")
        lines.append(f"> {config.ALLERGEN_ALERT_PREFIX} 检出过敏原：**{found}**")
        if allergen.get("dish_results"):
            risky = [r["dish_name"] for r in allergen["dish_results"] if r.get("has_allergen")]
            lines.append(f"> 涉及菜品：{'、'.join(risky)}")
        lines.append("> 后厨将安排独立工位、专用器具制作，出餐前专人复核，请务必确认相关顾客不食用。")

    # 营养建议
    advice = nutrition.get("advice", [])
    if advice:
        lines.append("")
        lines.append(f"**营养建议**：{'；'.join(advice)}")
    totals = nutrition.get("total", {})
    if totals:
        lines.append(f"**营养合计**：热量 {totals.get('calories', 0)} kcal，"
                     f"蛋白质 {totals.get('protein', 0)} g，脂肪 {totals.get('fat', 0)} g")

    summary = "\n".join(lines)

    # 尝试用 LLM 润色（失败则用规则版，保证不阻断流程）
    try:
        from tools.llm_client import llm_client
        polished = await llm_client.chat(
            [{"role": "user", "content": ORDER_SUMMARY.format(
                customer_needs=needs, order_detail=summary,
                nutrition=nutrition.get("advice", []), allergen_result=allergen)}],
            temperature=0.3, max_retries=0,
        )
        if polished and len(polished) > len(summary) * 0.5:
            summary = polished
    except Exception as exc:  # noqa: BLE001
        logger.info(f"[OrderAgent] 摘要 LLM 润色跳过，使用规则版: {exc}")

    return {"order_summary": summary, "current_stage": "summarized"}


# ==================== 节点 8：submit_order ====================

@node("提交订单到 POS 系统和后厨 KDS")
async def submit_order(state: Dict[str, Any]) -> Dict[str, Any]:
    logger.info("[OrderAgent] 节点 10/10 submit_order —— 提交订单")
    tenant_id = state.get("tenant_id", "")
    dishes = state.get("recommended_dishes", [])
    price = state.get("price_detail", {})
    needs = state.get("customer_needs", {})

    # 订单号：用 uuid4 短码保证稳定可复现
    order_id = state.get("order_id") or _new_order_id(tenant_id)

    # ---- 状态机：created → submitted（先落 created，再跃迁 submitted）----
    await _ensure_created(state, order_id, tenant_id)
    ok, detail = await _record_status(
        state, order_id, ORDER_STATUS_SUBMITTED,
        reason="校验通过，提交 POS/KDS",
        meta={"dish_count": len(dishes), "total": price.get("total", 0)},
    )
    if not ok:
        # 状态机拒绝（如终端态重复提交）→ 不写外部系统，直接返回失败结果
        logger.warning(f"[OrderAgent] 状态机拒绝提交（{detail}），订单 {order_id} 未下发")
        return {
            "order_id": order_id,
            "order_status": state.get("order_status", ""),
            "submit_result": {"pos": {"submitted": False, "reason": detail},
                              "kds": {"dispatched": False, "reason": detail}},
            "submit_blocked": True,
            "current_stage": "submit_rejected",
        }

    # 构造发往 POS 的订单（含合规字段）
    order_payload = {
        "订单号": order_id,
        "门店": tenant_id,
        "桌号": needs.get("table_no", ""),
        "人数": needs.get("guest_count", 0),
        "菜品列表": [{"菜品名称": d.get("dish_name"), "数量": d.get("quantity", 1),
                      "单价": d.get("price", 0)} for d in dishes],
        "总金额": price.get("total", 0),
        "过敏原": state.get("allergen_result", {}).get("allergens_found", []),
        "特殊处理": state.get("allergen_risk") in ("high", "medium"),
    }

    # 并行提交到 POS 与 KDS（节点并行）
    pos_task = sync_pos_order(tenant_id, order_payload)
    kds_task = _submit_to_kds(state, order_id, dishes)
    pos_result, kds_result = await asyncio.gather(pos_task, kds_task, return_exceptions=True)

    if isinstance(pos_result, Exception):
        logger.warning(f"[OrderAgent] POS 提交失败: {pos_result}")
        pos_result = {"submitted": False, "error": str(pos_result)}
        state.setdefault("degraded_tools", []).append("sync_pos_order")
    if isinstance(kds_result, Exception):
        logger.warning(f"[OrderAgent] KDS 提交失败: {kds_result}")
        kds_result = {"dispatched": False, "error": str(kds_result)}

    # 异步写回长期记忆（需求 3.6 ④ 写入时机）
    try:
        store = await get_store()
        await store.append_store_preferences(
            tenant_id,
            {
                "popular_dishes": [d.get("dish_name") for d in dishes if d.get("dish_name")],
                "avg_order_value": price.get("per_person", 0) or 0,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[OrderAgent] 写回门店偏好失败: {exc}")

    return {
        "order_id": order_id,
        "order_status": ORDER_STATUS_SUBMITTED,
        "submit_result": {"pos": pos_result, "kds": kds_result},
        "current_stage": "submitted",
    }


# ==================== 订单状态机辅助 ====================

async def _ensure_created(state: Dict[str, Any], order_id: str, tenant_id: str) -> None:
    """
    确保订单已进入 created 状态（幂等）。
    首次流转必须显式落 created，否则后续跃迁会被状态机拒绝。
    """
    store = await get_store()
    current = await store.load_order_status(tenant_id, order_id)
    if current is None:
        await store.transition_order_status(
            tenant_id, order_id, ORDER_STATUS_CREATED,
            operator="OrderAgent", user_id=state.get("user_id", ""),
            reason="订单校验通过，创建订单",
            meta={"intent": state.get("intent", ""),
                  "items": len(state.get("recommended_dishes", []))},
        )


async def _record_status(state: Dict[str, Any], order_id: str, to_status: str,
                         reason: str = "", meta: Optional[Dict[str, Any]] = None):
    """
    记录订单状态跃迁（带合法性校验）。返回 (是否成功, 说明)。
    写失败不影响主流程，仅记 warning —— 状态机是「增强可追溯性」，不应阻断业务。
    """
    try:
        store = await get_store()
        return await store.transition_order_status(
            state.get("tenant_id", ""), order_id, to_status,
            operator=state.get("_status_operator", "OrderAgent"),
            user_id=state.get("user_id", ""),
            reason=reason, meta=meta,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[OrderAgent] 订单状态跃迁记录失败（{to_status}）: {exc}")
        return False, f"error:{exc}"


def _new_order_id(tenant_id: str) -> str:
    """
    生成订单号：ORD + 门店短码 + 日期 + uuid4 短码。
    用 uuid 保证全局唯一且可复现（不受 PYTHONHASHSEED 影响）。
    """
    store_code = "".join(c for c in (tenant_id or "000")[-3:] if c.isalnum()) or "000"
    return f"ORD{store_code}{_dt.now().strftime('%m%d')}{uuid.uuid4().hex[:6].upper()}"


async def _submit_to_kds(state: Dict[str, Any], order_id: str,
                         dishes: list) -> Dict[str, Any]:
    """提交到后厨 KDS（复用 external_api 的 sync_kitchen_display）"""
    from tools.external_api import sync_kitchen_display
    tasks = [{"order_id": order_id, "dish_name": d.get("dish_name"),
              "quantity": d.get("quantity", 1),
              "special_note": "过敏原隔离" if state.get("allergen_risk") in ("high", "medium") else ""}
             for d in dishes]
    return await sync_kitchen_display(state.get("tenant_id", ""), tasks)
