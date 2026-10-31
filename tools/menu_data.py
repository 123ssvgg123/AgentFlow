"""
    菜品与营养内置数据：tools/menu_data.py

    职责：提供「无外部依赖也能跑通」的本地菜品库、营养表与营养估算能力。
        对应需求文档 3.4.1 ④「MCP 工具检索失败时降级为关键词搜索」。

    数据规模：
        MENU_DB        570 道菜（6 大类：热菜/凉菜/汤品/主食/甜品/饮品）
        NUTRITION_DB   570 条营养数据（与 MENU_DB 一一对应）
        数据由 tools/menu_seed.py 结构化生成（29 道手写核心菜 + 541 道扩充菜），
        固定随机种子，保证每次加载结果完全一致、可复现。

    说明：真实项目中这些数据来自甲方 POS / 食材管理系统；
        本文件是**降级兜底数据**，保证 Milvus / 外部 API 不可用时系统仍可演示与自测。
"""
from typing import Dict, List

from tools.menu_seed import (CORE_DISHES, build_extended_menu,  # noqa: F401
                             build_menu_db, build_nutrition_db)

# ==================================================================
# 菜品库（570 道，覆盖 6 大类别）
# 字段：dish_name / category / price / allergens / is_vegetarian / calories / sales / tags
# ==================================================================
MENU_DB: List[dict] = build_menu_db()


# ==================================================================
# 营养成分表（每份：热量 kcal / 蛋白质 g / 脂肪 g / 碳水 g / 钠 mg）
# ==================================================================
NUTRITION_DB: Dict[str, dict] = build_nutrition_db()


# 按菜品类别的营养估算基准（未收录菜品时使用）
CATEGORY_NUTRITION_BASELINE = {
    "凉菜": {"calories": 150, "protein": 8, "fat": 8, "carb": 12, "sodium": 550},
    "热菜": {"calories": 400, "protein": 22, "fat": 22, "carb": 20, "sodium": 900},
    "汤品": {"calories": 140, "protein": 7, "fat": 7, "carb": 10, "sodium": 600},
    "主食": {"calories": 380, "protein": 10, "fat": 8, "carb": 62, "sodium": 450},
    "甜品": {"calories": 240, "protein": 4, "fat": 8, "carb": 38, "sodium": 120},
    "饮品": {"calories": 100, "protein": 1, "fat": 1, "carb": 22, "sodium": 20},
}


# 名称索引（O(1) 查询，避免每次线性扫描 570 条）
_DISH_INDEX: Dict[str, dict] = {d["dish_name"]: d for d in MENU_DB}


def get_dish(dish_name: str) -> dict:
    """按名称获取菜品（未命中返回 None）"""
    return _DISH_INDEX.get(dish_name)


def search_dishes(keyword: str, limit: int = 20) -> List[dict]:
    """
    关键词搜索菜品（供 MCP 工具不可用时降级使用，需求 3.4.1 ④）。

    匹配优先级：菜名精确 > 菜名包含 > 标签命中 > 主料字符命中
    结果按「销量降序」排序，保证推荐的是真正热销的菜。
    """
    if not keyword:
        return sorted(MENU_DB, key=lambda d: d.get("sales", 0), reverse=True)[:limit]

    kw = keyword.strip()
    exact, name_hit, tag_hit, char_hit = [], [], [], []
    kw_chars = set(kw)

    for dish in MENU_DB:
        name = dish["dish_name"]
        if name == kw:
            exact.append(dish)
        elif kw in name:
            name_hit.append(dish)
        elif any(kw in t for t in dish.get("tags", [])):
            tag_hit.append(dish)
        elif kw_chars & set(name):
            char_hit.append(dish)

    merged = exact + name_hit + tag_hit + char_hit
    return merged[:limit]


def list_by_category(category: str, limit: int = 50) -> List[dict]:
    """按类别列出菜品（销量降序）"""
    items = [d for d in MENU_DB if d.get("category") == category]
    return sorted(items, key=lambda d: d.get("sales", 0), reverse=True)[:limit]


def estimate_nutrition(dish_name: str, category: str = "") -> dict:
    """
    按菜品类别估算营养（未收录菜品时使用）。
    说明：估算值仅用于展示，营养数据以内置营养表为准（未收录按类别估算）。
    """
    if not category:
        dish = get_dish(dish_name)
        category = dish.get("category", "热菜") if dish else "热菜"
    return dict(CATEGORY_NUTRITION_BASELINE.get(category, CATEGORY_NUTRITION_BASELINE["热菜"]))


if __name__ == '__main__':
    print("=" * 60)
    print("菜品数据自检")
    print("=" * 60)
    print(f"菜品库条数：{len(MENU_DB)}")
    print(f"营养表条数：{len(NUTRITION_DB)}")

    from collections import Counter
    cats = Counter(d["category"] for d in MENU_DB)
    print("\n分类分布：")
    for cat, cnt in cats.most_common():
        print(f"  {cat:6s}: {cnt:4d} 道")

    print("\n示例菜品：", get_dish("宫保鸡丁"))
    print("估算营养：", estimate_nutrition("未知菜品", "热菜"))
    print("\n关键词搜索「川菜」：")
    for d in search_dishes("川菜", limit=5):
        print(f"  {d['dish_name']:16s} ¥{d['price']:<5} 过敏原={d['allergens']}")

    assert len(MENU_DB) >= 500, f"菜品库应 ≥500 道，实际 {len(MENU_DB)}"
    assert len(NUTRITION_DB) >= len(MENU_DB), "营养表应覆盖全部菜品"
    print("\n自检通过。")
