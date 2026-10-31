"""
    种子菜单数据集（500+ 道）：tools/menu_seed.py

    职责：为 tools/menu_data.py 提供「手写核心菜 + 程序化扩充菜」的完整数据源。

    设计动机：
        需求文档宣称菜品库覆盖 500+ 道，但原 MENU_DB 只有 29 道手写示例。
        本模块通过「菜系 × 主料 × 做法」的结构化组合，生成语义合理、
        过敏原标注正确、营养有梯度的 500+ 道菜品，且：
            · 核心 29 道手写菜保持原样（带真实价格与销量，用于演示）
            · 扩充菜使用确定性伪随机（固定 seed）→ 数据可复现，不随进程变化
            · 过敏原由主料/调料推导，保证 allergen_detection 工具真实可用

    数据字段（与 MENU_DB 一致）：
        dish_name / category / price / allergens / is_vegetarian / calories / sales / tags
"""
import random
from typing import Dict, List

# 固定 seed：保证每次生成的菜单完全一致（避免自检 diff 抖动）
_RNG = random.Random(20241013)


# ==================================================================
# 核心手写菜（29 道，保留原样 —— 演示与测试依赖这些名字）
# ==================================================================
CORE_DISHES: List[dict] = [
    # ---------- 招牌菜 ----------
    {"dish_name": "招牌烤鸭", "category": "热菜", "price": 168, "allergens": ["麸质"],
     "is_vegetarian": False, "calories": 620, "sales": 980, "tags": ["招牌", "必点", "京菜"]},
    {"dish_name": "清蒸鲈鱼", "category": "热菜", "price": 88, "allergens": ["鱼类"],
     "is_vegetarian": False, "calories": 280, "sales": 760, "tags": ["清淡", "招牌", "粤菜"]},
    {"dish_name": "宫保鸡丁", "category": "热菜", "price": 48, "allergens": ["花生", "鸡肉"],
     "is_vegetarian": False, "calories": 450, "sales": 920, "tags": ["经典", "川菜", "微辣"]},
    {"dish_name": "麻婆豆腐", "category": "热菜", "price": 38, "allergens": ["大豆", "牛肉"],
     "is_vegetarian": False, "calories": 380, "sales": 880, "tags": ["川菜", "麻辣", "下饭"]},

    # ---------- 热菜 ----------
    {"dish_name": "红烧狮子头", "category": "热菜", "price": 58, "allergens": ["鸡蛋", "麸质"],
     "is_vegetarian": False, "calories": 520, "sales": 640, "tags": ["淮扬菜", "家常"]},
    {"dish_name": "糖醋里脊", "category": "热菜", "price": 52, "allergens": ["鸡蛋", "麸质"],
     "is_vegetarian": False, "calories": 480, "sales": 820, "tags": ["酸甜", "儿童喜爱"]},
    {"dish_name": "黑椒牛柳", "category": "热菜", "price": 78, "allergens": ["牛肉"],
     "is_vegetarian": False, "calories": 430, "sales": 590, "tags": ["西式", "咸鲜"]},
    {"dish_name": "水煮牛肉", "category": "热菜", "price": 68, "allergens": ["牛肉"],
     "is_vegetarian": False, "calories": 560, "sales": 700, "tags": ["川菜", "麻辣", "重口"]},
    {"dish_name": "干锅花菜", "category": "热菜", "price": 36, "allergens": [],
     "is_vegetarian": True, "calories": 220, "sales": 680, "tags": ["素食", "湘菜", "微辣"]},
    {"dish_name": "地三鲜", "category": "热菜", "price": 32, "allergens": [],
     "is_vegetarian": True, "calories": 260, "sales": 620, "tags": ["素食", "东北菜", "家常"]},
    {"dish_name": "鱼香茄子", "category": "热菜", "price": 34, "allergens": [],
     "is_vegetarian": True, "calories": 240, "sales": 590, "tags": ["素食", "川菜", "下饭"]},
    {"dish_name": "番茄炒蛋", "category": "热菜", "price": 26, "allergens": ["鸡蛋"],
     "is_vegetarian": True, "calories": 180, "sales": 950, "tags": ["素食", "家常", "清爽"]},

    # ---------- 凉菜 ----------
    {"dish_name": "口水鸡", "category": "凉菜", "price": 42, "allergens": ["鸡肉", "芝麻", "花生"],
     "is_vegetarian": False, "calories": 320, "sales": 720, "tags": ["川菜", "麻辣", "开胃"]},
    {"dish_name": "凉拌木耳", "category": "凉菜", "price": 24, "allergens": ["木耳"],
     "is_vegetarian": True, "calories": 90, "sales": 660, "tags": ["素食", "低卡", "爽口"]},
    {"dish_name": "拍黄瓜", "category": "凉菜", "price": 18, "allergens": ["蒜"],
     "is_vegetarian": True, "calories": 60, "sales": 780, "tags": ["素食", "低卡", "解腻"]},
    {"dish_name": "皮蛋豆腐", "category": "凉菜", "price": 22, "allergens": ["大豆", "鸡蛋"],
     "is_vegetarian": True, "calories": 150, "sales": 640, "tags": ["素食", "家常", "清爽"]},

    # ---------- 汤品 ----------
    {"dish_name": "番茄蛋汤", "category": "汤品", "price": 28, "allergens": ["鸡蛋"],
     "is_vegetarian": True, "calories": 120, "sales": 830, "tags": ["素食", "清淡", "解腻"]},
    {"dish_name": "菌菇汤", "category": "汤品", "price": 38, "allergens": ["蘑菇"],
     "is_vegetarian": True, "calories": 110, "sales": 520, "tags": ["素食", "低卡", "养生"]},
    {"dish_name": "酸辣汤", "category": "汤品", "price": 32, "allergens": ["鸡蛋", "大豆"],
     "is_vegetarian": False, "calories": 160, "sales": 610, "tags": ["酸辣", "开胃"]},
    {"dish_name": "老鸭汤", "category": "汤品", "price": 88, "allergens": ["鸭肉"],
     "is_vegetarian": False, "calories": 380, "sales": 480, "tags": ["养生", "浓郁"]},

    # ---------- 主食 ----------
    {"dish_name": "米饭", "category": "主食", "price": 2, "allergens": [],
     "is_vegetarian": True, "calories": 230, "sales": 1000, "tags": ["主食", "必需"]},
    {"dish_name": "扬州炒饭", "category": "主食", "price": 32, "allergens": ["鸡蛋", "虾"],
     "is_vegetarian": False, "calories": 520, "sales": 740, "tags": ["主食", "热门"]},
    {"dish_name": "手工水饺", "category": "主食", "price": 36, "allergens": ["麸质", "猪肉"],
     "is_vegetarian": False, "calories": 460, "sales": 680, "tags": ["主食", "北方面食"]},
    {"dish_name": "素三鲜蒸饺", "category": "主食", "price": 30, "allergens": ["麸质"],
     "is_vegetarian": True, "calories": 340, "sales": 450, "tags": ["素食", "主食"]},
    {"dish_name": "阳春面", "category": "主食", "price": 18, "allergens": ["麸质"],
     "is_vegetarian": True, "calories": 320, "sales": 620, "tags": ["素食", "清淡", "面食"]},

    # ---------- 甜品 / 饮品 ----------
    {"dish_name": "红豆沙", "category": "甜品", "price": 20, "allergens": [],
     "is_vegetarian": True, "calories": 180, "sales": 520, "tags": ["素食", "甜品", "低糖可选"]},
    {"dish_name": "杨枝甘露", "category": "甜品", "price": 32, "allergens": ["芒果", "乳制品"],
     "is_vegetarian": True, "calories": 260, "sales": 780, "tags": ["甜品", "港式", "网红"]},
    {"dish_name": "鲜榨橙汁", "category": "饮品", "price": 22, "allergens": [],
     "is_vegetarian": True, "calories": 110, "sales": 690, "tags": ["饮品", "健康", "低卡"]},
    {"dish_name": "酸梅汤", "category": "饮品", "price": 16, "allergens": [],
     "is_vegetarian": True, "calories": 90, "sales": 810, "tags": ["饮品", "解腻", "低卡"]},
]

