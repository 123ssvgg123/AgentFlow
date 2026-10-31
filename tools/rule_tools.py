"""
    规则引擎工具（4 个）：tools/rule_tools.py

    对应需求文档 3.4「规则引擎」清单：
        allergen_detection       过敏原检测（预设过敏原库 + 菜品配方精确匹配）  → OrderAgent
        price_calculation        订单总价计算（折扣/套餐/会员价/服务费）      → OrderAgent
        inventory_calculation    食材需求量计算与库存对比                    → KitchenAgent
        priority_calculation     订单优先级计算（5 维加权打分）              → KitchenAgent

    「为什么这些用规则引擎而不是 LLM？」（需求文档 3.4.1 ③）
        成本：规则引擎零成本。800+ 门店日均 15 万笔订单，若每笔都调 LLM 算价格，月成本超 5 万。
        速度：规则引擎 < 10ms，LLM 至少 500ms-2s。点餐 SLA 要求单订单 < 5 秒。
        准确性：数学计算 LLM 经常算错，规则引擎 100% 准确。食品安全容不得半点差错。

    容错：规则引擎本身不失败；输入缺失时返回默认值并在结果中标记「数据缺失」。
"""
# 导入数学工具与类型标注
import math
from datetime import datetime
from typing import Any, Dict, List, Optional

from base.config import config
from base.exceptions import safe_default
from base.logger import logger


# ==================================================================
# 规则引擎 1：allergen_detection —— 过敏原检测（食品安全门禁）
# ==================================================================

def allergen_detection(dish_ingredients: List[str],
                       allergen_list: Optional[List[str]] = None,
                       dish_name: str = "") -> Dict[str, Any]:
    """
    检测菜品中的过敏原（基于预设过敏原库 + 菜品配方匹配）。

    参数：
        dish_ingredients: 菜品配料/过敏原标签列表
        allergen_list:    顾客声明的过敏原列表
        dish_name:        菜品名（用于日志与结果展示）
    返回：
        {has_allergen, allergens_found, risk_level, cross_contamination_risk, detail}
    """
    ingredients = dish_ingredients or []
    declared = allergen_list or []

    logger.info(f"[allergen_detection] 菜品「{dish_name}」配料 {len(ingredients)} 项，顾客声明过敏原 {declared}")

    if not ingredients:
        # 数据缺失：菜品未标注配方时，保守返回「未知风险」
        safe_default([], "dish_ingredients", tool="allergen_detection")
        return {
            "has_allergen": False,
            "allergens_found": [],
            "risk_level": "unknown",
            "cross_contamination_risk": False,
            "data_missing": True,
            "detail": "该菜品未标注配料信息，无法完成过敏原检测，建议人工向厨房确认",
        }

    # Step 1：本菜品实际含有的过敏原（与预设过敏原库做交集）
    dish_allergens = list({a for a in ingredients if a in config.ALLERGEN_LIBRARY})
    # 兜底：配料中直接出现的过敏原词（即便不在标准库中，只要顾客声明了也算命中）
    dish_allergens += [a for a in declared if a in ingredients and a not in dish_allergens]

    # Step 1.5：把配料里的「衍生物」归一化成基础过敏原（花生油 → 花生、蚝油 → 甲壳类/贝类）
    # 顾客声明的多是基础过敏原，而配方写的是加工形态，只做字符交集会成片漏判，
    # 属于安全侧漏——因此这里按「宁可误报不可漏判」统一展开，最终处置仍交人工确认。
    derivative_hits = []
    index = getattr(config, 'ALLERGEN_DERIVATIVE_INDEX', {})
    if index:
        for ing in ingredients:
            for base in index.get(ing, ()):
                if base not in dish_allergens:
                    dish_allergens.append(base)
                derivative_hits.append((ing, base))

    # Step 2：与顾客声明的过敏原求交集 → 命中即为风险
    found = [a for a in declared if a in dish_allergens]
    has_allergen = len(found) > 0

    # Step 3：风险等级判定
    if not has_allergen:
        risk_level = "none"
    elif any(a in config.HIGH_RISK_ALLERGENS for a in found):
        risk_level = "high"      # 高危过敏原（花生/坚果/甲壳类等）——必须隔离处理
    else:
        risk_level = "medium"

    # Step 4：交叉污染风险提示（需求文档 Q8：虽不含花生但同工位制作可能交叉接触）
    cross_risk = has_allergen and risk_level == "high"

    detail = ""
    if has_allergen:
        detail = f"检出过敏原：{'、'.join(found)}"
        # 由衍生物归一化命中的，写清是哪个配料触发的，便于人工复核而不是只看结论
        derived_for_found = [(i, b) for i, b in derivative_hits if b in found]
        if derived_for_found:
            detail += "（配料 " + "、".join(f"{i}→{b}" for i, b in dict.fromkeys(derived_for_found)) + "）"
        if cross_risk:
            detail += "；该菜品虽经确认，但同工位制作可能交叉接触，后厨须单独隔离处理"
    elif dish_allergens:
        detail = f"该菜品含 {'、'.join(dish_allergens)}，但未与顾客声明的过敏原冲突"
    else:
        detail = "未检出顾客声明的过敏原"

    return {
        "has_allergen": has_allergen,
        "allergens_found": found,
        "dish_allergens": dish_allergens,
        "derivative_hits": [f"{i}→{b}" for i, b in dict.fromkeys(derivative_hits)],
        "risk_level": risk_level,
        "cross_contamination_risk": cross_risk,
        "data_missing": False,
        "detail": detail,
        # 合规留痕字段（需求：留痕日志保存 3 年）
        "compliance": {
            "declared_allergens": declared,
            "detected": found,
            "action": "trigger_food_safety_review" if has_allergen else "normal",
            "retention_years": config.COMPLIANCE_LOG_YEARS,
        },
    }


