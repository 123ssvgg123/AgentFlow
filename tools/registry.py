"""
    工具注册表：tools/registry.py

    职责：把 15 个工具（LLM 4 + 规则引擎 4 + MCP 4 + 外部 API 3）统一注册，
        供 LangGraph 节点按名调用，并对外提供工具清单（/api/tools 接口数据源）。

    设计：单一注册入口 + 按 Agent 归类，避免节点直接 import 散落的工具函数。
"""
from typing import Any, Callable, Dict, List

from base.logger import logger
# 四类工具
from tools.external_api import EXTERNAL_TOOLS
from tools.llm_tools import LLM_TOOLS
from tools.mcp_tools import MCP_TOOLS
from tools.rule_tools import RULE_TOOLS


class ToolRegistry:
    """
    工具注册表：15 个工具的统一入口。

    分类统计（对应需求文档 3.4）：
        LLM 工具      4 个
        规则引擎      4 个
        MCP 工具      4 个
        外部 API 封装 3 个
        合计          15 个
    """

    # 工具分类定义：{类别: (工具字典, 中文名)}
    CATEGORIES = {
        "llm": (LLM_TOOLS, "LLM 工具"),
        "rule": (RULE_TOOLS, "规则引擎"),
        "mcp": (MCP_TOOLS, "MCP 工具"),
        "external": (EXTERNAL_TOOLS, "外部 API 封装"),
    }

    # 工具归属 Agent（用于 /api/tools 展示与权限控制）
    AGENT_OWNERSHIP = {
        "llm_classify_intent": "OrderAgent",
        "llm_recommend_dishes": "OrderAgent",
        "llm_sentiment_analysis": "ReviewAgent",
        "llm_generate_improvement": "ReviewAgent",
        "allergen_detection": "OrderAgent",
        "price_calculation": "OrderAgent",
        "inventory_calculation": "KitchenAgent",
        "priority_calculation": "KitchenAgent",
        "search_menu_knowledge": "OrderAgent",
        "query_food_safety_law": "ReviewAgent / SafetyAgent",
        "query_nutrition": "OrderAgent / SafetyAgent",
        "web_search": "ReviewAgent / SafetyAgent",
        "get_order_detail": "OrderAgent / KitchenAgent",
        "get_ingredient_inventory": "KitchenAgent",
        "get_review_data": "ReviewAgent",
    }

    # 工具功能说明（供前端工具清单展示）
    DESCRIPTIONS = {
        "llm_classify_intent": "用 LLM 识别顾客点餐意图（人数/口味/过敏/预算）",
        "llm_recommend_dishes": "根据顾客需求智能推荐菜品组合（含推荐理由）",
        "llm_sentiment_analysis": "对顾客评价做情感分析（好评/中评/差评 + 强度）",
        "llm_generate_improvement": "根据评价分析结果生成改进建议报告",
        "allergen_detection": "检测菜品过敏原（预设过敏原库 + 配方精确匹配）",
        "price_calculation": "计算订单总价（折扣/套餐/会员价/服务费）",
        "inventory_calculation": "计算食材需求量并对比当前库存",
        "priority_calculation": "计算订单优先级（5 维加权打分）",
        "search_menu_knowledge": "菜品知识库检索（本地关键词检索）",
        "query_food_safety_law": "食品安全法规知识库查询",
        "query_nutrition": "查询菜品营养成分",
        "web_search": "联网搜索（餐饮趋势/竞品动态）",
        "get_order_detail": "查询订单详情（对接甲方 POS 系统）",
        "get_ingredient_inventory": "查询食材库存（对接食材管理系统）",
        "get_review_data": "采集多平台评价（美团/大众点评/抖音）",
    }

    def __init__(self):
        # 合并全部工具到单一字典
        self._tools: Dict[str, Callable] = {}
        for _, (tools, _name) in self.CATEGORIES.items():
            self._tools.update(tools)
        logger.info(f"工具注册表初始化完成，共注册 {len(self._tools)} 个工具")

    def get(self, name: str) -> Callable:
        """按名获取工具函数（不存在时抛 KeyError，便于尽早暴露拼写错误）"""
        if name not in self._tools:
            raise KeyError(f"未注册的工具：{name}（可用：{list(self._tools.keys())}）")
        return self._tools[name]

    def has(self, name: str) -> bool:
        """判断工具是否已注册"""
        return name in self._tools

    def all_tools(self) -> Dict[str, Callable]:
        """返回全部工具的副本"""
        return dict(self._tools)

    def count(self) -> Dict[str, int]:
        """按类别统计工具数量（用于自检与 /api/tools）"""
        return {name: len(tools) for _, (tools, name) in self.CATEGORIES.items()}

    def describe(self) -> List[Dict[str, Any]]:
        """返回工具清单（供 /api/tools 接口与前端展示）"""
        result = []
        for category, (tools, category_name) in self.CATEGORIES.items():
            for tool_name in tools:
                result.append({
                    "name": tool_name,
                    "category": category,
                    "category_name": category_name,
                    "agent": self.AGENT_OWNERSHIP.get(tool_name, "-"),
                    "description": self.DESCRIPTIONS.get(tool_name, ""),
                })
        return result


# ==================== 全局单例 ====================
tool_registry = ToolRegistry()


if __name__ == '__main__':
    print("=" * 60)
    print("工具注册表自检")
    print("=" * 60)
    counts = tool_registry.count()
    for name, cnt in counts.items():
        print(f"  {name:10s}: {cnt} 个")
    print(f"  合计：{sum(counts.values())} 个（需求要求 15 个）")
    assert sum(counts.values()) == 15, f"工具总数应为 15，实际 {sum(counts.values())}"
    print("\n工具清单：")
    for item in tool_registry.describe():
        print(f"  [{item['category_name']}] {item['name']:28s} → {item['agent']}")
    print("\n自检通过：15 个工具全部注册。")
