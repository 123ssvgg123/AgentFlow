"""
    提示词模板：prompts/templates.py

    职责：集中管理所有 Prompt 模板，与业务代码解耦。

    设计要点（对应需求文档各工具与节点的能力要求）：
        1. 结构化输出优先 —— 意图解析、需求解析、情感分析一律要求 JSON，便于程序消费
        2. 餐饮领域知识注入 —— 情感分析需识别「一般般」「凑合」等餐饮语境口语
        3. 食品安全硬约束 —— 推荐与摘要必须显式标注过敏原，禁止遗漏
        4. 不幻觉 —— 菜品推荐必须基于检索到的菜品库，禁止编造菜品
"""
from typing import Dict, List

from base.config import config


class Prompts:
    """Prompt 模板集合（全部为纯字符串，便于单元测试与热替换）"""

    # ==================== LLM 工具提示词 ====================

    # 1. llm_classify_intent —— 解析顾客点餐需求
    PARSE_ORDER = """你是连锁餐饮集团的点餐需求解析专家。请从顾客的自然语言中提取结构化点餐需求。

【顾客原话】
{order_text}

【上下文】
{context}

请严格输出以下 JSON（不要任何解释文字，缺失字段填默认值）：
{{
  "guest_count": 人数（整数，无法判断填 0）,
  "taste_preferences": ["口味偏好列表，如 清淡/麻辣/酸甜/咸鲜"],
  "dietary_restrictions": ["饮食限制列表，如 素食/清真/低卡/低糖/无麸质"],
  "allergens": ["顾客明确声明的过敏原列表，如 花生/海鲜/麸质"],
  "budget_per_person": 人均预算（数字，单位元，无法判断填 0）,
  "budget_total": 总预算（数字，无法判断填 0）,
  "occasion": "用餐场景，如 家庭聚餐/商务宴请/朋友聚会/日常用餐",
  "special_requests": ["其他特殊要求列表"],
  "mentioned_dishes": ["顾客明确点名的菜品列表"]
}}
"""

    # 2. llm_recommend_dishes —— 菜品组合推荐
    RECOMMEND_DISHES = """你是资深餐饮搭配顾问，为连锁餐厅顾客推荐菜品组合。

【顾客需求】
{customer_needs}

【可选菜品库】（只能从这里选，禁止编造菜品）
{menu_data}

【推荐要求】
1. 必须严格满足顾客的饮食限制与过敏原规避要求，含过敏原的菜品一律排除；
2. 荤素搭配合理、冷热菜兼顾、口味层次丰富；
3. 推荐数量控制在 {top_k} 道左右（含主食与汤品）；
4. 每道菜给出推荐理由，且必须说明该菜是否含过敏原。

请严格输出以下 JSON（不要任何解释文字）：
{{
  "dishes": [
    {{
      "dish_name": "菜品名称（必须与菜品库一致）",
      "category": "凉菜/热菜/汤品/主食/甜品/饮品",
      "price": 单价数字,
      "quantity": 推荐份数,
      "reason": "推荐理由（20字内）",
      "allergens": ["该菜含有的过敏原"],
      "is_vegetarian": true/false,
      "calories": 估算热量数字
    }}
  ],
  "combination_notes": "整体搭配说明（含荤素比例、口味平衡）",
  "vegetarian_arrangement": "若有素食者，说明如何单独安排"
}}
"""

    # 3. llm_sentiment_analysis —— 评价情感分析
    SENTIMENT_ANALYSIS = """你是餐饮行业顾客评价分析专家。请对以下顾客评价做情感分析。

【评价来源平台】{platform}
【评价原文】
{review_text}

餐饮语境识别要点（重要）：
- "一般般""凑合""还行""就那样" 在餐饮评价中属于【偏负面】表达，不要误判为好评；
- 同时包含好评与差评时，按负面内容判定整体倾向（如"菜好吃但服务差"应为 negative）；
- 关注食品安全类关键词（异物/变质/腹泻/拉肚子/不新鲜），一旦出现必须标记 high 严重度。

请严格输出以下 JSON（不要任何解释文字）：
{{
  "sentiment": "positive/neutral/negative",
  "score": 情感强度（-1.0 到 1.0 的浮点数，-1 极度负面，1 极度正面）,
  "emotions": ["识别到的情绪列表，如 满意/失望/愤怒/惊喜"],
  "keywords": ["关键描述词列表"],
  "is_food_safety": true/false（是否涉及食品安全问题）,
  "severity": "low/medium/high"（严重程度）
}}
"""

    # 4. llm_generate_improvement —— 改进建议生成
    GENERATE_IMPROVEMENT = """你是连锁餐饮集团的运营顾问。请根据评价分析数据，为门店生成可执行的改进建议报告。

【门店信息】
{store_info}

【评价分析数据】
{analysis_data}

【输出要求】
1. 报告结构：数据概览 → 关键问题排名（按提及次数降序）→ 分项改进建议 → 优先级行动清单；
2. 每条建议必须可落地（明确「做什么、谁来做、怎么衡量」），禁止空话；
3. 若涉及食品安全问题，必须单独列出合规整改项并说明法规依据；
4. 用 Markdown 输出，不要输出 JSON；
5. **禁止使用任何 emoji / 表情符号**，保持专业文本风格。
"""

    # ==================== Agent 节点提示词 ====================
    # 说明：节点级提示词（ORDER_SUMMARY / KITCHEN_PARSE_ORDER / NEGATIVE_REPLY）
    # 已随 Agent 结构重构下沉到各 Agent 子包：
    #     agents/order/prompts.py    → ORDER_SUMMARY
    #     agents/kitchen/prompts.py  → KITCHEN_PARSE_ORDER
    #     agents/review/prompts.py   → NEGATIVE_REPLY
    # 此处仅保留被 tools/llm_tools.py 等工具层复用的提示词。

    # ==================== Orchestrator 提示词 ====================

    ORCHESTRATOR_ROUTE = """你是餐饮智能助手系统的调度中枢（Orchestrator）。请识别用户意图并决定路由。

【用户输入】
{user_input}

【历史对话摘要】
{history_summary}

【支持的 9 类意图】
1. dish_recommend     —— 菜品推荐（按人数/口味/预算推荐菜品组合）  → OrderAgent
2. smart_order        —— 智能点餐（解析需求并完成下单）              → OrderAgent
3. allergen_query     —— 过敏原查询（问某菜是否含某过敏原）          → OrderAgent
4. kitchen_dispatch   —— 后厨调度（排产、出餐顺序）                  → KitchenAgent
5. inventory_check    —— 食材库存检查（某食材够不够、快用完了）      → KitchenAgent
6. review_analysis    —— 评价分析（分析顾客评价、好评率、差评原因）  → ReviewAgent
7. improvement_advice —— 改进建议（针对问题要改进方案）              → ReviewAgent
8. food_safety_query  —— 食安法规问答（卫生规范/冷藏温度/留样消毒/事故处置等） → SafetyAgent
9. nutrition_query    —— 营养健康问答（热量/蛋白质/脂肪/健康饮食等）  → SafetyAgent

【判定原则】
- 若用户一句话里包含多个诉求（如"从推荐到评价全流程处理"），则判定为多 Agent 串联 Pipeline；
- 无法明确归类时，默认 dish_recommend。

请严格输出以下 JSON（不要任何解释文字）：
{{
  "intents": ["识别到的意图列表，按执行顺序排列"],
  "primary_intent": "主意图（列表第一项）",
  "is_pipeline": true/false（是否多 Agent 串联）,
  "reason": "路由理由（30字内）"
}}
"""

    # ==================== 前端展示用：快捷问题 ====================

    @staticmethod
    def quick_questions() -> List[Dict[str, str]]:
        """通用快捷问题（无角色信息时的兜底，需求文档 4.1「3-5 个预设按钮」）"""
        return [
            {"icon": "utensils", "text": "推荐今日招牌", "prompt": "帮我推荐今天的招牌菜组合"},
            {"icon": "leaf", "text": "推荐低卡套餐", "prompt": "帮我推荐一份低卡套餐，两人份"},
            {"icon": "clock", "text": "查看后厨排队", "prompt": "现在后厨有多少单在排队，帮我安排出餐顺序"},
            {"icon": "alert", "text": "食材库存预警", "prompt": "哪些食材快用完了，给我预警"},
            {"icon": "chart", "text": "分析本周评价", "prompt": "帮我分析一下本周的顾客评价"},
        ]

    @staticmethod
    def quick_questions_by_role() -> Dict[str, List[Dict[str, str]]]:
        """
        按角色给出的快捷问题（需求文档 4.1「不同角色的使用差异」）。

        与 quick_questions() 的区别：
            · quick_questions()          —— 通用固定列表，无角色信息时兜底；
            · quick_questions_by_role()  —— 按登录角色返回**可直接发送的完整自然句**，
              前端据 user.role 选取，使首页按钮贴合该角色的真实职责。

        prompt 字段是点下去就直接发给 Agent 的语句，因此写成完整的口语化表达
        （而非「推荐菜品组合」这类短标签），无需用户二次补充信息。
        """
        return {
            # 服务员：智能点餐、菜品推荐、过敏原查询（第一线面对顾客）
            "waiter": [
                {"icon": "utensils", "text": "给顾客推荐菜品",
                 "prompt": "帮 3 号桌的顾客推荐几道招牌菜，口味偏清淡一些"},
                {"icon": "leaf", "text": "推荐低卡套餐",
                 "prompt": "帮我推荐一份低卡套餐，两人份"},
                {"icon": "alert", "text": "查过敏原",
                 "prompt": "这道宫保鸡丁里有哪些过敏原？对花生过敏的顾客能吃吗"},
                {"icon": "book", "text": "查菜品信息",
                 "prompt": "帮我查一下麻婆豆腐的做法的口味特点，顾客问起来我好介绍"},
                {"icon": "box", "text": "提交顾客订单",
                 "prompt": "帮我把 3 号桌刚点好的订单提交后厨"},
            ],
            # 后厨主管：后厨调度、排产管理、食材监控
            "kitchen": [
                {"icon": "clock", "text": "查看后厨排队",
                 "prompt": "现在后厨有多少单在排队，帮我安排出餐顺序"},
                {"icon": "alert", "text": "食材库存预警",
                 "prompt": "哪些食材快用完了，给我预警"},
                {"icon": "box", "text": "检查食材余量",
                 "prompt": "帮我查一下牛肉和青椒还有多少库存，够不够今晚用"},
                {"icon": "utensils", "text": "调整出餐优先级",
                 "prompt": "8 号桌催单了，帮我把他们的菜提到最前面做"},
                {"icon": "chart", "text": "看出品效率",
                 "prompt": "帮我统计一下今天各菜品的平均出餐时间"},
            ],
            # 店长：评价分析、经营数据、决策支持
            "manager": [
                {"icon": "chart", "text": "分析本周评价",
                 "prompt": "帮我分析一下本周的顾客评价"},
                {"icon": "alert", "text": "处理差评",
                 "prompt": "最近有一条差评说上菜太慢，帮我拟一份回复"},
                {"icon": "trend", "text": "看经营概况",
                 "prompt": "帮我汇总一下本周的经营情况，有哪些需要改进的地方"},
                {"icon": "utensils", "text": "优化菜单结构",
                 "prompt": "根据本周的销售数据，帮我分析哪些菜品该主推、哪些该下架"},
                {"icon": "book", "text": "查合规要求",
                 "prompt": "帮我查一下食材留样和冷藏温度的合规要求"},
            ],
        }

    @staticmethod
    def role_hints() -> Dict[str, List[str]]:
        """按角色给出引导语标签（保留：供接口文档/自检展示用短标签列表）"""
        return {
            "waiter": ["推荐菜品组合", "确认过敏原", "查询菜品信息", "提交订单"],
            "kitchen": ["后厨排产", "食材库存检查", "调整优先级", "出品监控"],
            "manager": ["评价分析", "经营数据", "改进建议", "差评处理"],
        }


if __name__ == '__main__':
    print("提示词模板自检：")
    print("  快捷问题条数：", len(Prompts.quick_questions()))
    print("  角色数：", list(Prompts.role_hints().keys()))
    print("  过敏原前缀：", config.ALLERGEN_ALERT_PREFIX)
