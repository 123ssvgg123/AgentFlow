# FoodAgent 架构与流程详解

> 这份文档的目标：**让你在半小时内搞清楚一次请求在系统里怎么流动、每层代码负责什么、想改东西该动哪个文件。**
> 配套的顶层架构图与业务说明见 [README.md](../README.md)；需求口径见 [REQUIREMENTS.md](REQUIREMENTS.md)。

---

## 目录

1. [怎么读这份文档](#一怎么读这份文档)
2. [一句话讲清这个系统](#二一句话讲清这个系统)
3. [分层架构与职责边界](#三分层架构与职责边界)
4. [一次请求的完整链路](#四一次请求的完整链路)
5. [Orchestrator：系统的调度中枢](#五orchestrator系统的调度中枢)
6. [四个 Agent 的节点图](#六四个-agent-的节点图)
7. [工具层：为什么分四类](#七工具层为什么分四类)
8. [三层记忆与读写时机](#八三层记忆与读写时机)
9. [State 数据流：最容易踩坑的地方](#九state-数据流最容易踩坑的地方)
10. [降级设计：没有依赖也能跑](#十降级设计没有依赖也能跑)
11. [代码索引：想改 X 去哪](#十一代码索引想改-x-去哪)
12. [调试指南](#十二调试指南)
13. [上手验证清单](#十三上手验证清单)

---

## 一、怎么读这份文档

| 你的目的 | 读哪几节 | 预计时间 |
|---------|---------|---------|
| 面试/答辩前快速过一遍 | 二、三、四、六 | 15 分钟 |
| 要改代码 / 加功能 | 五、六、九、十一 | 30 分钟 |
| 排查线上问题 | 十、十二、十三 | 随时查 |
| 完全从零了解 | 全文顺序读 | 45 分钟 |

---

## 二、一句话讲清这个系统

**它是一个"餐厅员工用的 AI 工作台"**：服务员、后厨主管、店长登录后，用自然语言提需求
（"推荐 10 人套餐"、"后厨现在几单"、"分析本周差评"），系统自动判断意图、路由到对应的
专业 Agent、调用精确工具算账/查过敏原/排产，最后返回可直接使用的结果。

它解决的三个核心难题：

| 难题 | 做法 |
|------|------|
| **LLM 会瞎编，但食品安全不能瞎编** | 把"必须精确"的计算（过敏原、价格、库存、优先级）交给规则引擎，LLM 只做理解和表达 |
| **一句话可能横跨多个业务环节** | Orchestrator 识别意图，单点直达 or 多 Agent 串联 Pipeline |
| **对话要记住上下文，业务要能追溯** | 三层记忆：Checkpointer 记对话、PG 记业务数据、合规表记责任 |

---

## 三、分层架构与职责边界

```
┌──────────────────────────────────────────────────────────┐
│ ① 交互层  static/index.html                              │
│    聊天窗口、SSE 打字机、角色切换                          │
├──────────────────────────────────────────────────────────┤
│ ② 接口层  api/app.py · routes.py · models.py · auth.py   │
│    HTTP 入参校验、JWT 鉴权、SSE 编码、生命周期钩子          │
├──────────────────────────────────────────────────────────┤
│ ③ 编排层  agents/orchestrator.py                         │
│    意图识别、路由决策、Pipeline 串联、上下文传递            │
├──────────────────────────────────────────────────────────┤
│ ④ Agent 层  agents/{order,kitchen,review,safety}/       │
│    LangGraph 状态图：节点编排 + 条件边 + 节点级快照         │
├──────────────────────────────────────────────────────────┤
│ ⑤ 工具层  tools/                                         │
│    15 个原子能力：LLM / 规则 / MCP / 外部 API              │
├──────────────────────────────────────────────────────────┤
│ ⑥ 记忆层  memory/                                        │
│    Checkpointer（短期）+ 6 张业务表（长期）+ 状态机         │
├──────────────────────────────────────────────────────────┤
│ ⑦ 基础设施  base/                                        │
│    配置、日志、异常、重试                                   │
└──────────────────────────────────────────────────────────┘
```

**每层的"该做"与"不该做"**（这条边界是项目能扩展的前提）：

| 层 | 该做 | 不该做 |
|----|------|--------|
| 接口层 | 鉴权、参数校验、把 HTTP 转成领域调用 | 写业务判断、直接调工具 |
| 编排层 | 决定"谁来干"、"按什么顺序干" | 实现具体业务节点 |
| Agent 层 | 决定"干的步骤"（节点顺序、分支条件） | 写 SQL、拼 HTTP 请求 |
| 工具层 | 一个工具只做一件确定的事 | 感知自己属于哪个 Agent |
| 记忆层 | 存取数据、保证隔离与一致性 | 判断业务该不该记 |

一个具体的例子：你说"推荐不辣的花生过敏套餐"
- 接口层只负责：JWT 有效吗？query 字段有没有？
- 编排层判断：这是 `dish_recommend` → OrderAgent
- OrderAgent 决定：先解析需求 → 再推荐 → 必须查过敏原 → 有风险就走告知留痕
- 工具层执行：`llm_recommend_dishes` 出候选，`allergen_detection` 用规则精确判定
- 记忆层记录：门店偏好更新、合规留痕写入

---

## 四、一次请求的完整链路

以 `POST /api/chat` 为例，从 HTTP 进来到底层落库，一共 9 步：

```mermaid
flowchart TD
    S1["① 前端 POST /api/chat<br/>Header: Bearer JWT"] --> S2["② routes.chat()<br/>auth_service.current_user 解出 tenant/user/role"]
    S2 --> S3["③ orchestrator.run()<br/>生成 thread_id"]
    S3 --> S4["④ _load_memory()<br/>读门店偏好 + 顾客画像"]
    S4 --> S5["⑤ classify_intent()<br/>快路径 → 缓存 → LLM，规则兜底"]
    S5 --> S6{"⑥ 是否需要多个 Agent？"}
    S6 -- 单意图 --> S7A["_run_single()<br/>直达一个 Agent"]
    S6 -- 跨 Agent / 全流程 --> S7B["_run_pipeline()<br/>Order → Kitchen → Review → Safety 串行"]
    S7A --> S8["⑦ agent.ainvoke(state)<br/>LangGraph 逐节点执行，节点内调工具"]
    S7B --> S8
    S8 --> S9["⑧ Checkpointer 存节点快照<br/>thread_id 隔离"]
    S9 --> S10["⑨ _aggregate() 汇总<br/>answer + pending_actions + degraded_tools"]
    S10 --> S11["返回 JSON / SSE 事件流"]
```

### 每一步的代码位置

| 步 | 做什么 | 代码位置 |
|----|--------|---------|
| ② | JWT 解析、注入 tenant/user | `api/auth.py` → `auth_service.current_user` |
| ③ | 生成 `thread_id`、整体调度 | `agents/orchestrator.py` → `run()` |
| ④ | 读长期记忆 | `orchestrator._load_memory()` → `memory/store.py` |
| ⑤ | 意图识别 | `orchestrator.classify_intent()` / `_rule_classify()` |
| ⑥ | 单点 or Pipeline 分流 | `orchestrator.run()` 第 242 行 `if agent_sequence or is_pipeline` |
| ⑦ | 状态图执行 | `agents/base_agent.py` → `ainvoke()` |
| ⑧ | 节点快照落库 | `memory/checkpointer.py`（由 LangGraph 自动调用） |
| ⑨ | 结果汇总 | `orchestrator._aggregate()` |

### SSE 流式与普通请求的区别

`GET /api/chat/stream` 走 `orchestrator.run_stream()`，把过程拆成四类事件：

```mermaid
flowchart LR
    E1["intent<br/>意图识别结果"] --> E2["stage<br/>Agent 阶段进度"] --> E3["token<br/>回答分片"] --> E4["result<br/>完整元数据"]
```

注意：`token` 是**把最终答案按标点切分后逐片吐出**（模拟打字机），不是 LLM 真流式。
因为 Agent 内部节点是确定性的（规则 + 工具），先有完整结果才有输出更稳妥。

---

## 五、Orchestrator：系统的调度中枢

`agents/orchestrator.py` 是整个系统最"聪明"的地方，做四件事：

### 5.1 意图识别（三级：快路径 → 缓存 → LLM，规则兜底）

```
用户输入
   ↓
① 规则快路径（短查询 ≤40 字 + 强信号正则 + 无复合标记词）→ 直接判定
   ↓ 未命中
② 结果缓存（intent:{tenant}:{md5(query)}，TTL 600s，Redis 优先 / 进程内兜底）
   ↓ 未命中
③ LLM 识别（Prompts.ORCHESTRATOR_ROUTE，输出 JSON）
   ↓ 失败 / 返回非法意图
④ 规则关键词匹配（INTENT_KEYWORDS）
   ↓
{intents: [...], primary_intent, is_pipeline, degraded, source}
```

- **为什么有前置两级**：门店高频问题高度重复，意图识别是每次请求的固定开销；快路径与缓存把单次 1.3s 压到 0.1ms，`source` 字段可定位本次命中的是 `rule_fast / cache / llm / rule_fallback`
- **LLM 通道**：能理解复合诉求（"从推荐到评价全流程处理"），但可能超时或被限流
- **规则通道**：9 组关键词命中，按业务顺序排序（点餐 → 后厨 → 评价 → 食安），**永远能出结果**
- **快路径的保守边界**：出现"另外/同时/然后/全流程/从推荐到"等复合标记，或文本超 40 字，立即交回 LLM，避免把两件事判成一件
- `is_pipeline` 判定：命中跨多个 Agent 的意图，或输入含"全流程/从推荐到/一条龙"

### 5.2 单 Agent 直达 vs Pipeline

```mermaid
flowchart TD
    I["意图列表 intents"] --> D{"涉及几个 Agent？"}
    D -- 1 个 --> S["_run_single<br/>直接 ainvoke 一个 Agent"]
    D -- 多个 --> P["_run_pipeline<br/>按 order → kitchen → review → safety 顺序"]
    P --> P1["阶段 1: OrderAgent<br/>产出 recommended_dishes / pending_orders"]
    P1 --> P2["阶段 2: KitchenAgent<br/>消费 pending_orders 排产"]
    P2 --> P3["阶段 3: ReviewAgent<br/>评价分析"]
    P3 --> P4["阶段 4: SafetyAgent<br/>食安问答（终态）"]
    P1 -.上下文传递.-> P2
    P2 -.上下文传递.-> P3
```

**上下文怎么传**：`_CONTEXT_EXTRACTORS` 是声明式提取表

| 上游 | 提取字段 | 给谁用 |
|------|---------|--------|
| order | `recommended_dishes` + `pending_orders` | KitchenAgent 排产（没订单就没法排） |
| kitchen | `ingredient_needs` + `station_assignment` | 下游运营参考 |
| review / safety | 无（终态） | — |

**声明式三件套**（`run()` 的三个可选参数，传 None 时与旧行为完全一致）：

| 参数 | 作用 |
|------|------|
| `agent_sequence` | 显式指定阶段与顺序，如 `["kitchen","review"]` 可跳过 OrderAgent |
| `context_extractors` | 覆盖内置提取器（按 key 覆盖），自定义上下文字段 |
| `seed_context` | 为下游阶段预置上下文（跳过 order 时手动提供 `pending_orders`） |

### 5.3 Pipeline 的 thread_id 隔离

Pipeline 每一阶段都会改 thread_id：

```python
stage_state["thread_id"] = f"{state.get('thread_id')}_{key}"   # 例: store_001_waiter_01_abc_order
```

**为什么**：同一个 thread_id 的 Checkpointer 状态会被后续节点覆盖。三个阶段共用会互相污染，
所以各阶段独立快照，但业务上下文通过 `carried_context` 显式传递。

### 5.4 Human-in-the-Loop 的两种模式

| 模式 | 行为 | 原因 |
|------|------|------|
| 单 Agent | 遇到 `need_human_confirm` 可真正中断（interrupt），等前端带 `human_confirmed=true` 重新请求 | 一次请求只做一件事，停得起 |
| Pipeline | **不中断**，`pipeline_mode=true`，把待确认项累积到 `pending_actions` | HTTP 请求不能挂起等人工，流水线要跑完 |

---

## 六、四个 Agent 的节点图

**4 个 Agent / 33 个节点** = OrderAgent 10 + KitchenAgent 7 + ReviewAgent 7 + SafetyAgent 9。

四个 Agent 都继承 `BaseAgent`，只要实现 `define_nodes()` + `define_structure()`，
图装配、降级、终态校验全部由基类接管。

### 6.1 OrderAgent（10 节点）—— 点餐主流程

```mermaid
flowchart TD
    P["parse_order<br/>解析人数/口味/过敏原/预算"] --> R["recommend_dishes<br/>LLM 推荐菜品组合"]
    R --> A{"check_allergens<br/>规则引擎精确判定"}
    A -- "无风险" --> N["check_nutrition<br/>营养分析"]
    A -- "中/高风险" --> C["confirm_allergen_notice<br/>口头告知 + 顾客确认留痕"]
    C --> N
    N --> PR["calculate_price<br/>折扣/套餐/会员价"] --> V["validate_order<br/>订单完整性校验"]
    V -- "通过" --> G["generate_summary<br/>生成订单摘要"]
    G --> SUB["submit_order<br/>提交 POS + 后厨"]
    V -- "不通过且未超重试" --> R
    V -- "不通过且已达上限" --> MR["require_manual_review<br/>转人工"]
```

三个设计要点：

1. **过敏原风险触发额外分支**：中高风险必须先做"口头告知 + 顾客确认"，把 `notice_channel` /
   `customer_ack` 写进合规表 —— 这是食品安全法规要求，也是本项目"合规"卖点的落点。
   注意告知节点只是**插在检测与营养之间的合规节点**，出口必须回到 `check_nutrition`：
   早期实现把它的出口直接连到 `validate_order`，结果涉敏订单既没有营养分析也没有价格明细
   （`nutrition_info` / `price_detail` 为空），是二次审计修掉的真实缺陷。
2. **校验失败会回环重推荐**，但有重试上限（`_MAX_RECOMMEND_RETRY`）。超上限不再硬提交，
   转 `require_manual_review`。**这是被修过的 bug**：原实现无上限会死循环，或超限后强行提交不可下单的订单。
3. **推荐和校验之间是双向的**，所以 `BaseAgent.ainvoke` 的 `recursion_limit` 设成 `节点数 × 4`。

### 6.2 KitchenAgent（7 节点）—— 后厨排产

```mermaid
flowchart TD
    L["load_orders<br/>加载待处理订单"] --> C{"check_inventory<br/>食材够不够"}
    C -- "充足 / 已重试过" --> G["generate_tasks<br/>拆解出餐任务"]
    C -- "缺料" --> L
    G --> S["schedule_tasks<br/>按优先级排产"]
    S --> CP{"check_priority<br/>超过阈值需主管确认"}
    CP -- "主管调整且未应用" --> S
    CP -- "否则" --> A["assign_stations<br/>分配工位"]
    A --> D["dispatch_orders<br/>下发 KDS"]
```

**两处回环的边界**：
- 缺料回 `load_orders` 有 `_MAX_INVENTORY_RETRY` 上限；超限后**带着缺料预警继续往下走**（不阻断出餐）
- `check_priority` 回 `schedule_tasks` 只在"主管确实调整了但尚未应用"时触发，避免无限重排

> 这两条回环都曾是"写了但从未生效"的死代码（回退到入口节点 / 两支都指向同一目标），
> 修复后才有真正的重试语义。看这段代码时注意 `adjustment_applied` 标志位。

### 6.3 ReviewAgent（7 节点）—— 评价分析

```mermaid
flowchart TD
    C["collect_reviews<br/>多平台评价采集"] --> S["classify_sentiment<br/>情感分析"]
    S --> T{"extract_topics<br/>主题提取"}
    T -- "涉及食品安全" --> Q["query_food_safety<br/>查食安法规"]
    T -- "一般话题" --> R["generate_report<br/>生成分析报告"]
    Q --> R
    R --> N{"check_negative<br/>有严重差评？"}
    N -- "店长要求重写" --> R
    N -- "否则" --> SV["save_analysis<br/>保存到 review_analysis"]
```

**为什么先查法规再出报告**：涉食安差评如果只给"服务态度"的分析是无效的，必须带上法规依据
（如《食品安全法》第 X 条）才能支撑后续处理与留痕。

### 6.4 SafetyAgent（9 节点）—— 食安知识问答（轻量 RAG）

```mermaid
flowchart TD
    CQ{"classify_query<br/>问题分类"} -- "general" --> GG["generate_general<br/>通用直答"]
    CQ -- "regulation / nutrition / allergen" --> RK["retrieve_knowledge<br/>检索内置知识库"]
    RK --> CC{"check_confidence<br/>检索命中？"}
    CC -- "命中" --> GA["generate_answer<br/>基于条款生成答案"]
    CC -- "未命中" --> WS["web_search<br/>联网兜底"]
    WS --> GF{"generate_fallback<br/>还能答吗？"}
    GF -- "可答" --> SV["save_memory<br/>落定 + 整理来源"]
    GF -- "不可答" --> EP["enqueue_pending<br/>转人工（附服务热线）"]
    GA --> SV
    GG --> SV
    EP --> SV
```

处理意图：`food_safety_query` / `nutrition_query`（**这是第四类 Agent 存在的理由**——
前三个 Agent 解决"营业动作"，它解决"知识问答"，两者的正确性标准完全不同）。

四个设计要点：

1. **分类先于检索**：`classify_query` 决定去查法规库、营养表还是过敏原规范，避免对全部知识做一次无关检索。
2. **置信不足就联网**：`check_confidence` 只看 `retrieval_hit`（本地是否命中），未命中才走 `web_search`，省一次外部调用。
3. **宁可转人工也不编**：`generate_fallback` 判断 `rescuable`（有内置知识或有联网结果），否则进 `enqueue_pending` 转人工并附服务热线。
   这是**幻觉控制在业务层的落点**——覆盖率换正确性。
4. **知识层是内置的、可插拔的**：数据来自 `tools/safety_knowledge.py`（法规要点 + 过敏原管理规范）与
   `tools/menu_data.py`（菜品营养表），不依赖任何外部服务；若要换成外部 RAG，只改 `tools/mcp_tools.py`
   里 `query_food_safety_law` / `query_nutrition` / `search_menu_knowledge` 三个函数的数据源即可。

---

## 七、工具层：为什么分四类

15 个工具分四类不是凑数，而是按"**错了代价多大**"划分：

| 类别 | 数量 | 特征 | 典型工具 | 错了会怎样 |
|------|------|------|---------|-----------|
| **LLM 工具** | 4 | 灵活但不精确 | `llm_recommend_dishes` | 推荐不合口味，可接受 |
| **规则引擎** | 4 | 精确但死板 | `allergen_detection` | 漏判过敏原 → **食品安全事故** |
| **MCP 工具** | 4 | 知识检索 | `query_food_safety_law` | 引用错法规 → 合规风险 |
| **外部 API** | 3 | 对接甲方系统 | `get_order_detail` | 数据不同步，可重试 |

**核心原则**：LLM 工具和规则引擎**不是替代关系，是互补关系**。

每个 LLM 工具内部都有规则降级分支，例如 `llm_classify_intent`：
- 正常：调 DeepSeek 解析出 `{people, taste, allergens, budget}`
- 降级：正则抽"X 人""不辣""花生过敏""预算 Y 元"

这就是为什么 `LLM_ENABLED=false` 时系统还能跑通全流程。

**调用方式**：节点里不直接 import 工具函数，统一走注册表

```python
from tools.registry import tool_registry
tool = tool_registry.get("allergen_detection")   # 名字拼错会立刻 KeyError
result = await tool(...)
```

`tools/registry.py` 还维护 `AGENT_OWNERSHIP`（工具归谁）和 `DESCRIPTIONS`（中文说明），
`GET /api/tools` 的数据就来自这里。

---

## 八、三层记忆与读写时机

```mermaid
flowchart TD
    subgraph 短期记忆
        CP["LangGraph Checkpointer<br/>thread_id 隔离<br/>存 State 快照"]
    end
    subgraph 长期记忆
        SP["store_preferences<br/>门店偏好"]
        CU["customer_profiles<br/>顾客画像"]
        RV["review_analysis<br/>评价分析"]
        IU["ingredient_usage<br/>食材消耗"]
    end
    subgraph 合规留痕
        CL["compliance_log<br/>3 年备查"]
        OL["order_status_log<br/>状态流转"]
    end
    REQ["一次任务请求"] --> CP
    REQ -->|"任务启动时读"| SP
    REQ -->|"任务启动时读"| CU
    REQ -->|"任务结束后写"| RV
    REQ -->|"后厨排产后写"| IU
    REQ -->|"过敏原告知时写"| CL
    REQ -->|"状态每次变更时写"| OL
```

### 读写时机表

| 时机 | 读/写 | 目标 | 代码位置 |
|------|-------|------|---------|
| 任务启动 | 读 | 门店偏好 + 顾客画像 → 注入 State | `orchestrator._load_memory()` |
| 每个节点执行后 | 写 | Checkpointer 快照（自动） | LangGraph 内部调 `memory/checkpointer.py` |
| 过敏原高风险 | 写 | `compliance_log`（含告知渠道/确认人/时间） | OrderAgent `confirm_allergen_notice` 节点 |
| 订单状态变更 | 写 | `order_status_log` | `memory/order_state.py` |
| 后厨排产完成 | 写 | `ingredient_usage`（原子累加） | KitchenAgent `generate_tasks` 节点 |
| 评价分析完成 | 写 | `review_analysis` | ReviewAgent `save_analysis` 节点 |
| 定时任务 | 删 | 按保留策略清理过期数据 | `scripts/purge_expired.py` |

### 为什么 Checkpointer 和业务表分开

| | Checkpointer | 业务表 |
|---|---|---|
| 存什么 | 运行时 State 快照（JSON 序列化） | 结构化业务事实 |
| 谁维护 | LangGraph 框架 | 项目自己的 SQL |
| 查询方式 | 只能按 thread_id 取，不给你写 SQL | 任意 SQL、索引、聚合 |
| 生命周期 | 90 天归档 | 按业务口径（永久 / 730 天 / 3 年） |

**关键认知**：Checkpointer 不是数据库，别指望用它做统计报表。
"这家店最近 30 天卖了哪些菜"必须查 `store_preferences` / `order_status_log`。

### 双层隔离怎么落到 SQL

```sql
-- 门店隔离：每条查询必带 tenant_id
WHERE tenant_id = 'store_001'

-- 员工级数据再叠加 user_id
WHERE tenant_id = 'store_001' AND customer_id = 'cust_888'
```

`memory/store.py` 的所有方法签名都以 `tenant_id` 为第一个参数，这是刻意的约束 ——
不给调用方"忘记隔离"的机会。

### 写入语义（本轮 PG 接入时修正过）

| 表 | 语义 | 实现 |
|----|------|------|
| `store_preferences` | 一店一行，覆盖 | `ON CONFLICT (tenant_id) DO UPDATE` |
| `customer_profiles` | 一人一行，字段合并 | `ON CONFLICT (tenant_id, customer_id)` + `GREATEST` 保留最大到店次数 |
| `ingredient_usage` | **原子累加** | `ON CONFLICT ... SET used_amount = 原值 + 增量` |

> 早期实现这几处都是纯 INSERT，会堆积重复行；`customer_profiles` 读取是 `LIMIT 1` 无 `ORDER BY`，
> 可能导致**过敏原读到旧画像** —— 属于安全相关缺陷，已修。

---

## 九、State 数据流：最容易踩坑的地方

这是 LangGraph 新手最常翻车的地方，务必看懂。

### 9.1 节点只返回"增量"

```python
async def parse_order(state):
    ...
    return {"customer_needs": {...}, "current_stage": "parsed"}   # 只返回改动的字段
```

### 9.2 但 LangGraph 对 dict schema 是"整状态替换"

如果直接把上面的函数交给 `add_node`，节点返回后 State 就只剩这两个字段，
前面注入的 `store_preferences`、`tenant_id` 全丢了 → 条件边读不到数据 → 死循环或行为诡异。

### 9.3 所以基类包了一层 `_wrap_node`

```python
async def _wrapped(state):
    update = await node_func(state)      # 拿到增量
    if isinstance(update, dict):
        state.update(update)             # 合并进完整 State
    return state                         # 返回完整 State
```

**这个包装同时解决了降级问题**：顺序执行器直接调原始 `node_func`（自己 `state.update`），
LangGraph 走 `_wrapped`，两边语义一致。

### 9.4 State 里有哪些字段

`agents/state.py` 的 `initial_state()` 给出基础字段：

| 字段 | 作用 |
|------|------|
| `tenant_id` / `user_id` / `role` | 隔离键与角色 |
| `thread_id` | Checkpointer 隔离键 |
| `user_input` / `messages` | 输入与历史 |
| `store_preferences` / `customer_history` | 注入的长期记忆 |
| `current_stage` | 当前阶段（终态校验依赖它） |
| `errors` / `degraded_tools` | 可观测性：错误与降级留痕 |

各 Agent 还会往里塞自己的字段（`recommended_dishes`、`order_valid`、`pending_orders` 等），
`**extra` 允许 `initial_state` 接收任意扩展字段。

### 9.5 终态校验（防静默失败）

LangGraph 有个坑：条件边目标不可达时会**静默结束**，不抛异常。
表现为"只跑了前三个节点就返回了"，很难发现。

`BaseAgent._reached_terminal()` 用 `_FINAL_STAGES` 表检查：

```python
_FINAL_STAGES = {
    "OrderAgent": {"submitted", "manual_review_required"},
    "KitchenAgent": {"dispatched"},
    "ReviewAgent": {"negative_checked", "saved"},
    "SafetyAgent": {"saved"},
}
```

没走到终态 → 打 warning 并用顺序执行器补跑一次。**加新 Agent 时记得往这张表里登记**。

---

## 十、降级设计：没有依赖也能跑

这是本项目的工程亮点，也是答辩常被问的点。

```mermaid
flowchart TD
    Q{"langgraph 装了吗？"} -- 有 --> G["走 LangGraph 状态图<br/>支持条件边/Checkpointer/interrupt"]
    Q -- 无 --> S["顺序执行器<br/>条件边退化为 if/else"]
    G --> T{"走到终态了吗？"}
    T -- 是 --> OK["返回结果"]
    T -- "否（静默中断）" --> S
    S --> OK
    G -.异常.-> S
```

### 完整降级矩阵

| 依赖 | 缺失表现 | 降级方案 | 代码位置 |
|------|---------|---------|---------|
| `langgraph` | ImportError | 顺序执行器 | `base_agent._build()` / `_run_sequential()` |
| LangGraph 静默中断 | 未达终态 | 顺序执行器补跑 | `base_agent._reached_terminal()` |
| DeepSeek API | 超时/401/限流 | 规则引擎分支 | `tools/llm_client.py` + 各 LLM 工具内部 |
| DeepSeek 余额不足（402） | 账户级失败 | **不重试、不切备用模型**，直接降级规则引擎 | `llm_client._is_account_level_error()` |
| 主模型不可用 | 重试耗尽（仅可重试类错误） | 切备用模型（`.env` 可配） | `llm_client` fallback 逻辑 |
| PostgreSQL | 连不上 | 长期记忆 → JSON 文件<br>Checkpointer → 内存 | `memory/store.get_store()` / `checkpointer.get_checkpointer()` |
| Redis | 连不上 | 进程内字典缓存 | `tools/cache.init()` |
| 外部知识源 | 未接入 | 内置知识库（`safety_knowledge` + `menu_data`） | `tools/mcp_tools.py` |
| 联网搜索 | 失败/超时 | 返回缓存结果（TTL 24h） | `tools/mcp_tools.web_search()` |
| POS / KDS | 未接入 | Mock 数据 | `tools/external_api.py` |
| Milvus | 未安装 | 关键词匹配 | `tools/mcp_tools.py` |
| LangSmith / Prometheus | 未启用 | no-op | `tools/tracing.py` / `metrics.py` |

**降级的可观测性**：所有降级都会进 `state["degraded_tools"]`，最终出现在 API 响应的
`degraded_tools` 字段里。前端和日志都能看到"这次哪些能力没用上"。

> 注意：PG 不可用时，`checkpointer.py` 和 `store.py` 会各自独立降级 ——
> 可能出现"长期记忆用文件、短期记忆用内存"的混合状态，日志里会分别打印。

---

## 十一、代码索引：想改 X 去哪

| 我想…… | 去改 |
|---------|------|
| 加一个意图 | `base/config.py` 的 `INTENT_ROUTES` + `orchestrator.py` 的 `INTENT_KEYWORDS` |
| 调意图路由到别的 Agent | `config.INTENT_ROUTES[intent]["agent"]` |
| 加一个 Agent 节点 | `agents/<agent>/nodes.py` 写函数 → `graph.py` 的 `define_nodes()` 登记 → `define_structure()` 连边 |
| 改节点执行顺序 / 分支条件 | `agents/<agent>/graph.py` 的 `define_structure()` |
| 改 Prompt | `prompts/templates.py`（全局）或 `agents/<agent>/prompts.py`（节点级） |
| 加一个工具 | `tools/{llm,rule,mcp,external}_tools.py` 写函数 → `registry.py` 的 `AGENT_OWNERSHIP` / `DESCRIPTIONS` 登记 |
| 改过敏原库 | `base/config.py` 的 `ALLERGEN_LIBRARY` / `HIGH_RISK_ALLERGENS` |
| 加一张记忆表 | `memory/schemas.py` 加 DDL → `ALL_DDL` / `TABLES` → `store.py` 加读写方法 |
| 改表结构 | `memory/schemas.py`（唯一来源）→ 重新导出 `docs/deploy/schema.sql` |
| 改记忆保留策略 | `config.ini [memory]` 或 `.env` 的 `MEMORY_RETENTION_*` |
| 加接口 | `api/routes.py` + `api/models.py` |
| 改鉴权 / 账号 | `api/auth.py` |
| 改排队阈值 / 工位 | `config.ini [kitchen]` |
| 调 LLM 模型 / 超时 | `.env` 的 `LLM_*` |
| 开关可选组件 | `.env` 的 `PG_ENABLED` / `REDIS_ENABLED` / `LANGSMITH_ENABLED` / `PROMETHEUS_ENABLED` |
| 换知识源（接外部 RAG/向量库） | `tools/mcp_tools.py` 里 `search_menu_knowledge` / `query_food_safety_law` / `query_nutrition` 的数据源 |
| 改部署端口 | `config.ini [server]` 或 `.env APP_PORT`；compose 用 `PG_HOST_PORT` |

> 改表结构后记得同步：`python -c "from memory.schemas import init_sql; open('docs/deploy/schema.sql','w',encoding='utf-8').write(init_sql())"`

---

## 十二、调试指南

### 12.1 启动日志看什么

正常启动应该出现：

```
[Checkpointer] 使用 PostgresSaver（按 thread_id 隔离，持久化到 PostgreSQL）
[启动] PostgreSQL  : 启用 localhost:5432/foodagent
[启动] 意图路由    : 9 类
[启动] 过敏原库    : 65 条
[记忆系统] 使用 PostgreSQL 长期记忆仓库
[启动] 长期记忆仓库: PostgresStore
[启动] 短期记忆检查点器: PostgresSaver
[缓存] Redis 缓存已启用：localhost:6379/db2
[启动] 工具注册    : 共 15 个 {'LLM 工具': 4, '规则引擎': 4, 'MCP 工具': 4, '外部 API 封装': 3}
[启动] LangSmith   : 已启用 project=foodagent-dev
[启动] Prometheus  : 已启用 /metrics
启动完成。4 个 Agent / 15 个工具 / 9 类意图 / 18 个接口 已就绪
```

**异常信号对照**：

| 日志 | 说明 |
|------|------|
| `使用 MemorySaver` | PG 没连上，或 langgraph-checkpoint-postgres 未装 |
| `使用文件长期记忆仓库` | PG 没连上，去看 `[PG] ... 初始化失败` 的 warning |
| `PostgresSaver 初始化失败` | 检查 DSN、PG 是否启动、密码对不对 |
| `Redis 连接失败` | redis 容器没起；启动会因此慢约 48 秒 |
| `工具数量应为 15` | 注册表被改坏了 |
| `状态图未到达终态` | 条件边写错了，去 `define_structure()` 查 |

### 12.2 单次请求怎么追

整个链路都有 `logger.info`，按 `thread_id` 串联：

```bash
# Windows
findstr "thread=store_001_waiter_01" logs\app.log

# 或者直接看最近 200 行
powershell -Command "Get-Content logs\app.log -Tail 200"
```

关键日志顺序：
```
[Orchestrator] 任务开始 thread=...
[Orchestrator] 用户输入：...
[Orchestrator] LLM 识别意图：[...]（Pipeline=False）
[Orchestrator] 单 Agent 路由：dish_recommend → OrderAgent
[OrderAgent] LangGraph 状态图构建完成：10 个节点...
[API] /api/chat 完成 耗时 2.3s 意图=[...] 降级=[]
```

### 12.3 查库确认数据真落进去了

```bash
docker exec -it foodagent_postgres psql -U postgres -d foodagent
```

```sql
\dt                                              -- 看所有表
SELECT count(*) FROM order_status_log;           -- 订单流转有没有写
SELECT tenant_id, order_id, to_status FROM order_status_log ORDER BY id DESC LIMIT 5;
SELECT thread_id, count(*) FROM checkpoints GROUP BY 1;   -- 对话快照
SELECT * FROM compliance_log ORDER BY id DESC LIMIT 3;    -- 合规留痕
```

### 12.4 常见症状 → 病因

| 症状 | 最可能的原因 | 排查入口 |
|------|------------|---------|
| 对话没有上下文，每轮都"失忆" | thread_id 没传 / 每次新生成 | `routes.chat()` 的 `req.thread_id` |
| 推荐完就结束，没有后续节点 | 条件边目标缺失 → 静默中断 | `_reached_terminal` 的 warning |
| 订单被硬提交，但其实不可下单 | `validate_order` 重试逻辑异常 | `_MAX_RECOMMEND_RETRY` 与条件边 |
| 过敏原查不到 | 菜品配方没配 allergens 字段 | `tools/menu_data.py` |
| 同一顾客过敏原"丢了" | 画像读到旧行（历史上会） | `uq_customer_tenant` 唯一索引是否在 |
| 启动特别慢（约 48 秒） | Redis 没起，连接重试 | `docker start redis` |
| 接口 401 | JWT 过期或未带 Header | `api/auth.py`，SSE 用 query 传 token |
| 前端页面空白 | static 目录未挂载 / index.html 缺失 | `api/app.py` 静态挂载段 |

### 12.5 用 CLI 快速试节点逻辑

```bash
python run_cli.py          # 不进 HTTP 层，直接在终端跟 Agent 对话
```

比每次都起服务、登录、发请求快得多，改 Prompt 或节点逻辑时首选。

---

## 十三、上手验证清单

按顺序跑一遍，能确认环境、记忆、工具、降级都是通的：

```bash
cd D:\heima\FoodAgent

# 1) 基础设施（应看到两个 Up）
docker ps --filter name=foodagent_postgres --filter name=redis --format "{{.Names}} {{.Status}}"

# 2) 环境自检（Python / 依赖 / 配置 / 密钥）
conda activate system
python main.py --check

# 3) 项目完整性（6 个维度）
python scripts/selfcheck.py

# 4) 起服务
python main.py
```

另开一个终端：

```bash
# 5) 健康检查：确认 checkpointer=PostgresSaver
curl -s http://127.0.0.1:8090/api/health

# 6) 登录拿 token
curl -s -X POST http://127.0.0.1:8090/api/auth/login \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"waiter_01\",\"password\":\"123456\"}"

# 7) 一次真实对话（把 <TOKEN> 换成上一步的值）
curl -s -X POST http://127.0.0.1:8090/api/chat \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <TOKEN>" \
  -d "{\"query\":\"推荐几个招牌菜\",\"session_id\":\"demo_01\"}"

# 8) 18 接口冒烟
python scripts/test_api.py
```

**验收标准**：
- 步骤 5 返回 `"checkpointer": "PostgresSaver"`
- 步骤 7 返回带菜品清单与过敏原警示的 Markdown
- 步骤 8 全绿
- PG 里 `order_status_log`、`store_preferences` 有数据

---

## 附：术语表

| 术语 | 含义 |
|------|------|
| **State** | LangGraph 的运行时数据载体，本项目用 dict |
| **节点（node）** | 状态图中一个执行单元，读 state → 返回增量 |
| **条件边** | 根据 state 决定下一步走哪个节点 |
| **Checkpointer** | 状态快照存储器，按 thread_id 隔离 |
| **thread_id** | 一次任务/会话的隔离键 |
| **Pipeline** | 多个 Agent 在一次请求内串行执行 |
| **HITL** | Human-in-the-Loop，需要人工确认的环节 |
| **降级** | 依赖不可用时切换到备用实现，而不是报错 |
| **tenant_id** | 门店 ID，数据隔离的第一道边界 |
| **DSN** | 数据库连接串 `postgresql://user:pwd@host:port/db` |
