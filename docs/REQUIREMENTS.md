# FoodAgent · AI 智能餐饮 Agent 系统 · 需求基线

> 来源：原始需求文档 `09-餐饮行业-智能餐饮Agent.html`（AgenticWorkflow-EduAgent-V7.5）**不在本机**
> 需求原文来源：`D:\heima\需求对照核查报告.md` 第一章「FoodAgent 逐条对照」各表格的「需求」列（逐条摘录，未改写）
> 实现侧来源：`D:\heima\FoodAgent` 源码逐文件核对（见文末「核查方法说明」）
> 状态：**反推基线**。由「报告摘录的需求原文 + 代码实现事实」双向对齐而成，作为验收追溯依据；原始 HTML 归位后应回填复核
> 项目名：FoodAgent · AI 智能餐饮 Agent 系统（LangGraph 状态图 + Orchestrator 编排器）

---

## 一、业务目标

某连锁餐饮集团的门店经营场景下，把「顾客点餐 → 后厨出餐 → 顾客评价」整条链路交由 Agent 系统处理：点餐要吃得安全（过敏原零漏检）、后厨要出得快（排产有序）、评价要改得准（差评归因与改进建议）。下列目标全部取自核查报告的需求侧表述，并给出**可核查**的量化指向。

| 编号 | 目标 | 量化指向 |
|------|------|----------|
| G1 | 用 4 个专业 Agent 覆盖点餐 / 后厨 / 评价 / 食安问答四条业务线 | **4 个 Agent = 33 节点**（OrderAgent 10 + KitchenAgent 7 + ReviewAgent 7 + SafetyAgent 9） |
| G2 | 工具按「LLM / 规则引擎 / MCP / 外部 API」四类分工，精确任务不交给 LLM | **15 个工具**（LLM 4 + 规则引擎 4 + MCP 4 + 外部 API 3），`registry.py` 断言 `sum == 15` |
| G3 | 自然语言请求自动识别为餐饮意图并路由到正确 Agent | **9 类意图**（`INTENT_ROUTES` 9 类，名称逐一对齐；含 `food_safety_query` / `nutrition_query`） |
| G4 | 对上层系统提供完整可对接的 RESTful 能力面 | **18 个业务接口**（另有元信息接口，见 4.3 节口径说明） |
| G5 | 涉敏订单必经食品安全审查，并**口头告知**顾客后留痕 3 年备查 | 留痕保留 **3 年**；`check_allergens →（涉敏时）confirm_allergen_notice → check_nutrition → calculate_price → validate_order` 强制链路 |
| G6 | 多门店、多员工共用一套系统而不串数据 | **`tenant_id + user_id` 双层隔离**（门店级 + 员工级） |
| G7 | 高峰期多桌并发下单时系统稳定不崩 | Locust **500 并发**压测脚本（`scripts/locustfile.py`） |
| G8 | 关键节点需人工确认（Human-in-the-Loop），不让 Agent 自行放行高风险动作 | KitchenAgent `check_priority`、ReviewAgent `check_negative` 均用 LangGraph `interrupt` |
| G9 | 长期运行后存储不无界增长，且满足合规归档要求 | 记忆清理已实现：对话 **90 天**（口径见 4.4 注）/ 门店偏好与顾客画像**永久** / 评价分析 **730 天（2 年）** / 合规留痕 **3 年** |
| G10 | 主模型不可用时业务不中断 | LLM **备用模型切换**已实现（主模型重试耗尽 → 备用模型再试一次） |
| G11 | 可观测、可部署、可扩缩容 | LangSmith / Locust / Prometheus 三态开关 + `Dockerfile` + `docs/deploy/`（compose 与 K8s 清单） |

> **口径说明（勿美化）**：上表「量化指向」中的 **4 / 33 / 15 / 9 / 18 / 3 年 / 500 并发 / 90·730 天**均可在代码或报告中直接核到。
> 而**峰值并发数、平均响应时间、缓存命中率**这类运行期性能指标，核查报告对 FoodAgent **未给出需求侧数值**，本基线**不填、不估**（见「待核实事项」）。

## 二、用户角色

角色口径取自代码：`api/auth.py` 的 `DEMO_ACCOUNTS` 使用 `role = waiter | kitchen | manager`；`agents/state.py` 的 `BaseState.role` 字段注释同为 `waiter / kitchen / manager`。内置演示账号共 **4 个**，密码均为 `123456`。

| 角色（代码值） | 演示账号 | 门店 | 核心诉求 |
|------|----------|------|----------|
| 服务员（`waiter`） | `waiter_01`（张服务）/ `waiter_02`（李服务） | `store_001` / `store_002` | 智能点餐、菜品推荐、过敏原查询；对涉敏订单执行口头告知并确认 |
| 后厨主管（`kitchen`） | `kitchen_01`（王主厨） | `store_001` | 后厨调度、排产管理、食材库存监控；高峰期确认或调整优先级 |
| 店长（`manager`） | `manager_01`（赵店长） | `store_001` | 评价分析、经营数据、决策支持；严重差评审核回复 |

- 登录方式：门店工号 + 密码 → 签发 JWT（`POST /api/auth/login`），Token 内携带 `tenant_id / user_id / role`（`api/auth.py`，PBKDF2-HMAC-SHA256 加盐哈希）。
- 前端按角色下发不同快捷问题：`GET /api/config` 返回 `quick_by_role` 与 `role_hints`（`prompts/templates.py` 的 `quick_questions_by_role()` / `role_hints()`）。

> 需求 4.1 提及「不同角色的使用差异」，代码以「**按角色分发的快捷问题 + `role` 字段贯穿 State**」落地；
> **未实现接口级 RBAC**——所有角色均可调用全部 18 个接口，隔离仅靠 `tenant_id`（见「范围界定」）。

## 三、核心流程

### ① 意图路由 → 单 Agent 直达 / 多 Agent Pipeline

实现位置：`agents/orchestrator.py`（`classify_intent()` → `_rule_classify()` → `run()` / `run_stream()`）。

```
用户输入（自然语言）
  → 意图识别（三级）：
      ① 规则快路径：短查询（≤40 字）+ 强信号正则 + 无复合标记词 → 直接判定（<1ms）
      ② 结果缓存：intent:{tenant}:{md5(query)}，TTL 600s（Redis 优先 / 进程内兜底）
      ③ LLM 识别：Prompts.ORCHESTRATOR_ROUTE → llm_client.chat_json
      ④ 规则兜底：LLM 不可用 / 返回非法 → INTENT_KEYWORDS（9 组关键词命中，_rule_classify）
  → 意图合法性校验（仅保留 config.INTENT_ROUTES 中的 9 类）
  → 是否 Pipeline？ is_pipeline = 命中跨多个 Agent 的意图
      ├── 否 → 单 Agent 直达：order / kitchen / review / safety 之一
      └── 是 → 多 Agent 串联：OrderAgent → KitchenAgent → ReviewAgent → SafetyAgent
                 （前一 Agent 的 State 作为后一个的初始输入，
                   上下文由 Orchestrator 的 context_extractors 声明式转交）
  → 结果汇总（answer / intents / degraded_tools / processing_time）
  → 返回：POST /api/chat（一次性）或 GET /api/chat/stream（SSE 逐字）
```