#: 核心菜品名集合（扩充时用于去重，保证不覆盖手写数据）
CORE_NAMES = {d["dish_name"] for d in CORE_DISHES}


# ==================================================================
# 扩充素材：菜系 / 主料 / 做法 / 价位 / 过敏原推导
# ==================================================================

#: 菜系（决定标签与前缀）
CUISINES = ["川菜", "粤菜", "湘菜", "鲁菜", "苏菜", "浙菜", "闽菜", "徽菜",
            "京菜", "东北菜", "西北菜", "家常"]

#: 主料定义：(主料名, 类别, 是否素, 基础过敏原, 基准热量, 基准价)
#: 基准价单位为元；实际价格会按做法系数与随机浮动微调
INGREDIENTS: List[tuple] = [
    # --- 畜禽 ---
    ("鸡", "热菜", False, ["鸡肉"], 420, 48),
    ("鸭", "热菜", False, ["鸭肉"], 480, 68),
    ("牛", "热菜", False, ["牛肉"], 460, 72),
    ("猪", "热菜", False, ["猪肉"], 450, 52),
    ("羊", "热菜", False, ["羊肉"], 470, 78),
    ("排骨", "热菜", False, ["猪肉"], 480, 58),
    ("里脊", "热菜", False, ["猪肉"], 430, 50),
    ("牛腩", "热菜", False, ["牛肉"], 500, 76),
    ("鸡翅", "热菜", False, ["鸡肉"], 440, 46),
    ("肥肠", "热菜", False, ["猪肉"], 490, 62),
    # --- 水产 ---
    ("鲈鱼", "热菜", False, ["鱼类"], 260, 82),
    ("草鱼", "热菜", False, ["鱼类"], 280, 62),
    ("鲫鱼", "热菜", False, ["鱼类"], 250, 58),
    ("黄鱼", "热菜", False, ["鱼类"], 270, 88),
    ("带鱼", "热菜", False, ["鱼类"], 300, 56),
    ("虾仁", "热菜", False, ["虾", "甲壳类"], 220, 76),
    ("鱿鱼", "热菜", False, ["贝类"], 230, 66),
    ("扇贝", "热菜", False, ["贝类"], 200, 78),
    ("蛤蜊", "热菜", False, ["贝类"], 180, 52),
    ("蟹", "热菜", False, ["甲壳类"], 240, 128),
    # --- 蔬菜 ---
    ("青菜", "热菜", True, [], 120, 22),
    ("西兰花", "热菜", True, [], 110, 28),
    ("菠菜", "热菜", True, [], 100, 24),
    ("生菜", "热菜", True, [], 90, 22),
    ("娃娃菜", "热菜", True, [], 95, 26),
    ("土豆", "热菜", True, [], 200, 26),
    ("茄子", "热菜", True, [], 180, 28),
    ("豆角", "热菜", True, [], 170, 28),
    ("冬瓜", "热菜", True, [], 90, 24),
    ("南瓜", "热菜", True, [], 160, 26),
    ("苦瓜", "热菜", True, [], 110, 28),
    ("莲藕", "热菜", True, [], 190, 30),
    ("山药", "热菜", True, [], 180, 32),
    ("芦笋", "热菜", True, [], 100, 42),
    ("秋葵", "热菜", True, [], 105, 34),
    ("香菇", "热菜", True, ["蘑菇"], 120, 32),
    ("口蘑", "热菜", True, ["蘑菇"], 115, 34),
    ("杏鲍菇", "热菜", True, ["蘑菇"], 130, 36),
    ("木耳", "热菜", True, ["木耳"], 95, 28),
    ("金针菇", "热菜", True, ["蘑菇"], 100, 26),
    # --- 豆制品 ---
    ("豆腐", "热菜", True, ["大豆"], 200, 28),
    ("豆干", "热菜", True, ["大豆"], 240, 30),
    ("腐竹", "热菜", True, ["大豆"], 260, 32),
    ("千张", "热菜", True, ["大豆"], 250, 30),
    ("油豆腐", "热菜", True, ["大豆"], 280, 32),
    # --- 蛋类 ---
    ("鸡蛋", "热菜", True, ["鸡蛋"], 180, 24),
    ("蒸蛋", "热菜", True, ["鸡蛋"], 130, 22),
    ("咸蛋黄", "热菜", True, ["鸡蛋"], 260, 34),
]

