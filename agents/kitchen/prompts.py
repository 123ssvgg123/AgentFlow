"""
    KitchenAgent 节点提示词：agents/kitchen/prompts.py

    与 AGENT 结构对齐：Agent 专属节点级提示词从 prompts/templates.py 下沉到包内。
"""

KITCHEN_PARSE_ORDER = """你是后厨生产计划专家。请把订单转换为后厨制作任务单。

【待处理订单】
{orders}

【后厨工位】
{stations}

请严格输出以下 JSON（不要任何解释文字）：
{{
  "tasks": [
    {{
      "order_id": "订单号",
      "table_no": "桌号",
      "dish_name": "菜品名称",
      "station": "建议工位",
      "estimated_minutes": 预计制作耗时（分钟，数字）,
      "ingredients": ["所需食材"],
      "special_note": "特殊要求（过敏原隔离等）"
    }}
  ]
}}
"""