### ② 点餐 → 后厨 → 评价 全链路

```
[OrderAgent 10 节点]
  parse_order → recommend_dishes → check_allergens
    ├─ 无过敏原风险 ───────────────────────────────→ check_nutrition
    └─ 有风险（allergen_risk ∈ high/medium）→ confirm_allergen_notice → check_nutrition
  check_nutrition → calculate_price → validate_order
    ├─ order_valid=True ─────────────────→ generate_summary → submit_order（写 POS + 下发 KDS）
    ├─ 不通过且 recommend_retry ≤ 2 ─────→ recommend_dishes（回环换菜）
    └─ 不通过且已超上限 ─────────────────→ require_manual_review（终态拦截，不下单）
                                            ↓ 状态写入 order_status_log

[KitchenAgent 7 节点]
  load_orders → check_inventory
    ├─ 库存充足 / 已达重试上限(1) ────────→ generate_tasks
    └─ 库存不足且未重试 ─────────────────→ load_orders（标记缺料后重载）
  generate_tasks → schedule_tasks → check_priority
    ├─ 主管要求调整且未应用 ─────────────→ schedule_tasks（重新排产）
    └─ 主管确认无需调整 ─────────────────→ assign_stations → dispatch_orders（下发 KDS）

[ReviewAgent 7 节点]
  collect_reviews → classify_sentiment → extract_topics
    ├─ involves_food_safety=True ────────→ query_food_safety（查内置法规库）→ generate_report
    └─ 一般话题 ─────────────────────────→ generate_report
  generate_report → check_negative
    ├─ 店长要求修改 ─────────────────────→ generate_report（重新生成）
    └─ 无严重差评 / 店长已确认 ──────────→ save_analysis（落库 review_analysis）
```

### ③ Human-in-the-Loop 人工确认

```
触发点 1｜KitchenAgent.check_priority（高峰期排队超阈值）
  排队单数 > config.KITCHEN_INTERRUPT_THRESHOLD（默认 10）
    → _try_interrupt({...})                    # LangGraph interrupt，暂停图执行
    → 响应置 need_human_confirm=True，前端/平板提示后厨主管
    → 主管再次调用（human_confirmed=true）或带 priority_adjustment
    → 图恢复：确认 → assign_stations；要求调整 → 回 schedule_tasks 重排

触发点 2｜ReviewAgent.check_negative（严重差评）
  差评 + 涉及食品安全/服务态度
    → _try_interrupt({...})                    # 暂停等店长审核回复
    → 店长确认 manager_confirmed=true → save_analysis
    → 店长要求修改 manager_request_revise=true → 回 generate_report

Pipeline 模式差异：多 Agent 串联时不逐个 interrupt（避免中途挂起整条流水线），
改为「标记待确认」，等流水线结束后由 API 层统一提示人工介入。

旁路的人工介入（非 interrupt）：
  OrderAgent.require_manual_review —— 校验超限订单不进 interrupt，走本地终态拦截
  （needs_manual_review=True，订单状态置 STATUS_MANUAL_REVIEW），由 API 层提示人工介入。
```

### ④ 记忆清理任务（外部调度）

```
外部定时任务（Windows 计划任务 / cron / K8s CronJob）
  → scripts/purge_expired.py [--tenant store_001] [--dry-run]
       ├─ --dry-run            只统计不删除（先看会删多少）
       ├─ --tenant             只清理指定门店，不传 = 全部门店/全表
       └─ --conversation-days / --review-days / --order-log-days / --compliance-years
                               临时覆盖保留策略（0 = 永久）
  → memory/store.py 的 BaseStore.purge_expired(retention, tenant_id, dry_run)
       ├─ PostgresStore：DELETE FROM {domain} WHERE ... RETURNING id
       └─ FileStore：过滤 JSON 数组后回写
  → 返回各数据域清理条数，并逐域打日志（保留天数 / 截止日期 / 清理条数）
```

## 四、功能需求对照表

> 本节按核查报告第一章的章节组织。**判定口径与报告完全一致，不做美化**：
> ✅ 完整实现　⚠️ 部分实现或语义偏移　❌ 未实现　⚪ 需求标注「可选」。
> 「需求」列为报告摘录的需求原文（含报告给出的原始措辞）；「实现」列为本次读 `D:\heima\FoodAgent` 源码核实到的类/函数/表/节点名。

### 4.1 四大 Agent（LangGraph 状态图）

| 需求项 | 需求 | 实现 | 判定 |
|---|---|---|---|
| OrderAgent 节点数 | 8 个 | **10 个**：`parse_order` / `recommend_dishes` / `check_allergens` / `confirm_allergen_notice` / `check_nutrition` / `calculate_price` / `validate_order` / `generate_summary` / `require_manual_review` / `submit_order`（`OrderAgent.define_nodes()`，`info()['node_count']` 实测 10） | ⚠️ 超需求（见注解） |
| KitchenAgent 节点数 | 7 个 | 7 个：`load_orders` / `check_inventory` / `generate_tasks` / `schedule_tasks` / `check_priority` / `assign_stations` / `dispatch_orders`（实测 7） | ✅ |
| ReviewAgent 节点数 | 7 个 | 7 个：`collect_reviews` / `classify_sentiment` / `extract_topics` / `query_food_safety` / `generate_report` / `check_negative` / `save_analysis`（实测 7） | ✅ |
| SafetyAgent 节点数 | —（本期新增） | 9 个：`classify_query` / `retrieve_knowledge` / `check_confidence` / `web_search` / `generate_answer` / `generate_fallback` / `generate_general` / `enqueue_pending` / `save_memory`（实测 9，食安问答轻量 RAG 流程） | ✅ 超需求 |
| 条件边（conditional_edges） | 各 Agent 明确列出 | 3 组 / 2 组 / 2 组 / 3 组，路由函数齐备（`define_structure()` 的 `conditional_edges`） | ✅ |
| 节点并行 | 需求 3.7 提及 `asyncio.gather` | 检索环节并行执行（`order_agent.recommend_dishes` 内 `asyncio.gather`） | ✅ |
| 工具分配 | 每 Agent 5-6 个 | 与需求映射一致：`ToolRegistry.AGENT_OWNERSHIP` 中 OrderAgent 6 个、KitchenAgent 4 个、ReviewAgent 5 个、SafetyAgent 3 个（`query_food_safety_law` / `query_nutrition` / `web_search` 与 Review 共用），`get_order_detail` 由 Order/Kitchen 共享 | ✅ |
| Human-in-the-Loop | KitchenAgent `check_priority`、ReviewAgent `check_negative` 用 interrupt | 均用 `interrupt` 实现（两个 Agent 各有一个 `_try_interrupt()` 调用 `langgraph.types.interrupt`） | ✅ |
| 回环（反推荐/库存不足） | `validate_order → recommend_dishes`、`check_inventory → load_orders` | `_MAX_RECOMMEND_RETRY = 2`、`_MAX_INVENTORY_RETRY = 1` | ✅ |