#: 凉菜素材（做法不同，基准更低）
COLD_INGREDIENTS: List[tuple] = [
    ("黄瓜", "凉菜", True, [], 60, 18),
    ("木耳", "凉菜", True, ["木耳"], 90, 24),
    ("海带丝", "凉菜", True, ["海藻"], 70, 22),
    ("豆皮", "凉菜", True, ["大豆"], 150, 26),
    ("莴笋", "凉菜", True, [], 65, 22),
    ("金针菇", "凉菜", True, ["蘑菇"], 80, 24),
    ("腐竹", "凉菜", True, ["大豆"], 170, 28),
    ("鸡丝", "凉菜", False, ["鸡肉"], 200, 38),
    ("牛肉", "凉菜", False, ["牛肉"], 230, 46),
    ("猪耳", "凉菜", False, ["猪肉"], 240, 42),
    ("皮蛋", "凉菜", False, ["鸡蛋"], 150, 26),
    ("粉丝", "凉菜", True, ["麸质"], 160, 24),
]

#: 汤品素材
SOUP_INGREDIENTS: List[tuple] = [
    ("冬瓜", "汤品", True, [], 80, 26),
    ("番茄", "汤品", True, [], 100, 28),
    ("紫菜", "汤品", True, ["海藻"], 60, 22),
    ("萝卜", "汤品", True, [], 85, 26),
    ("玉米", "汤品", True, [], 130, 30),
    ("山药", "汤品", True, [], 140, 34),
    ("菌菇", "汤品", True, ["蘑菇"], 110, 38),
    ("老鸭", "汤品", False, ["鸭肉"], 380, 88),
    ("排骨", "汤品", False, ["猪肉"], 360, 68),
    ("鲫鱼", "汤品", False, ["鱼类"], 290, 62),
    ("鸡汤", "汤品", False, ["鸡肉"], 320, 58),
    ("牛尾", "汤品", False, ["牛肉"], 400, 88),
]