def batch_allergen_detection(dishes: List[Dict[str, Any]],
                             allergen_list: List[str]) -> Dict[str, Any]:
    """
    批量过敏原检测（订单级）：返回每道菜的检测结果 + 订单整体风险汇总。
    对应 OrderAgent 的 check_allergens 节点。
    """
    results = []
    all_found: List[str] = []
    risky_dishes: List[str] = []

    for dish in dishes or []:
        name = dish.get("dish_name", "")
        ingredients = dish.get("allergens", []) or dish.get("ingredients", []) or []
        res = allergen_detection(ingredients, allergen_list, dish_name=name)
        res["dish_name"] = name
        results.append(res)
        if res["has_allergen"]:
            all_found.extend(res["allergens_found"])
            risky_dishes.append(name)

    all_found = list(dict.fromkeys(all_found))
    # 订单整体风险等级
    # 说明：顾客「未声明任何过敏原」时，不存在冲突对象，风险一律判为 none
    #   （此时个别菜品数据缺失记为 unknown 是噪音，会误导前端展示为「风险未知」）
    if not allergen_list:
        order_risk = "none"
    elif any(r["risk_level"] == "high" for r in results):
        order_risk = "high"
    elif any(r["risk_level"] == "medium" for r in results):
        order_risk = "medium"
    elif any(r["risk_level"] == "unknown" for r in results):
        order_risk = "unknown"
    else:
        order_risk = "none"

    # 数据缺失的菜品（用于上层提示「建议人工向厨房确认」，不阻断下单）
    missing_data_dishes = [r["dish_name"] for r in results if r.get("data_missing")]

    return {
        "order_risk_level": order_risk,
        "has_allergen": bool(all_found),
        "allergens_found": all_found,
        "risky_dishes": risky_dishes,
        "dish_results": results,
        "data_missing_dishes": missing_data_dishes if allergen_list else [],
        "review_required": order_risk in ("high", "medium"),
        "compliance_log": {
            "triggered_at": datetime.now().isoformat(),
            "declared": allergen_list,
            "detected": all_found,
            "action": "trigger_food_safety_review" if all_found else "normal",
            "retention_years": config.COMPLIANCE_LOG_YEARS,
        },
    }


# ==================================================================
# 规则引擎 2：price_calculation —— 订单总价计算
# ==================================================================

# 会员折扣率（等级 → 折扣）
MEMBER_DISCOUNT = {
    "普通": 1.00,
    "银卡": 0.95,
    "金卡": 0.90,
    "钻石": 0.85,
    "黑卡": 0.80,
}

# 服务费率（堂食默认）
DEFAULT_SERVICE_RATE = 0.0