**注解（OrderAgent 10 vs 8）**：实现比需求多 2 个节点——

- `confirm_allergen_notice`：对应需求 `3.8 Q8` 的「过敏原须**口头告知**顾客并留痕」，属需求的**合规要求**，需求 `3.3.1` 的节点表里漏列了；
- `require_manual_review`：高风险订单转人工复核，属安全增强。

> ⚠️ **口径统一（本次已处理）**：需求 `3.3.1` 与简历表述均写 **8 节点**，实现为 **10 节点**，核查报告 P2-15 指出该不一致。
> 本基线**统一采信实现口径：OrderAgent 10 节点、4 个 Agent 共 33 节点（10 + 7 + 7 + 9）**，并以替换补丁形式给出 `README.md` 的修正。
> **代码注释层面已同步**（P2-15 闭环）：`agents/__init__.py` 概览写「共 33 个节点（10 + 7 + 7 + 9）」并在说明中区分「需求文档 3.1 记为 22 个（OrderAgent 8 节点）」这一**需求侧历史口径**；
> `scripts/selfcheck.py` 的节点数自检亦按 10 节点断言。

### 4.2 工具层（15 个）

| 类别 | 需求 | 实现 | 判定 |
|---|---|---|---|
| LLM 工具 | 4：`llm_classify_intent` / `llm_recommend_dishes` / `llm_sentiment_analysis` / `llm_generate_improvement` | 同名 4 个，全部注册（`tools/llm_tools.py` 的 `LLM_TOOLS`） | ✅ |
| 规则引擎 | 4：`allergen_detection` / `price_calculation` / `inventory_calculation` / `priority_calculation` | 同名 4 个（`tools/rule_tools.py` 的 `RULE_TOOLS`） | ✅ |
| MCP 工具 | 4：`search_menu_knowledge` / `query_food_safety_law` / `query_nutrition` / `web_search` | 同名 4 个（`tools/mcp_tools.py` 的 `MCP_TOOLS`；全部读内置知识库，失败降级关键词匹配，联网失败走缓存） | ✅ |
| 外部 API | 3：`get_order_detail` / `get_ingredient_inventory` / `get_review_data` | 同名 3 个（`tools/external_api.py` 的 `EXTERNAL_TOOLS`）+ 辅助函数（`list_pending_orders` / `sync_pos_order` / `sync_kitchen_display` / `get_store_info` 等，不计入 15 个） | ✅ |
| 合计 | 15 | `registry.py` 断言 `sum == 15`；实测 `count()` = LLM 4 / 规则引擎 4 / MCP 4 / 外部 API 3，合计 15 | ✅ |
| 字段映射 | 需求给了 `ORDER_STATUS_MAP` / `DISH_CATEGORY_MAP` 示例 | 均已实现 + `PLATFORM_MAP`（`tools/external_api.py`） | ✅ |
| 鉴权 | OAuth2.0 Bearer + StoreKey 签名 | 已实现 | ✅ |
| 容错降级 | 外部API：重试1次→切备用源→友好提示；MCP：降级关键词/缓存；规则：缺数据返回默认值并标记 | 三档降级均实现，且写进 `degraded_tools` 响应字段（`base/exceptions.py` 重试装饰器 + `State.degraded_tools`） | ✅ |
| 过敏原判定不交给 LLM | 需求 3.4.1 ③（报告引述的设计要求） | `allergen_detection` 为纯规则精确交集；预设过敏原库 `config.ALLERGEN_LIBRARY` **实测 65 条**（去重后） | ✅ |

### 4.3 意图与 API

| 需求项 | 需求 | 实现 | 判定 |
|---|---|---|---|
| 意图类别 | 7 类 | `INTENT_ROUTES` **9 类**（`dish_recommend` / `smart_order` / `allergen_query` / `kitchen_dispatch` / `inventory_check` / `review_analysis` / `improvement_advice` / `food_safety_query` / `nutrition_query`），前 7 类名称与需求逐一对齐，后 2 类为 SafetyAgent 新增 | ✅ 超需求 |
| 意图识别 | LLM 优先 + 规则兜底 | 三级：规则快路径 / 结果缓存 / LLM，`INTENT_KEYWORDS` 规则兜底（`classify_intent`） | ✅ |
| RESTful API | 18 个 | 18 个业务接口（`api/routes.py` 的 `ENDPOINTS` 常量，`GET /api/endpoints` 自文档化）；另有元信息接口 | ✅ |
| 流式输出 | SSE | `run_stream` SSE（`GET /api/chat/stream`，事件类型 `intent` / `stage` / `token` / `result` / `error` / `done`）+ 前端联动 | ✅ |
| JWT 认证 | 需求 5 | `/auth/login` + `/auth/me`（`api/auth.py`，PBKDF2-HMAC-SHA256 加盐哈希，有效期由 `config.JWT_EXPIRE_MINUTES` 控制） | ✅ |

18 个业务接口清单（来源：`api/routes.py` 的 `ENDPOINTS` 常量，可用 `GET /api/endpoints` 复核）：

| # | 方法 | 路径 | 说明 |
|---|------|------|------|
| ① | POST | `/api/auth/login` | 登录 |
| ② | GET | `/api/auth/me` | 当前用户信息 |
| ③ | POST | `/api/tasks` | 创建任务 |
| ④ | POST | `/api/chat` | 对话（非流式） |
| ⑤ | GET | `/api/chat/stream` | 对话（SSE 流式） |
| ⑥ | DELETE | `/api/chat/history/{thread_id}` | 清除任务历史 |
| ⑦ | POST | `/api/orders` | 创建订单 |
| ⑧ | GET | `/api/orders/{order_id}` | 查询订单详情 |
| ⑨ | GET | `/api/orders` | 订单列表 |
| ⑩ | GET | `/api/kitchen/pending` | 待处理订单（后厨） |
| ⑪ | POST | `/api/kitchen/schedule` | 生成排产方案 |
| ⑫ | POST | `/api/reviews/analyze` | 评价分析 |
| ⑬ | GET | `/api/reviews/history` | 评价分析历史 |
| ⑭ | GET | `/api/memory/preferences` | 查询门店偏好 |
| ⑮ | PUT | `/api/memory/preferences` | 更新门店偏好 |
| ⑯ | GET | `/api/memory/customer/{customer_id}` | 查询顾客画像 |
| ⑰ | PUT | `/api/memory/customer/{customer_id}` | 更新顾客画像 |
| ⑱ | GET | `/api/agents` | Agent 清单与结构 |

