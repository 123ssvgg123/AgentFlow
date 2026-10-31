# 工具名对照表（文档 → 代码）

> 生成时间：本轮审计修订
> 权威来源：`FoodAgent/tools/registry.py`（注册表键名 = 代码里唯一可用的工具名）
> 说明：需求文档（`09-餐饮行业-智能餐饮Agent.html`）在**节点表格**里使用了一组「语义化命名」，
> 与 `tools/registry.py` 的**注册键名**不完全一致。代码侧一律以注册表为准；
> 本文档给出映射关系，避免后续按文档写代码时出现 `KeyError: 未注册的工具`。

---

## 一、总体结论

| 项目 | 数量 | 说明 |
| --- | --- | --- |
| 注册表实际工具数 | **15** | `registry.py` 底部 `assert sum(counts.values()) == 15` 强校验 |
| 文档语义名中**不存在**于注册表的 | **4** | `order_validation` / `assign_calculation` / `llm_parse_order` / `llm_generate_recommendation` |
| 处理方式 | — | **不改注册表**（15 个是需求硬指标），改文档/注释对齐 → 已在本轮完成 |

> 为什么不能直接把注册表改成文档名：需求文档明确要求「LLM 工具 4 + 规则引擎 4 + MCP 4 + 外部 API 3 = 15」，
> 而 `order_validation`、`assign_calculation` 属于**新增工具**而非改名，会突破 15 个的硬约束。
> 因此正确处理是：**文档语义名 → 声明为「节点内规则校验」或映射到已有工具**。

---

## 二、逐项对照

### 2.1 文档语义名 → 实际实现

| 文档中的名称 | 出现位置 | 实际实现 | 类型 | 处理方式 |
| --- | --- | --- | --- | --- |
| `llm_parse_order` | KitchenAgent 表格「generate_tasks」 | `llm_classify_intent` | LLM 工具 | **改名对齐**：任务单生成复用意图分类工具的结构化输出能力 |
| `llm_generate_recommendation` | OrderAgent 表格「generate_summary」 | `llm_recommend_dishes` | LLM 工具 | **改名对齐**：摘要改写复用推荐工具；纯规则拼装走 `menu_data` |
| `assign_calculation` | KitchenAgent 表格「assign_stations」 | `priority_calculation` | 规则引擎 | **改名对齐**：工位分配直接复用优先级计算的多维打分结果 |
| `order_validation` | OrderAgent 表格「validate_order」 | `allergen_detection`（复检）+ 节点内断言 | 规则引擎 | **语义归并**：不新增工具，归入节点内规则校验 |
| `get_store_info` | KitchenAgent「load_orders」 | `list_pending_orders` | 外部 API | **改名对齐**：门店信息由订单列表接口附带返回 |
| `sync_kitchen_display` | KitchenAgent「dispatch_orders」 | `sync_kitchen_display` ✓ | 外部 API | 名称一致，仅注册表未收录（属**外部 API 函数**而非注册工具） |
| `sync_pos_order` | OrderAgent「submit_order」 | `sync_pos_order` ✓ | 外部 API | 名称一致，同上 |

### 2.2 注册表 15 个工具（权威清单）

| # | 类别 | 工具名 | 归属 Agent | 功能 |
| --- | --- | --- | --- | --- |
| 1 | LLM 工具 | `llm_classify_intent` | OrderAgent | 识别点餐意图（人数/口味/过敏/预算） |
| 2 | LLM 工具 | `llm_recommend_dishes` | OrderAgent | 智能推荐菜品组合（含推荐理由） |
| 3 | LLM 工具 | `llm_sentiment_analysis` | ReviewAgent | 评价情感分析（好/中/差 + 强度） |
| 4 | LLM 工具 | `llm_generate_improvement` | ReviewAgent | 生成改进建议报告 |
| 5 | 规则引擎 | `allergen_detection` | OrderAgent | 检测菜品过敏原（过敏原库 + 配方匹配） |
| 6 | 规则引擎 | `price_calculation` | OrderAgent | 计算订单总价（折扣/套餐/会员价/服务费） |
| 7 | 规则引擎 | `inventory_calculation` | KitchenAgent | 计算食材需求量并对比库存 |
| 8 | 规则引擎 | `priority_calculation` | KitchenAgent | 计算订单优先级（5 维加权打分） |
| 9 | MCP 工具 | `search_menu_knowledge` | OrderAgent | 菜品知识库检索（内置菜品库关键词检索，可换向量检索） |
| 10 | MCP 工具 | `query_food_safety_law` | ReviewAgent / SafetyAgent | 食品安全法规知识库查询（内置法规库） |
| 11 | MCP 工具 | `query_nutrition` | OrderAgent / SafetyAgent | 查询菜品营养成分（内置营养表） |
| 12 | MCP 工具 | `web_search` | ReviewAgent / SafetyAgent | 联网搜索（餐饮趋势/竞品动态，失败走 24h 缓存） |
| 13 | 外部 API | `get_order_detail` | OrderAgent / KitchenAgent | 查询订单详情（对接甲方 POS） |
| 14 | 外部 API | `get_ingredient_inventory` | KitchenAgent | 查询食材库存（对接食材系统） |
| 15 | 外部 API | `get_review_data` | ReviewAgent | 采集多平台评价（美团/点评/抖音） |

---

## 三、非注册表的外部 API 函数（在 `tools/external_api.py`，不进 registry）

这些函数被节点直接 `import` 调用，**不占用 15 个工具的配额**：

| 函数名 | 调用节点 | 用途 |
| --- | --- | --- |
| `list_pending_orders` | KitchenAgent.load_orders | 拉取待处理订单列表 |
| `sync_pos_order` | OrderAgent.submit_order | 下单同步到 POS |
| `sync_kitchen_display` | KitchenAgent.dispatch_orders | 下发任务到 KDS 屏 |
| `get_store_info` | KitchenAgent.load_orders | 门店信息（工位/产能） |

---

## 四、本轮同步修改的代码注释

| 文件 | 修改内容 |
| --- | --- |
| `agents/order_agent.py` | 模块头节点表：8 个 → **9 个**（补 `require_manual_review`）；工具名改为注册表键名 |
| `agents/kitchen_agent.py` | 模块头节点表：工具名改为注册表键名 |
| `agents/kitchen_agent.py` | `degraded_tools` 记录名：`llm_parse_order` → `llm_classify_intent(task_parse)` |

---

## 五、后续新增工具时的规则

1. 新工具必须同时更新 `CATEGORIES` / `AGENT_OWNERSHIP` / `DESCRIPTIONS` 三处；
2. 数量变化必须同步改 `registry.py` 底部的 `assert sum(counts.values()) == N`；
3. 若确实需要 `order_validation` 这类「独立校验工具」，应先评审是否突破需求 15 个约束，
   否则应实现为**节点内规则函数**（如本轮 `validate_order` 的过敏原复检）。