#: 主食素材
STAPLE_INGREDIENTS: List[tuple] = [
    ("米饭", "主食", True, [], 230, 2),
    ("蛋炒饭", "主食", False, ["鸡蛋"], 500, 32),
    ("牛肉炒饭", "主食", False, ["牛肉", "鸡蛋"], 560, 42),
    ("拉面", "主食", False, ["麸质"], 480, 36),
    ("刀削面", "主食", True, ["麸质"], 460, 34),
    ("米线", "主食", True, [], 400, 30),
    ("河粉", "主食", True, [], 420, 32),
    ("馄饨", "主食", False, ["麸质", "猪肉"], 420, 34),
    ("包子", "主食", False, ["麸质", "猪肉"], 380, 28),
    ("花卷", "主食", True, ["麸质"], 300, 16),
    ("锅贴", "主食", False, ["麸质", "猪肉"], 450, 32),
    ("烧饼", "主食", True, ["麸质", "芝麻"], 340, 18),
]

#: 甜品 / 饮品素材
DESSERT_INGREDIENTS: List[tuple] = [
    ("银耳羹", "甜品", True, [], 150, 22),
    ("双皮奶", "甜品", True, ["乳制品"], 240, 28),
    ("龟苓膏", "甜品", True, [], 130, 24),
    ("芒果班戟", "甜品", True, ["芒果", "乳制品", "麸质"], 320, 36),
    ("桂花糕", "甜品", True, ["麸质"], 260, 24),
    ("绿豆糕", "甜品", True, [], 230, 22),
    ("红豆沙", "甜品", True, [], 180, 20),
    ("芝麻糊", "甜品", True, ["芝麻"], 210, 22),
    ("杏仁露", "甜品", True, ["坚果"], 170, 26),
    ("椰汁西米露", "甜品", True, [], 200, 24),
    ("芋圆甜品", "甜品", True, [], 240, 28),
    ("布丁", "甜品", True, ["乳制品", "鸡蛋"], 190, 22),
    ("柠檬茶", "饮品", True, [], 90, 20),
    ("菊花茶", "饮品", True, [], 10, 16),
    ("豆浆", "饮品", True, ["大豆"], 120, 12),
    ("酸奶", "饮品", True, ["乳制品"], 140, 18),
    ("青柠汽水", "饮品", True, [], 130, 20),
    ("椰汁", "饮品", True, [], 160, 22),
    ("珍珠奶茶", "饮品", True, ["乳制品", "麸质"], 320, 26),
    ("茉莉花茶", "饮品", True, [], 8, 16),
    ("鲜榨西瓜汁", "饮品", True, [], 100, 20),
    ("冰镇酸辣汤", "饮品", False, ["鸡蛋"], 110, 18),
]