> **元信息接口口径**：报告写作「另有 5 个元信息接口」。本次核实 `api/routes.py` 实际注册的元信息路由为
> `/api/tools`（15 工具清单）、`/api/intents`（9 意图清单）、`/api/config`（前端配置）、
> `/api/health`（健康检查，**已扩展主/备模型观测字段**）、`/api/endpoints`（接口自文档化）共 **5 个**
> （`/api/agents` 已计入上表第 ⑱ 项，故合计 6 个元信息路由）。**口径一律以 `ENDPOINTS` 常量与实际路由装饰器为准。**

### 4.4 记忆与隔离

| 需求项 | 需求 | 实现 | 判定 |
|---|---|---|---|
| 短期记忆 | LangGraph Checkpointer，按 `thread_id` 隔离 | `memory/checkpointer.py` 的 `build_thread_id()` → `{tenant_id}_{user_id}_{task_id}` | ✅ |
| Checkpointer 后端 | PostgreSQL（`PostgresSaver`） | **三级降级**：`PostgresSaver` > `MemorySaver` > `None`（`CheckpointerHolder.get()`） | ✅ 超出 |
| 长期记忆 | PostgreSQL，`tenant_id + user_id` 双层隔离 | `memory/store.py` 的 `PostgresStore`（psycopg3）+ `FileStore` 降级；门店级写锁 `_WRITE_LOCKS` + `_get_write_lock()` | ✅ |
| 三张表 | store_preferences / customer_profiles / review_analysis | 均已实现（另有 `ingredient_usage`、`compliance_log`、`order_status_log`，共 6 张，见 `memory/schemas.py` 与 `docs/deploy/schema.sql`） | ✅ 超出 |
| 读时机 | 任务启动时加载偏好注入 State | `Orchestrator._load_memory()` 启动加载，写入 `State.store_preferences` / `State.customer_history` | ✅ |
| 写时机 | 节点后 Checkpointer 自动存 + 结束后异步写库 | 已实现（如 `order_agent.submit_order` 异步写回长期记忆） | ✅ |
| **清理策略** | **对话 90 天归档 / 门店偏好永久 / 顾客画像永久 / 评价分析 2 年** | **已实现**：`BaseStore.purge_expired()`（`memory/store.py`）按数据域清理，`PostgresStore` 走 `DELETE FROM {domain} ... RETURNING id`、`FileStore` 过滤 JSON 数组后回写；策略唯一来源 `default_retention()` → `config.memory_retention()`；`store_preferences` / `customer_profiles` 在 `PERMANENT_DOMAINS` 中被**强制置 0、永不清理**；调用入口 `scripts/purge_expired.py`（`--tenant` / `--dry-run` / `--*-days` 覆盖）；调度示例见 `docs/OBSERVABILITY.md` 第九节 | ✅ **已实现**（**对话域有限制，见下注**） |

> **清理口径与已知限制（必须按此陈述，不得写成"完全满足"）**：
> - 保留天数唯一来源为 `base/config.py` 与 `config.ini` 的 `[memory]` 段：
>   `MEMORY_RETENTION_CONVERSATION_DAYS = 90`、`MEMORY_RETENTION_REVIEW_DAYS = 730`（= 2 年）、
>   `MEMORY_RETENTION_ORDER_LOG_DAYS = 90`、门店偏好与顾客画像 **0 = 永久**（`MEMORY_RETENTION_STORE_PREF_DAYS` / `MEMORY_RETENTION_CUSTOMER_DAYS`）；
>   合规留痕沿用 `[compliance] log_years = 3`（单位「年」，按 365.25 天换算）。
> - 时间判定列（`TIME_COLUMNS`）：`review_analysis.review_date`、`order_status_log.created_at`、`compliance_log.created_at`；
>   FileStore 另按 `FILE_TIME_KEYS` 依次兜底（如 `compliance_log` 退 `confirmed_at` / `noticed_at`）。
> - **保守策略**：记录时间戳缺失或无法解析时**跳过不删**并打 warning，不做猜测性删除。
> - ⚠️ **对话 90 天归档并未物理执行**：短期记忆存在 LangGraph Checkpointer 的表里，`memory/schemas.py` 的 6 张业务表**没有对话表**，
>   因此 `purge_expired()` 对 `conversation` 域**恒返回 0、不做任何物理删除**（代码中显式回填 `purged["conversation"] = 0`，并在日志中说明「Checkpointer 由 PG 维护，本方法返回 0」）。
>   该域目前只是**保留策略已登记、清理动作未落地**；真正的对话归档需另行清理 Checkpointer（例如按 `thread_id` 的创建时间清理其 checkpoint 表），**当前未实现**。
>   因此需求 3.6.5 的「对话 90 天归档」应判定为 **⚠️ 部分满足**，其余三个数据域（评价分析 730 天、合规留痕 3 年、门店偏好/顾客画像永久）为 ✅ 完整实现。

### 4.5 合规与工程