def price_calculation(dishes: List[Dict[str, Any]],
                      member_level: str = "普通",
                      promotions: Optional[List[Dict[str, Any]]] = None,
                      service_rate: float = DEFAULT_SERVICE_RATE,
                      guest_count: int = 0) -> Dict[str, Any]:
    """
    计算订单总价（含折扣、套餐优惠、会员价、服务费）。

    参数：
        dishes:       [{dish_name, price, quantity}, ...]
        member_level: 会员等级（普通/银卡/金卡/钻石/黑卡）
        promotions:   [{name, type: percent|minus, value, min_amount}, ...]
        service_rate: 服务费率（0.1 = 10%）
        guest_count:  用餐人数（用于人均计算）
    返回：
        {subtotal, discount, service_fee, total, per_person, breakdown}
    """
    dishes = dishes or []
    promotions = promotions or []
    logger.info(f"[price_calculation] 计算 {len(dishes)} 道菜，会员={member_level}，促销 {len(promotions)} 项")

    # Step 1：小计（逐菜计算，保留 2 位小数避免浮点误差）
    breakdown = []
    subtotal = 0.0
    for dish in dishes:
        price = float(dish.get("price", 0) or 0)
        qty = int(dish.get("quantity", 1) or 1)
        line_total = round(price * qty, 2)
        subtotal += line_total
        breakdown.append({
            "dish_name": dish.get("dish_name", ""),
            "price": price,
            "quantity": qty,
            "line_total": line_total,
        })
    subtotal = round(subtotal, 2)

    # Step 2：会员折扣
    member_rate = MEMBER_DISCOUNT.get(member_level, 1.00)
    member_discount = round(subtotal * (1 - member_rate), 2)

    # Step 3：促销优惠（依序应用，先算门槛）
    promo_discount = 0.0
    promo_applied = []
    running = subtotal - member_discount
    for promo in promotions:
        min_amount = float(promo.get("min_amount", 0) or 0)
        if running < min_amount:
            continue
        ptype = promo.get("type", "percent")
        value = float(promo.get("value", 0) or 0)
        if ptype == "percent":
            cut = round(running * value, 2)
        elif ptype == "minus":
            cut = round(value, 2)
        else:
            cut = 0.0
        cut = min(cut, running)  # 优惠不得超过剩余金额
        promo_discount += cut
        running -= cut
        promo_applied.append({"name": promo.get("name", "优惠"), "amount": cut})

    promo_discount = round(promo_discount, 2)
    discount = round(member_discount + promo_discount, 2)

    # Step 4：服务费（按折后金额计）
    after_discount = max(subtotal - discount, 0.0)
    service_fee = round(after_discount * float(service_rate or 0), 2)

    # Step 5：总计
    total = round(after_discount + service_fee, 2)
    per_person = round(total / guest_count, 2) if guest_count and guest_count > 0 else 0.0

    return {
        "subtotal": subtotal,
        "member_discount": member_discount,
        "promo_discount": promo_discount,
        "discount": discount,
        "service_fee": service_fee,
        "total": total,
        "per_person": per_person,
        "member_level": member_level,
        "member_rate": member_rate,
        "promotions_applied": promo_applied,
        "breakdown": breakdown,
        "currency": "¥",
        "display": {
            "subtotal": f"¥{subtotal}",
            "discount": f"-¥{discount}",
            "total": f"¥{total}",
            "per_person": f"¥{per_person}" if per_person else "-",
        },
    }


# ==================================================================
# 规则引擎 3：inventory_calculation —— 食材库存计算
# ==================================================================