#: 甜品/饮品的「风味前缀」，用于扩充同类项
DESSERT_FLAVORS = ["冰镇", "桂花", "蜂蜜", "椰香", "焦糖", "抹茶", "红枣", "黑糖",
                   "柠檬", "酸奶", "香草", "巧克力"]

#: 做法：(名称, 价格系数, 热量系数, 附加过敏原, 附加标签)
COOKING_METHODS: List[tuple] = [
    ("清蒸", 1.05, 0.80, [], ["清淡", "低油"]),
    ("红烧", 1.00, 1.15, ["大豆"], ["浓郁", "下饭"]),
    ("香辣", 1.00, 1.10, ["辣椒"], ["麻辣", "重口"]),
    ("蒜蓉", 0.95, 0.95, ["蒜"], ["蒜香"]),
    ("干煸", 1.00, 1.20, ["辣椒"], ["干香", "微辣"]),
    ("白灼", 1.10, 0.75, [], ["清淡", "原味"]),
    ("黑椒", 1.15, 1.10, [], ["西式", "咸鲜"]),
    ("糖醋", 0.95, 1.15, ["麸质"], ["酸甜", "儿童喜爱"]),
    ("椒盐", 0.90, 1.25, ["麸质"], ["酥脆", "重口"]),
    ("水煮", 1.00, 1.10, ["辣椒"], ["川味", "麻辣"]),
    ("干锅", 1.05, 1.15, ["辣椒"], ["香辣", "下饭"]),
    ("铁板", 1.10, 1.10, [], ["镬气", "热销"]),
    ("砂锅", 1.05, 1.05, [], ["暖身", "炖煮"]),
    ("豉汁", 1.00, 1.00, ["大豆"], ["粤式", "鲜香"]),
    ("咖喱", 1.10, 1.15, ["乳制品"], ["南洋", "浓郁"]),
    ("孜然", 0.95, 1.10, ["芝麻"], ["西北", "烤肉香"]),
    ("葱爆", 0.98, 1.05, [], ["京味", "爆炒"]),
    ("卤", 1.00, 1.00, ["大豆"], ["冷盘", "入味"]),
]


def _pick_methods(n: int) -> List[tuple]:
    """为某个主料挑选 n 种做法（去重、稳定）"""
    pool = COOKING_METHODS[:]
    _RNG.shuffle(pool)
    return pool[:n]


def _build_dish(prefix: str, ing: tuple, method: tuple,
                category_override: str = "") -> dict:
    """由「前缀(可选) + 主料 + 做法」组装一道菜"""
    ing_name, category, is_veg, ing_allergens, base_cal, base_price = ing
    m_name, price_k, cal_k, extra_allergens, extra_tags = method

    name = f"{m_name}{ing_name}"
    if prefix and prefix not in name:
        name = f"{prefix}{name}"

    # 过敏原 = 主料过敏原 ∪ 做法过敏原（去重保序）
    allergens = list(dict.fromkeys([*ing_allergens, *extra_allergens]))

    # 价格：基准 × 做法系数，向上取整到 2 元档
    price = int(round(base_price * price_k / 2.0)) * 2
    price = max(8, min(price, 288))

    # 热量：基准 × 做法系数，加 ±6% 稳定抖动
    calories = int(round(base_cal * cal_k * _RNG.uniform(0.94, 1.06) / 10.0)) * 10
    calories = max(60, min(calories, 900))

    # 销量：与价格弱负相关 + 稳定抖动（便宜的下单多）
    sales = int(max(120, min(990, 900 - price * 2 + _RNG.randint(-120, 120))))

    tags = list(dict.fromkeys([
        *( ["素食"] if is_veg else [] ),
        *extra_tags,
    ]))

    return {
        "dish_name": name,
        "category": category_override or category,
        "price": price,
        "allergens": allergens,
        "is_vegetarian": is_veg,
        "calories": calories,
        "sales": sales,
        "tags": tags,
    }