| 需求项 | 需求 | 实现 | 判定 |
|---|---|---|---|
| 过敏原强制流程 | 涉敏订单必经食品安全审查 | `check_allergens` →（涉敏时）`confirm_allergen_notice` → `check_nutrition` → `calculate_price` → `validate_order`（`OrderAgent.define_structure()`：条件边进告知节点，固定边从告知节点回主流程） | ✅ |
| 留痕 3 年备查 | 需求 6 / 3.8 Q8 | `compliance_log` 表 + `store.save_compliance_log()` + `config.COMPLIANCE_LOG_YEARS`（默认 3）+ `purge_expired()` 按年限清理 | ✅ |
| Docker 部署 | 需求 8 | `Dockerfile` + `docs/deploy/docker-compose.yml`（`foodagent:1.0.0` 映射 `8090:8090`，含 `postgres:16-alpine`；知识层内置，无需额外容器） | ✅ |
| K8s | 需求（技术负责人档） | `docs/deploy/k8s/foodagent.yaml`（8 个资源，`yaml.safe_load_all` 全解析通过）+ `docs/deploy/k8s/README.md` | ✅ |
| 等保二级 | 需求 8 | **无对应文档/产物**（`docs/` 下仅 `OBSERVABILITY.md`、`TOOL_NAME_MAPPING.md`、`deploy/`，无等保相关材料） | ⚠️ 无法验证（如实记为未实现，见「范围界定」） |
| 可观测性 | LangSmith / Locust / Prometheus | 三态开关（`config.py` 的 `LANGSMITH_*` / `REDIS_*` / `PROMETHEUS_*` / `LOADTEST_*` / `K8S_*`）+ `docs/OBSERVABILITY.md` + `scripts/verify_observability.py` 63 PASS | ✅ |
| 性能压测 | Locust 500 并发 | `scripts/locustfile.py`（2 个用户类 `FoodAgentUser` / `KitchenUser`，语料覆盖 9 类意图 + 1 类 Pipeline） | ✅ |
| **LLM 备用模型** | **主模型不可用切备用模型（Qwen3-Premium → Qwen3-7B）** | **已实现**：`tools/llm_client.py` 中主模型重试耗尽后调用 `_switch_to_fallback()`（`_fallback_configured` = `LLM_FALLBACK_ENABLED` 且 `fallback_model` 非空），用备用 `model` / `base_url` / `api_key` 经 `_ensure_fallback_client()` 再试一次；成功后 `_switch_back_to_primary()` 复位。配置项 `LLM_FALLBACK_ENABLED` / `LLM_FALLBACK_MODEL` / `LLM_FALLBACK_BASE_URL` / `LLM_FALLBACK_API_KEY`（留空则沿用主模型），见 `base/config.py` 与 `config.ini` 的 `[llm]` 段。只读属性 `primary_model` / `fallback_enabled` / `active_model` / `using_fallback` 对外暴露；`GET /api/health` 的 `dependencies.llm` 段新增 `primary_model` / `fallback_enabled` / `fallback_model` / `active_model` / `using_fallback` 五个字段（`api_key` 仍经 `mask_key()` 脱敏）；`tools/metrics.py` 的 `foodagent_llm_calls_total` 新增 `result="success_fallback"` 取值。流式路径（`chat_stream`）同构：首个事件发出前失败即切备用模型重试 | ✅ **已实现** |
| 模型微调 | 3.9（可选） | 未做（可接受） | ⚪ 可选 |
| 目录规范 | — | 本文件（`docs/REQUIREMENTS.md`）即补齐该缺口（报告 1.5 与 P1-9 提及） | ✅ 本次补齐 |

## 五、数据模型

表结构唯一来源：`memory/schemas.py`（`ALL_DDL` + `TABLES`），导出产物 `docs/deploy/schema.sql`。**共 6 张表，全部含 `tenant_id`。**

| 表名 | 中文名 | 关键字段 | 隔离键 | 保留口径（`purge_expired()`） |
|------|--------|----------|--------|------|
| `store_preferences` | 门店菜品偏好表 | `popular_dishes`(TEXT[])、`seasonal_preferences`(JSONB)、`customer_feedback_summary`(JSONB)、`avg_order_value`、`peak_hours`(JSONB)、`updated_at` | `tenant_id` | **永久**（`PERMANENT_DOMAINS`，强制跳过） |
| `customer_profiles` | 顾客画像表 | `customer_id`、`dietary_restrictions`(TEXT[])、`allergens`(TEXT[])、`favorite_dishes`(TEXT[])、`visit_count`、`avg_spending`、`member_level`、`last_visit_date` | `tenant_id` + `user_id`（录入员工） | **永久**（`PERMANENT_DOMAINS`，强制跳过） |
| `review_analysis` | 评价分析记录表 | `review_id`、`platform`(meituan/dianping/douyin)、`rating`、`sentiment`、`sentiment_score`、`topics`(TEXT[])、`summary`、`suggested_reply`、`reply_status`、`review_date` | `tenant_id` + `user_id` | **730 天（2 年）**，按 `review_date` 判定 |
| `ingredient_usage` | 食材消耗记录表 | `usage_date`、`ingredient`、`used_amount`、`unit`、`order_count` | `tenant_id` | 需求 3.6 ③「食材消耗追踪」；**未纳入清理策略**（`TIME_COLUMNS` 未含该表） |
| `compliance_log` | 合规留痕表 | `order_id`、`declared_allergens`(TEXT[])、`detected_allergens`(TEXT[])、`recommended_dishes`(TEXT[])、`isolation_action`、`action`、`notice_channel`(verbal/sms/system)、`notice_content`、`noticed_at`、`confirmed_by`、`confirmed_at`、`customer_ack`、`retention_years`(默认 3) | `tenant_id` + `user_id` | **3 年**，按 `created_at` 判定（缺失时退 `confirmed_at` / `noticed_at`） |
| `order_status_log` | 订单状态流转表 | `order_id`、`from_status`、`to_status`、`operator`(agent/user/system)、`reason`、`meta`(JSONB)、`created_at` | `tenant_id` + `user_id`（操作员工） | **90 天**（与对话同归档口径），按 `created_at` 判定 |

索引：`idx_store_pref_tenant`、`idx_customer_tenant(tenant_id, customer_id)`、`idx_review_tenant_date` / `idx_review_sentiment`、`idx_ingredient_tenant_date`、`idx_compliance_tenant` / `idx_compliance_order`、`idx_order_status_order` / `idx_order_status_to`。

**隔离落地方式（可核查）**：

```sql
-- 门店级隔离：所有查询强制带 tenant_id
SELECT * FROM store_preferences WHERE tenant_id = %s ORDER BY updated_at DESC LIMIT 1;

-- 顾客级隔离：附加 customer_id（员工级隔离再加 user_id）
SELECT * FROM customer_profiles WHERE tenant_id = %s AND customer_id = %s LIMIT 1;

-- 留痕/流转查询同样强制带 tenant_id
SELECT * FROM compliance_log   WHERE tenant_id = %s AND order_id = %s ...;
SELECT * FROM order_status_log WHERE tenant_id = %s AND order_id = %s ...;

-- 清理（purge_expired 生成，均带 tenant_id 条件 + RETURNING id 统计）
DELETE FROM review_analysis WHERE tenant_id = %s AND review_date < %s RETURNING id;
```

- 短期记忆的隔离键是 `thread_id`（`build_thread_id(tenant_id, user_id, task_id)` → `{tenant_id}_{user_id|anon}_{task}`），与上表的 `tenant_id/user_id` 同源。
- 无 PostgreSQL 时降级为 `FileStore`（`data/memory_store/*.json`，文件名即 `tenant_id`），隔离语义不变；进程内写锁 `_WRITE_LOCKS` 按门店维度串行化「读-合-写」，避免并发覆盖。
- **清理策略已实现**，各表保留口径见上表；⚠️ **对话（Checkpointer）不在上述 6 张表内，`conversation` 域恒返回 0、不物理删除**（详见 4.4 注）；`ingredient_usage` 亦未纳入清理范围。

## 六、验收要点