def inventory_calculation(dishes: List[Dict[str, Any]],
                          current_inventory: Dict[str, float],
                          recipe_book: Optional[Dict[str, List[Dict[str, float]]]] = None) -> Dict[str, Any]:
    """
    根据订单菜品计算食材需求量，对比当前库存。

    参数：
        dishes:            订单菜品 [{dish_name, quantity}, ...]
        current_inventory: 当前库存 {食材名: 库存量}
        recipe_book:       配方表 {菜品名: [{ingredient, amount}, ...]}（每份用量）
    返回：
        {sufficient, shortages, warning_items, required, coverage}
    """
    dishes = dishes or []
    inventory = current_inventory or {}
    recipe_book = recipe_book or _default_recipe_book()
    logger.info(f"[inventory_calculation] 计算 {len(dishes)} 道菜的食材需求")

    # Step 1：累加所需食材
    required: Dict[str, float] = {}
    # 每道菜的食材需求（供上层把「缺货食材」映射回「需排除的菜品」）
    dish_requirements: List[Dict[str, Any]] = []
    for dish in dishes:
        name = dish.get("dish_name", "")
        qty = float(dish.get("quantity", 1) or 1)
        recipe = recipe_book.get(name)
        if not recipe:
            dish_requirements.append({"dish_name": name, "ingredients": {}, "shortage": False})
            continue  # 无配方数据的菜品跳过（不猜测用量）
        per_dish: Dict[str, float] = {}
        for item in recipe:
            ing = item.get("ingredient", "")
            amount = float(item.get("amount", 0) or 0) * qty
            required[ing] = round(required.get(ing, 0) + amount, 3)
            per_dish[ing] = round(per_dish.get(ing, 0) + amount, 3)
        dish_requirements.append({"dish_name": name, "ingredients": per_dish, "shortage": False})

    # Step 2：与库存对比
    shortages = []       # 库存不足
    warning_items = []   # 库存紧张（低于需求 120%）

    for ing, need in required.items():
        have = float(inventory.get(ing, 0) or 0)
        if have < need:
            shortages.append({
                "ingredient": ing, "required": round(need, 3),
                "available": round(have, 3), "shortage": round(need - have, 3),
            })
        elif have < need * 1.2:
            warning_items.append({
                "ingredient": ing, "required": round(need, 3),
                "available": round(have, 3), "remaining_ratio": round(have / need, 2),
            })

    # Step 2.1：标注受缺货影响的菜品（dish_requirements[].shortage）
    shortage_ingredients = {s["ingredient"] for s in shortages}
    for dr in dish_requirements:
        if any(ing in shortage_ingredients for ing in (dr.get("ingredients") or {})):
            dr["shortage"] = True

    coverage = 1.0
    if required:
        covered = sum(1 for ing, need in required.items() if float(inventory.get(ing, 0) or 0) >= need)
        coverage = round(covered / len(required), 3)

    # Step 3：全局低库存预警（即便是本单不需要的食材）
    for ing, have in inventory.items():
        if float(have or 0) <= 5 and ing not in [s["ingredient"] for s in shortages]:
            warning_items.append({
                "ingredient": ing, "required": 0,
                "available": float(have), "remaining_ratio": 0.0,
                "note": "全局低库存",
            })

    return {
        "sufficient": len(shortages) == 0,
        "shortages": shortages,
        "warning_items": warning_items,
        "required": required,
        "coverage": coverage,
        "dish_requirements": dish_requirements,
        "missing_recipe": [d.get("dish_name") for d in dishes if d.get("dish_name") not in recipe_book],
    }


def get_recipe_book() -> Dict[str, List[Dict[str, float]]]:
    """公开接口：获取配方表（供 KitchenAgent 按食材维度过滤菜品使用）"""
    return _default_recipe_book()


def _default_recipe_book() -> Dict[str, List[Dict[str, float]]]:
    """默认配方表（每份用量，单位：克/份）。真实项目应由甲方食材管理系统提供。"""
    return {
        "宫保鸡丁": [{"ingredient": "鸡胸肉", "amount": 200}, {"ingredient": "花生", "amount": 30},
                     {"ingredient": "干辣椒", "amount": 10}],
        "麻婆豆腐": [{"ingredient": "豆腐", "amount": 250}, {"ingredient": "牛肉末", "amount": 50},
                     {"ingredient": "花椒", "amount": 5}],
        "清蒸鲈鱼": [{"ingredient": "鲈鱼", "amount": 500}, {"ingredient": "葱", "amount": 20},
                     {"ingredient": "姜", "amount": 15}],
        "招牌烤鸭": [{"ingredient": "鸭肉", "amount": 800}, {"ingredient": "葱", "amount": 30},
                     {"ingredient": "甜面酱", "amount": 40}],
        "素什锦": [{"ingredient": "西兰花", "amount": 150}, {"ingredient": "木耳", "amount": 50},
                   {"ingredient": "胡萝卜", "amount": 80}],
        "清炒时蔬": [{"ingredient": "时令蔬菜", "amount": 250}, {"ingredient": "蒜", "amount": 10}],
        "番茄蛋汤": [{"ingredient": "番茄", "amount": 150}, {"ingredient": "鸡蛋", "amount": 100}],
        "米饭": [{"ingredient": "大米", "amount": 150}],
    }


# ==================================================================
# 规则引擎 4：priority_calculation —— 订单优先级计算
# ==================================================================