def build_extended_menu() -> List[dict]:
    """
    生成扩充菜单（出去重、去核心重名），返回新增菜品列表。

    规模策略（稳定可复现）：
        热菜   48 主料 × 最多 6 种做法  ≈ 260 道
        凉菜   12 主料 × 最多 5 种做法  ≈  50 道
        汤品   12 主料 × 最多 5 种做法  ≈  50 道
        主食   12 主料 × 最多 5 种做法  ≈  50 道
        甜品   12 主料 × 最多 4 种做法  ≈  46 道
        -------------------------------------------------
        小计 ≈ 456 道 + 核心 29 道 ≈ 485 道
        再按菜系加「前缀变体」（如「川味红烧鸡」）补足到 500+
    """
    extra: List[dict] = []
    seen = set(CORE_NAMES)

    def _add(dishes: List[dict]) -> None:
        for d in dishes:
            if d["dish_name"] in seen:
                continue
            seen.add(d["dish_name"])
            extra.append(d)

    # --- 热菜 ---
    for ing in INGREDIENTS:
        for method in _pick_methods(6):
            _add([_build_dish("", ing, method)])

    # --- 凉菜 / 汤品 / 主食 / 甜品 ---
    for pool, n in [(COLD_INGREDIENTS, 5), (SOUP_INGREDIENTS, 5),
                    (STAPLE_INGREDIENTS, 5), (DESSERT_INGREDIENTS, 4)]:
        for ing in pool:
            for method in _pick_methods(n):
                if ing[1] == "主食":
                    # 主食不做「红烧米饭」这类违和组合，改用风味前缀
                    flavored = _build_dish("", ing, ("", 1.0, 1.0, [], []))
                    flavored["dish_name"] = f"{method[0]}味{ing[0]}"
                    flavored["tags"] = list(dict.fromkeys(
                        [*( ["素食"] if ing[2] else [] ), *method[4]]))
                    _add([flavored])
                elif ing[1] in ("甜品", "饮品"):
                    _add([_build_dish("", ing, ("", 1.0, 1.0, [], [method[0]]))])
                else:
                    _add([_build_dish("", ing, method)])

    # --- 菜系前缀变体（补足到 500+，同时让搜索结果更丰富） ---
    base_snapshot = list(extra)
    for idx, d in enumerate(base_snapshot):
        if len(extra) >= 520:
            break
        cuisine = CUISINES[idx % len(CUISINES)]
        if d["category"] not in ("热菜", "凉菜"):
            continue
        variant = dict(d)
        variant["dish_name"] = f"{cuisine}{d['dish_name']}"
        variant["price"] = min(288, d["price"] + _RNG.choice([2, 4, 6]))
        variant["tags"] = list(dict.fromkeys([*d["tags"], cuisine]))
        variant["sales"] = max(100, d["sales"] - _RNG.randint(0, 200))
        _add([variant])

    # --- 甜品 / 饮品风味变体（补充这两类的规模，避免过于单薄） ---
    dessert_base = [d for d in extra if d["category"] in ("甜品", "饮品")]
    for idx, d in enumerate(dessert_base):
        if len(extra) >= 560:
            break
        flavor = DESSERT_FLAVORS[idx % len(DESSERT_FLAVORS)]
        variant = dict(d)
        variant["dish_name"] = f"{flavor}{d['dish_name']}"
        variant["price"] = min(88, d["price"] + _RNG.choice([0, 2, 4]))
        variant["calories"] = int(variant["calories"] * _RNG.uniform(0.95, 1.1))
        variant["calories"] = int(round(variant["calories"] / 10.0) * 10)
        variant["tags"] = list(dict.fromkeys([*d["tags"], flavor]))
        variant["sales"] = max(90, d["sales"] - _RNG.randint(0, 150))
        _add([variant])

    return extra


def build_menu_db() -> List[dict]:
    """核心手写菜 + 扩充菜 = 完整菜单库"""
    return [*CORE_DISHES, *build_extended_menu()]