| 编号 | 验收项 | 判定方式 |
|------|--------|----------|
| AC-01 | 4 个 Agent 可构建并对外暴露结构 | `GET /api/agents` 返回 `total_agents=4`；核对 OrderAgent 10 / KitchenAgent 7 / ReviewAgent 7 / SafetyAgent 9，`total_nodes=33` |
| AC-02 | 15 个工具全部注册 | 运行 `python tools/registry.py`，分类计数打印且 `assert sum == 15` 不抛错；或 `GET /api/tools` 看 `counts` / `total` |
| AC-03 | 9 类意图与路由目标正确 | `GET /api/intents` 返回 9 条，逐条核对 `agent` 与 `complex` 字段 |
| AC-04 | 18 个业务接口可访问 | `GET /api/endpoints` 返回 `total=18`；按 4.3 节表格逐条调用（认证类需带 JWT） |
| AC-05 | 自然语言可路由到正确 Agent（LLM 与规则两条路径均通） | 9 类意图各造一句问句实测；再以 `LLM_ENABLED=false`（或无 Key）复测规则兜底路径 |
| AC-06 | 多 Agent Pipeline 可串联执行 | 输入含「全流程 / 从推荐到」等复合诉求，观察 `intents` 命中多 Agent 且按 Order → Kitchen → Review → Safety 顺序执行 |
| AC-07 | 涉敏订单走强制合规链路并留痕 | 下一单含高/中风险过敏原的订单，核对响应含过敏原警示前缀（`config.ALLERGEN_ALERT_PREFIX`），且 `compliance_log` 新增含 `notice_channel` / `notice_content` / `confirmed_by` 的记录 |
| AC-08 | Human-in-the-Loop 可暂停并恢复 | 构造排队单数 > `KITCHEN_INTERRUPT_THRESHOLD`（默认 10）触发 `need_human_confirm=true`，再以 `human_confirmed=true` 调 `POST /api/kitchen/schedule` 验证流程继续；严重差评场景同理验证 `check_negative` |
| AC-09 | 双层隔离不串数据（门店 + 员工） | 用 `waiter_01`（store_001）与 `waiter_02`（store_002）分别写门店偏好与顾客画像，交叉查询互不可见（`/api/memory/preferences`、`/api/memory/customer/{id}`） |
| AC-10 | 三层记忆降级后功能不中断 | 分别在 `PG_ENABLED=true / false` 下启动，核对 `GET /api/health` 的 `checkpointer` 类型（`PostgresSaver` / `MemorySaver` / `none`），两种模式下主流程均可跑通 |
| AC-11 | SSE 流式可逐字输出 | 浏览器打开首页，或 `curl "http://127.0.0.1:8090/api/chat/stream?query=...&token=<JWT>"`，确认收到 `intent` / `stage` / `token` / `result` / `done` 事件序列 |
| AC-12 | 记忆清理按保留口径生效且不误删 | ① `python scripts/purge_expired.py --dry-run` 只看统计不改数据（可先造逾期 `review_analysis` / `order_status_log` / `compliance_log` 记录）；② 去掉 `--dry-run` 实跑，确认上述三域条数下降；③ 断言 `store_preferences` / `customer_profiles` 恒为 0（永久不清理）；④ `--tenant store_001` 只影响该门店。**限制**：`conversation` 域恒返回 0，不产生物理删除，验收时**不得**把该项计为通过 |
| AC-13 | 镜像与编排产物可用 | `docker build` 成功；`docker compose -f docs/deploy/docker-compose.yml up -d` 后 8090 可访问；`kubectl apply --dry-run=client -f docs/deploy/k8s/foodagent.yaml` 通过 |
| AC-14 | 可观测性三态矩阵全通过 | 运行 `python scripts/verify_observability.py`，期望 `63 PASS / 0 FAIL`；`python scripts/selfcheck.py --no-e2e` 期望全通过 |
| AC-15 | 500 并发压测不崩 | `python -m locust -f scripts/locustfile.py --host http://127.0.0.1:8090 --headless -u 500 -r 50 -t 120s`，观察失败率与 P95（阈值见 `docs/OBSERVABILITY.md` 第五节） |
| AC-16 | LLM 主备模型切换可用 | ① `GET /api/health` 的 `dependencies.llm` 应含 `primary_model` / `fallback_enabled` / `fallback_model` / `active_model` / `using_fallback`；② 配好 `LLM_FALLBACK_MODEL` 后令主模型不可用（错误 Key / 改错 `base_url`），发一次 `/api/chat`，断言 `active_model` 变为备用模型、`using_fallback=true`；③ `GET /metrics` 中 `foodagent_llm_calls_total{result="success_fallback"}` 计数增加；④ 恢复主模型后 `using_fallback` 回到 `false` |
| AC-17 | 等保二级合规 | **建议补充**：需求 8 提及，当前无任何对应文档/产物可核 |

## 七、范围界定

### 已实现（对应需求基线的核心闭环）

- ✅ 四大 Agent 的 LangGraph 状态图：OrderAgent 10 节点 / KitchenAgent 7 节点 / ReviewAgent 7 节点 / SafetyAgent 9 节点（共 33 节点），条件边 3 + 2 + 2 + 3 组，回环重试上限齐备
- ✅ Orchestrator 编排：9 类意图识别（规则快路径 / 结果缓存 / LLM / 规则兜底 四级，`source` 标注命中层级）、单 Agent 直达、多 Agent 串联 Pipeline、长期记忆注入、声明式 `agent_sequence` / `context_extractors` / `seed_context`
- ✅ 15 个工具四类分层（LLM 4 / 规则引擎 4 / MCP 4 / 外部 API 3）+ 工具注册表断言
- ✅ 规则引擎承担「必须精确」的判定：过敏原检测基于 65 条预设过敏原库做精确交集，不经 LLM
- ✅ 三层记忆：Checkpointer 短期记忆（`PostgresSaver > MemorySaver > None` 三级降级）+ 长期记忆（`PostgresStore ↔ FileStore` 降级）+ 行业特定记忆（6 张表）
- ✅ **记忆清理/归档（需求 3.6.5）**：`BaseStore.purge_expired()` + `PostgresStore`/`FileStore` 双实现 + `scripts/purge_expired.py` 入口（`--tenant` / `--dry-run`）；保留口径 = 对话 90 天（**仅登记策略，不物理删除，见 4.4 注**）/ 订单流转 90 天 / 评价分析 730 天 / 合规留痕 3 年 / 门店偏好与顾客画像永久（`PERMANENT_DOMAINS` 强制跳过）；时间戳缺失保守跳过；调度示例见 `docs/OBSERVABILITY.md` 第九节
- ✅ **LLM 备用模型切换（P1-7）**：`_switch_to_fallback()` / `_switch_back_to_primary()`；四个 `LLM_FALLBACK_*` 配置项；`/api/health` 暴露 5 个观测字段；Prometheus 新增 `result="success_fallback"`
- ✅ `tenant_id + user_id` 双层隔离 + 门店级进程内写锁
- ✅ 食品安全合规：过敏原强制链路（`check_allergens → confirm_allergen_notice → check_nutrition → calculate_price → validate_order`）、口头告知留痕（`compliance_log`，订单号在 `check_allergens` 阶段预生成，保证留痕与订单同号）、3 年保留与到期清理
- ✅ Human-in-the-Loop：KitchenAgent `check_priority`、ReviewAgent `check_negative` 用 LangGraph `interrupt`；Pipeline 模式转为标记待确认
- ✅ 18 个业务接口 + SSE 流式 + JWT 认证（PBKDF2 加盐哈希）+ 3 角色 4 演示账号
- ✅ 全链路降级：langgraph / openai / PostgreSQL / Redis / POS-KDS / Milvus / 外部知识源 任一缺失均自动降级，降级写入 `degraded_tools`
- ✅ 部署与工程产物：`Dockerfile`、`docs/deploy/docker-compose.yml`、`docs/deploy/schema.sql`、`docs/deploy/k8s/`（清单 + README）
- ✅ 可观测性：LangSmith / Redis / Prometheus / Locust / K8s 五组件默认关闭、按需开启
- ✅ 知识层内置化：食安法规 / 菜品库 / 营养表由 `tools/safety_knowledge.py` + `tools/menu_data.py` 提供，`tools/mcp_tools.py` 的 4 个检索工具统一封装，失败降级关键词匹配、联网结果缓存 24h；换外部 RAG 只需改该文件数据源
- ✅ 节点数口径统一（P2-15 闭环）：`README.md`、`agents/__init__.py`（概览「共 33 个节点（10 + 7 + 7 + 9）」）、`scripts/selfcheck.py` 一致