def priority_calculation(order: Dict[str, Any],
                         store_context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    计算订单优先级（5 维加权，输出 0-100 分）。

    5 个维度（需求文档 Q3）：
        ① 下单时间  越早越高
        ② 会员等级  VIP 加 20 分
        ③ 特殊要求  过敏/忌口加 15 分
        ④ 等待时长  每多等 5 分钟加 5 分
        ⑤ 菜品制作时间 耗时长的适当提前
    """
    order = order or {}
    ctx = store_context or {}
    now = ctx.get("now") or datetime.now()

    # ① 下单时间：越早得分越高（基准 20 分，每早 1 分钟 +0.5，上限 30）
    try:
        order_time = order.get("order_time")
        if isinstance(order_time, str):
            order_time = datetime.fromisoformat(order_time)
        if not isinstance(order_time, datetime):
            order_time = now
    except (ValueError, TypeError):
        order_time = now
    waited_minutes = max((now - order_time).total_seconds() / 60.0, 0)
    time_score = min(30.0, 20 + waited_minutes * 0.5)

    # ② 会员等级（VIP 加 20 分）
    member_level = order.get("member_level", "普通")
    member_score = {"普通": 0, "银卡": 8, "金卡": 14, "钻石": 18, "黑卡": 20}.get(member_level, 0)

    # ③ 特殊要求（过敏/忌口加 15 分）
    has_special = bool(order.get("allergens") or order.get("special_req")
                       or order.get("special_requests") or order.get("dietary_restrictions"))
    special_score = 15.0 if has_special else 0.0

    # ④ 等待时长（每多等 5 分钟加 5 分，上限 20）
    wait_score = min(20.0, math.floor(waited_minutes / 5) * 5)

    # ⑤ 菜品制作时间（耗时长适当提前，上限 15）
    est_minutes = float(order.get("estimated_minutes", 0) or 0)
    cook_score = min(15.0, est_minutes * 0.6)

    priority_score = round(min(100.0, time_score + member_score + special_score + wait_score + cook_score), 1)

    # 优先级分级
    if priority_score >= 70:
        level = "P1"
    elif priority_score >= 45:
        level = "P2"
    else:
        level = "P3"

    reasons = []
    if waited_minutes >= 15:
        reasons.append(f"已等待 {int(waited_minutes)} 分钟")
    if member_level in ("金卡", "钻石", "黑卡"):
        reasons.append(f"{member_level}会员")
    if has_special:
        reasons.append("含过敏原/特殊要求")
    if est_minutes >= 15:
        reasons.append(f"制作耗时 {int(est_minutes)} 分钟")

    return {
        "priority_score": priority_score,
        "priority_level": level,
        "waited_minutes": round(waited_minutes, 1),
        "dimension_scores": {
            "time": round(time_score, 1),
            "member": member_score,
            "special": special_score,
            "wait": wait_score,
            "cook": round(cook_score, 1),
        },
        "reason": "、".join(reasons) if reasons else "常规订单",
    }


def rank_orders(orders: List[Dict[str, Any]],
                store_context: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """
    批量订单排序（KitchenAgent 的 schedule_tasks 节点使用）。
    返回按优先级降序排列、并带 queue_position 的订单列表。
    """
    ctx = store_context or {}
    scored = []
    for order in orders or []:
        calc = priority_calculation(order, ctx)
        merged = {**order, **calc}
        scored.append(merged)
    scored.sort(key=lambda x: (x["priority_score"], -x.get("waited_minutes", 0)), reverse=True)
    for idx, item in enumerate(scored, 1):
        item["queue_position"] = idx
    return scored


# ==================== 工具注册表 ====================
RULE_TOOLS = {
    "allergen_detection": allergen_detection,
    "price_calculation": price_calculation,
    "inventory_calculation": inventory_calculation,
    "priority_calculation": priority_calculation,
}


if __name__ == '__main__':
    # 自检：验证四个规则引擎
    print("=" * 60)
    print("规则引擎自检")
    print("=" * 60)

    # 1. 过敏原检测
    r1 = batch_allergen_detection(
        [{"dish_name": "宫保鸡丁", "allergens": ["花生", "鸡肉"]},
         {"dish_name": "清炒时蔬", "allergens": ["蒜"]}],
        ["花生"],
    )
    print("过敏原检测：", r1["order_risk_level"], r1["allergens_found"])

    # 2. 价格计算
    r2 = price_calculation(
        [{"dish_name": "招牌烤鸭", "price": 168, "quantity": 1},
         {"dish_name": "米饭", "price": 2, "quantity": 10}],
        member_level="金卡", promotions=[{"name": "满200减30", "type": "minus", "value": 30, "min_amount": 200}],
        guest_count=10,
    )
    print("价格计算：", r2["display"])

    # 3. 库存计算
    r3 = inventory_calculation(
        [{"dish_name": "宫保鸡丁", "quantity": 2}, {"dish_name": "米饭", "quantity": 10}],
        {"鸡胸肉": 500, "花生": 50, "干辣椒": 200, "大米": 1000},
    )
    print("库存计算：充足=", r3["sufficient"], "覆盖度=", r3["coverage"])

    # 4. 优先级计算
    r4 = priority_calculation({"order_time": "2025-09-15T11:30:00", "member_level": "钻石",
                               "allergens": ["花生"], "estimated_minutes": 20})
    print("优先级计算：", r4["priority_level"], r4["priority_score"], r4["reason"])