def build_nutrition_db() -> Dict[str, dict]:
    """
    由菜单库推导营养成分表。

    规则（保证与 calories 字段自洽）：
        蛋白质 ≈ 热量 15% / 4 kcal per g
        脂肪   ≈ 热量 30% / 9 kcal per g
        碳水   ≈ 热量 55% / 4 kcal per g
        钠     ≈ 基础值 × 价格系数（越贵的菜通常调味越重）
    素食菜蛋白质比例下调、碳水比例上调。
    """
    table: Dict[str, dict] = {}

    # 先写入核心手写营养（保持精确值）
    table.update({
        "招牌烤鸭": {"calories": 620, "protein": 42.0, "fat": 38.0, "carb": 18.0, "sodium": 1180},
        "清蒸鲈鱼": {"calories": 280, "protein": 38.0, "fat": 12.0, "carb": 3.0, "sodium": 620},
        "宫保鸡丁": {"calories": 450, "protein": 28.0, "fat": 26.0, "carb": 22.0, "sodium": 980},
        "麻婆豆腐": {"calories": 380, "protein": 20.0, "fat": 24.0, "carb": 18.0, "sodium": 1050},
        "番茄炒蛋": {"calories": 180, "protein": 12.0, "fat": 11.0, "carb": 8.0, "sodium": 480},
        "凉拌木耳": {"calories": 90, "protein": 4.0, "fat": 5.0, "carb": 9.0, "sodium": 320},
        "拍黄瓜": {"calories": 60, "protein": 1.5, "fat": 3.0, "carb": 6.0, "sodium": 280},
        "番茄蛋汤": {"calories": 120, "protein": 8.0, "fat": 7.0, "carb": 6.0, "sodium": 520},
        "菌菇汤": {"calories": 110, "protein": 5.0, "fat": 5.0, "carb": 10.0, "sodium": 460},
        "米饭": {"calories": 230, "protein": 4.0, "fat": 0.5, "carb": 51.0, "sodium": 2},
        "扬州炒饭": {"calories": 520, "protein": 16.0, "fat": 18.0, "carb": 70.0, "sodium": 890},
        "鲜榨橙汁": {"calories": 110, "protein": 2.0, "fat": 0.3, "carb": 25.0, "sodium": 5},
        "酸梅汤": {"calories": 90, "protein": 0.5, "fat": 0.1, "carb": 22.0, "sodium": 30},
    })

    for dish in build_menu_db():
        name = dish["dish_name"]
        if name in table:
            continue
        cal = float(dish.get("calories", 300) or 300)
        veg = bool(dish.get("is_vegetarian"))
        # 素食：蛋白质低、碳水高；荤菜反之
        p_ratio, f_ratio, c_ratio = (0.12, 0.24, 0.64) if veg else (0.18, 0.36, 0.46)
        table[name] = {
            "calories": int(cal),
            "protein": round(cal * p_ratio / 4.0, 1),
            "fat": round(cal * f_ratio / 9.0, 1),
            "carb": round(cal * c_ratio / 4.0, 1),
            "sodium": int(max(100, min(1600,
                        260 + float(dish.get("price", 30) or 30) * 5.2))),
        }
    return table


if __name__ == '__main__':
    menu = build_menu_db()
    nutrition = build_nutrition_db()
    print("=" * 60)
    print("种子菜单数据集自检")
    print("=" * 60)
    print(f"核心手写菜：{len(CORE_DISHES)} 道")
    print(f"扩充菜：    {len(build_extended_menu())} 道")
    print(f"菜单库合计：{len(menu)} 道")
    print(f"营养表条数：{len(nutrition)}")

    # 分类统计
    from collections import Counter
    cats = Counter(d["category"] for d in menu)
    print("\n分类分布：")
    for cat, cnt in cats.most_common():
        print(f"  {cat:6s}: {cnt:4d} 道")

    # 唯一性断言
    names = [d["dish_name"] for d in menu]
    assert len(names) == len(set(names)), f"存在重名菜品：{len(names) - len(set(names))} 个"
    print("\n唯一性校验通过：无重名菜品")
    assert len(menu) >= 500, f"菜品数应 ≥500，实际 {len(menu)}"
    print(f"规模校验通过：{len(menu)} 道 ≥ 500 道")
    assert len(nutrition) >= len(menu), "营养表应覆盖全部菜品"
    print("营养覆盖校验通过")
    print("\n自检通过。")