### 明确未实现（如实列出报告指出的缺口）

| 未实现项 | 需求依据 | 现状（本次读源码核实） |
|----------|----------|------------------------|
| **对话记忆的物理归档** | 需求 3.6.5「对话 90 天归档」 | **保留策略已登记、清理动作未落地**。短期记忆在 LangGraph Checkpointer 的表内，`memory/schemas.py` 的 6 张业务表**没有对话表**，故 `purge_expired()` 对 `conversation` 域**恒返回 0、不删除任何数据**（代码显式回填 0 并打日志说明）。真正归档需另行清理 Checkpointer 表，当前未实现。故该子项判定为 **⚠️ 部分满足**（需求 3.6.5 整体判定为「已实现，但对话域受限」） |
| **等保二级** | 需求 8 | **无对应文档/产物**。`docs/` 下仅有 `OBSERVABILITY.md`、`TOOL_NAME_MAPPING.md`、`deploy/`（`schema.sql` / `docker-compose.yml` / `k8s/`），**没有**等保测评材料、安全设计说明或整改记录。核查报告判定「⚠️ 无法验证」，本基线同判——**本次亦不新建任何虚构的等保文档** |
| **模型微调** | 需求 3.9（可选） | 未做。需求本身标注「可选」，不构成缺口 |
| **接口级 RBAC** | 需求 4.1「不同角色的使用差异」 | 角色字段已贯穿 State 与前端（快捷问题按角色下发），但**未做接口权限控制**——任何有效 JWT 均可调用全部 18 个接口，数据隔离只靠 `tenant_id` |
| **`ingredient_usage` 的保留策略** | 需求 3.6 ③ | 该表已建并有写入，但 `purge_expired()` 的 `TIME_COLUMNS` **未包含** `ingredient_usage`，即食材消耗记录目前**不参与清理**（需求未对该域给出保留期，故不算违反需求，仅作登记） |
| **三级缓存（Redis + MySQL + BM25）** | 报告 P1-5 指出：需求为「Redis（操作类 TTL=12h / 法规类 24h）→ MySQL FAQ 索引 → BM25（阈值 0.85）」 | 该缺口属 **FoodSafetySystem** 侧的对照项（报告 2.4）。FoodAgent 侧缓存为 `tools/cache.py` 的**另一种三级**（L1 进程内字典 → L2 Redis → L3 透传），`REDIS_DEFAULT_TTL` 默认 300 秒，与需求所列「Redis + MySQL + BM25」结构不同。本基线一并记录以免混淆 |

> **已闭环的核查报告条目（供追溯）**：
> - **P0-4 记忆清理策略** → 已实现（`purge_expired()` + `scripts/purge_expired.py` + `MEMORY_RETENTION_*` 配置）；唯一限制是对话域的物理归档未落地（见上表）。
> - **P1-7 LLM 备用模型** → 已实现（`_switch_to_fallback()` / `_switch_back_to_primary()` + 四个 `LLM_FALLBACK_*` 配置 + `/api/health` 五字段 + `result="success_fallback"` 指标）。
> - **P1-9 缺需求基线文档** → 本文件即补齐。
> - **P2-11 `data/memory_store/` 残留验证脚本产物** → 已处理：验证脚本产物改为落在独立临时目录 `data/_verify_tmp/`（退出时自动清理），`scripts/selfcheck.py` 注释中已注明「跑完不留痕——不再往 data/memory_store/ 丢验证产物」。
> - **P2-15 OrderAgent 8 / 22 节点口径** → 已统一为 **10 / 33 节点**（`README.md`、`agents/__init__.py`、`scripts/selfcheck.py`）。

## 八、与本项目其他文档的关系

| 文档 | 分工 |
|------|------|
| `README.md` | 面向使用者的**入口说明**：系统概览、快速开始、演示账号、目录结构、接口清单、健壮性设计。本基线是它的**需求侧上游**——README 的「4 个 Agent / 共 33 节点（10+7+7+9）」「15 个工具」「9 类意图」「18 个接口」等口径均以本文件为准 |
| `docs/OBSERVABILITY.md` | **可选重型组件（LangSmith / Redis / Prometheus / Locust / K8s）的接入指南**，并在第九节给出**记忆清理任务的外部调度**（Windows 计划任务 / cron / K8s CronJob 示例）。本文件只声明这些能力「已实现且默认关闭」，操作细节一律指向该文档 |
| `docs/deploy/` | **部署产物与运维说明**：`schema.sql`（由 `memory/schemas.py` 导出）、`docker-compose.yml`（foodagent + PostgreSQL）、`k8s/foodagent.yaml` 与 `k8s/README.md`。本文件「数据模型」章只描述表结构与隔离语义，建表脚本以该目录为准 |
| `docs/TOOL_NAME_MAPPING.md` | 工具名对照表（需求工具名 ↔ 代码函数名），本文件 4.2 节的工具名核查以其为辅助 |

---

## 核查方法说明

