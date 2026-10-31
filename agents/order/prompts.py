"""
    OrderAgent 节点提示词：agents/order/prompts.py

    与 AGENT 结构对齐：Agent 专属提示词从 prompts/templates.py 下沉到各自包内。
    仅保留「节点级」提示词（直接由本 Agent 节点消费）；由工具层（tools/llm_tools.py）
    消费的 PARSE_ORDER / RECOMMEND_DISHES 等仍留在 prompts/templates.py 统一管理。
"""

ORDER_SUMMARY = """你是餐厅点餐服务助手。请把订单信息整理成顾客友好的确认摘要。

【顾客需求】
{customer_needs}

【推荐菜品与价格】
{order_detail}

【营养分析】
{nutrition}

【过敏原检测结果】
{allergen_result}

输出要求：
1. 用 Markdown 表格列出菜品清单（菜品/数量/单价/小计）；
2. 明确标注总价与人均；
3. 【必填】过敏原警示：若检测到过敏原或存在配料数据缺失，用 "> 过敏原警示：..." 引用块醒目标注，并说明后厨将如何隔离处理；
4. 营养建议用一句话概括（如荤素搭配、热量提示）；
5. 禁止编造未在输入中出现的菜品或价格；
6. **禁止使用任何 emoji / 表情符号**，保持专业文本风格。
"""