- **需求侧**：原始需求文档 `09-餐饮行业-智能餐饮Agent.html` **不在本机**，无法直接解析。因此本基线的全部「需求」列原文**逐条摘录自** `D:\heima\需求对照核查报告.md` 第一章（该报告由核查者用脚本剥离 HTML 标签后逐节阅读得出，其「需求原文均引自提取文本」）。**不引入报告之外的需求表述，不补造未见于报告的数字。**
- **实现侧**：以 `D:\heima\FoodAgent`（交付目标目录）的源码为实现事实。逐文件读源码核对，实际读取的文件包括
  `base/config.py`、`config.ini`、`memory/schemas.py`、`memory/store.py`、`memory/checkpointer.py`、`memory/order_state.py`、
  `agents/order_agent.py`、`agents/kitchen_agent.py`、`agents/review_agent.py`、`agents/orchestrator.py`、`agents/state.py`、`agents/base_agent.py`、
  `api/routes.py`、`api/auth.py`、`tools/registry.py`、`tools/llm_client.py`、`tools/metrics.py`、`tools/cache.py`、`tools/external_api.py`、
  `scripts/locustfile.py`、`scripts/selfcheck.py`、`scripts/test_api.py`、`scripts/purge_expired.py`、`docs/OBSERVABILITY.md`、`README.md`，
  并批量 grep 关键符号（`purge_expired` / `LLM_FALLBACK` / `MEMORY_RETENTION` / `success_fallback` / `using_fallback` / `retention` 等）。
- **计数类事实的一手机取**：用本机 Python 直接调用项目对象验证，而非仅读注释——
  `config.INTENT_ROUTES` = **9 类**；`config.ALLERGEN_LIBRARY` = **65 条**（去重后）；
  `tool_registry.count()` = LLM 4 / 规则引擎 4 / MCP 4 / 外部 API 3，合计 **15**；
  `memory.schemas.TABLES` = **6** 张表；
  `order_agent.info()['node_count']` = **10**，`kitchen_agent` = **7**，`review_agent` = **7**，`safety_agent` = **9**（合计 **33**）。
  接口数以 `api/routes.py` 的 `ENDPOINTS` 常量（**18** 条）与实际路由装饰器为准。
  > 该验证通过 `PYTHONPATH` 指向项目 + 桩替 `base.logger` 完成（项目日志处理器会写 `logs/app.log`，为避免写入被测项目目录而以桩替代）。
- **本轮复核（P0-4 / P1-7 两处判定升级）的依据与限制**：
  1. **依据**：核对到 `memory/store.py` 的 `BaseStore.purge_expired()`（含 `default_retention()` / `RETENTION_UNITS` / `PERMANENT_DOMAINS` / `TIME_COLUMNS` / `FILE_TIME_KEYS` / `_parse_ts()`，以及 `PostgresStore` 与 `FileStore` 各自的 `_purge_domain()` / `_purge_compliance_log()` 实现）、`scripts/purge_expired.py`（`--tenant` / `--dry-run` / `--conversation-days` / `--review-days` / `--order-log-days` / `--compliance-years`）、`base/config.py` 的 `MEMORY_RETENTION_*` 与 `memory_retention()`、`config.ini` 的 `[memory]` 段、`docs/OBSERVABILITY.md` 第九节；
     LLM 侧核对到 `tools/llm_client.py` 的 `_switch_to_fallback()` / `_switch_back_to_primary()` / `_ensure_fallback_client()` / `_active_model_name()` 与只读属性 `primary_model` / `fallback_enabled` / `using_fallback` / `active_model`、`api/routes.py` 的 `/api/health` 五字段、`tools/metrics.py` 的 `result="success_fallback"`、`scripts/test_api.py` 对健康检查字段的断言。
  2. **限制（如实记录）**：上述代码文件属**并行开发中的未落地改动**，本文件定稿时尚未同步到 `D:\heima\FoodAgent` 的对应文件。
     **使用本基线前，请按下列命令确认改动已落地，再据实采信 4.4 / 4.5 两节的 ✅ 判定**：
     ```powershell
     Select-String -Path D:\heima\FoodAgent\memory\store.py      -Pattern 'purge_expired'
     Select-String -Path D:\heima\FoodAgent\base\config.py       -Pattern 'MEMORY_RETENTION'
     Select-String -Path D:\heima\FoodAgent\tools\llm_client.py  -Pattern '_switch_to_fallback'
     Select-String -Path D:\heima\FoodAgent\api\routes.py        -Pattern 'using_fallback'
     Select-String -Path D:\heima\FoodAgent\tools\metrics.py     -Pattern 'success_fallback'
     Test-Path          D:\heima\FoodAgent\scripts\purge_expired.py
     ```
     六条均有命中即表示已落地；若某条无命中，该项应回退为「未实现」，并同步修改 4.4 / 4.5 与「范围界定」。
     > 补充说明：`result="success_fallback"` 这个**取值**由 `tools/llm_client.py` 计算并传入 `metrics.record_llm(model, result)`；
     > `tools/metrics.py` 中该词仅出现在 `record_llm()` 的 docstring（枚举可选值），故核查该指标须同时看这两个文件。
  3. **对话域限制已按要求单列**（不做"完全满足"表述）：`purge_expired()` 对 `conversation` 恒返回 0、不物理删除，真实归档需另行清理 Checkpointer——已写入 4.4 注、六、七三处。
- **判定口径**：✅ 完整实现；⚠️ 部分实现或语义偏移；❌ 未实现；⚪ 需求标注「可选」。
  与核查报告的差异及原因**全部显式标注**：报告判 ❌ 的**记忆清理**与**备用模型切换**，因本轮读到完整实现而升级为 ✅（对话域的物理归档另记 ⚠️ 部分满足）；
  报告判「⚠️ 无法验证」的**等保二级**维持未实现，且未新建任何替代性文档。
- **待核实事项**（无法从报告或代码确认，不做推测）：
  1. 需求侧是否给出**峰值并发数、响应时间、QPS** 等性能指标——报告对 FoodAgent 未列，故本基线不设对应目标；
  2. JWT Token 有效期需求值——`base/config.py` 注释写「需求文档约定 2 小时」，但原始文档不在本机，**该数值待回填复核**；
  3. 需求 3.4.1 外部 API 的 **StoreKey 签名**具体算法与字段——代码侧已实现，需求侧原文细节未见于报告；
  4. 预设过敏原库的需求条数（`config.py` 注释写「50+ 种」，实测 65 条）——需求侧原始条数待核实；
  5. 备用模型的需求**具体型号映射**（报告引述为 Qwen3-Premium → Qwen3-7B）与实现所用 DeepSeek 平台模型的对应关系——需求侧是否要求跨平台切换，原文不在本机，**待回填复核**。
- **本文档性质**：属**反推基线**。原始 HTML 归位后，应以原始文档逐条回归本文件的需求列与量化目标；如与本文件冲突，以原始文档为准，并同步更新 `README.md` 的口径。
