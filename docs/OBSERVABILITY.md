# 可观测性与重型组件接入指南

> 适用范围：FoodAgent 的 5 个**可选重型组件**（LangSmith / Redis / Prometheus / Locust / K8s）
>
> **核心原则：全部默认关闭。**  
> 不安装任何依赖、不改任何配置，主流程（意图路由 → 三 Agent → 记忆）100% 可跑通。  
> 需要时按本文档开启开关 + 装对应依赖即可 —— 属于「渐进增强」，不是「必须依赖」。

---

## 一、总览

| 组件             | 作用                     | 依赖包                 | 默认开关    | 关闭时的降级行为                |
| -------------- | ---------------------- | ------------------- | ------- | ----------------------- |
| **LangSmith**  | 全链路追踪（节点耗时、LLM IO、工具链） | `langsmith`         | `false` | 不追踪；`traceable` 变成恒等装饰器 |
| **Redis**      | 热点缓存（门店偏好/画像/检索结果）     | `redis`             | `false` | 降级为进程内字典缓存（单机可用）        |
| **Prometheus** | 指标暴露 `/metrics`        | `prometheus-client` | `false` | 所有记录函数为 no-op           |
| **Locust**     | 并发压测                   | `locust`            | `false` | 不影响服务；仅脚本存在             |
| **Kubernetes** | 多副本编排                  | 无（用 kubectl）        | `false` | 用 docker-compose 单机部署   |

### 落地状态速查

| 项目                                        | 状态                                                    |
| ----------------------------------------- | ----------------------------------------------------- |
| 配置段（`config.ini` 五段 + `base/config.py` 读取）  | ✅ 已完成（全部支持环境变量覆盖）                                     |
| 功能模块（`tracing.py` / `cache.py` / `metrics.py`） | ✅ 已完成（均含 `__main__` 自检块）                               |
| 启动接入（`api/app.py` 的 `lifespan` 第 ⑥ 步）       | ✅ 已完成（三组件统一初始化，失败只 warning 不阻塞启动）                      |
| `/metrics` 端点                             | ✅ 已完成（`api/app.py`，未启用时返回说明文本而非 404）                   |
| 依赖包                                       | ✅ 已随 `requirements.txt` 登记（见文件「可选重型组件」段；实际环境均已安装）        |
| 压测脚本（`scripts/locustfile.py`）             | ✅ 已完成（`locust --list` 可正常加载 2 个用户类）                     |
| K8s 清单（`docs/deploy/k8s/foodagent.yaml`） | ✅ 已完成（8 个资源，`yaml.safe_load_all` 全部解析通过）              |
| 汇总验证（`scripts/verify_observability.py`）  | ✅ 63 项全通过                                             |

> **注**：上表「默认开关」指配置文件里的初始值。实际是否生效由 `config.ini` 决定，
> 且**开关与依赖是两件独立的事** —— 开了开关但没装依赖，仍会降级。
> 用 `scripts/verify_observability.py` 的三态矩阵可一眼看出是哪一种情况。

---

## 二、LangSmith 全链路追踪

### 为什么需要

LangGraph 多节点 + 多级降级链的架构下，"回答为什么变慢了""哪一环降级了"靠日志很难定位。  
LangSmith 提供树状 trace：一次请求 → 每个 Agent → 每个节点 → 每次 LLM/工具调用的完整时序。

### 启用步骤

```bash
# 1. 安装依赖
D:/ruanjian/anaconda/envs/system/python.exe -m pip install langsmith langchain-core

# 2. 申请 API Key：https://smith.langchain.com → Settings → API Keys

# 3. 修改配置（config.ini）
[langsmith]
enabled = true
api_key = lsv2_pt_xxxxxxxxxxxx
project = foodagent-dev
sample_rate = 1.0        # 生产建议 0.1~0.3 降低开销

# 4. 重启服务，确认日志
# [追踪] LangSmith 追踪已启用：project=foodagent-dev endpoint=...
```

### 代码接入方式

```python
from tools.tracing import init_tracing, traceable

# 服务启动时初始化一次（api/app.py 的 lifespan 启动阶段已内置调用，无需手动加）
init_tracing()

# 给关键函数加追踪（未启用时是恒等装饰器，零开销）
@traceable("allergen_detection", "tool")
def allergen_detection(dish, allergens):
    ...
```

> 三个重型组件的初始化统一挂在 `api/app.py` 的 `lifespan` **启动阶段第 ⑥ 步**
> （在配置自检、记忆预热、工具校验、FSS 探测之后），任一失败只 warning、不阻塞启动。
> 启动日志会明确打印每个组件是「已启用」还是「未启用（原因）」。

### 验证

```bash
D:/ruanjian/anaconda/envs/system/python.exe tools/tracing.py
# 期望输出：初始化结果 + 恒等装饰器校验通过
```

### 排查

| 现象                        | 原因                    | 处理                       |
| ------------------------- | --------------------- | ------------------------ |
| 日志提示 `missing_dependency` | 未安装 `langsmith`       | 执行 pip install           |
| 日志提示 `missing_api_key`    | config.ini 未填 api_key | 填写 `[langsmith] api_key` |
| 平台上看不到 trace              | `sample_rate` 太低      | 临时改为 1.0                 |
| 服务启动变慢                    | 追踪初始化阻塞               | 已做成异步惰性初始化，一般无影响         |

---

## 三、Redis 缓存

### 为什么需要

三个高频重复查询会打满外部依赖：

1. **门店偏好 / 顾客画像** —— 每次任务启动都读一次（`_load_memory`）
2. **MCP 检索结果** —— 同一道菜的过敏原/营养被反复查
3. **食品安全法规** —— 法规文本几乎不变，却每次回查 FSS

### 启用步骤

```bash
# 1. 安装依赖
D:/ruanjian/anaconda/envs/system/python.exe -m pip install redis

# 2. 启动 Redis
#    若本机 6379 已被其他项目的 Redis 占用，直接复用即可（推荐用 db 分库隔离）：
docker ps | grep 6379                          # 先确认是否已有实例
#    没有的话再自建：
docker run -d --name foodagent-redis -p 6379:6379 redis:7-alpine

# 3. 修改配置
[redis]
enabled = true
host = localhost
port = 6379
db = 2                       # 与同机其他项目分库隔离（本机约定：db0=其他项目 / db1=FSS / db2=FoodAgent）
password =                   # 复用已有实例时填它的密码；自建无密码则留空
default_ttl = 300            # 门店偏好 5 分钟足够
key_prefix = foodagent:

# 4. 重启服务，确认日志
# [缓存] Redis 缓存已启用：localhost:6379/db2 prefix=foodagent:
```

> **复用已有 Redis 时的两个注意点**
> 1. **必须分 db + 加 `key_prefix` 双重隔离** —— 多个项目共用同一实例时，
>    若目标实例设了 `maxmemory-policy allkeys-lru`，任何 key 都可能被别的项目挤掉。
> 2. **不要对该实例执行 `flushall` / `flushdb`** —— 里面可能有其他项目的业务数据，
>    清理只清自己的前缀：`cache.clear("foodagent:")`。

### 代码接入方式

```python
from tools.cache import cache, KEY_STORE_PREFS

# 读缓存
key = f"{KEY_STORE_PREFS}{tenant_id}"
hit = cache.get(key)
if hit is None:
    hit = await store.load_store_preferences(tenant_id)
    cache.set(key, hit, ttl=300)

# 或用装饰器（异步函数）
from tools.cache import cached

@cached("nutrition", ttl=600)
async def query_nutrition_cached(dish_name):
    return await query_nutrition(dish_name)
```

### 三级降级

| 层级 | 触发条件        | 行为                       |
| -- | ----------- | ------------------------ |
| L1 | Redis 未启用   | 进程内字典缓存（单机可用，重启失效）       |
| L2 | Redis 启用且连通 | Redis + 进程内双层写，读优先 Redis |
| L3 | Redis 中途故障  | 自动回退进程内，业务不中断            |

> **重要**：Redis 缓存是**性能优化**，不是**数据源**。  
> 权威数据始终在 PG / FileStore，缓存失效最多导致一次回源。

### 验证

```bash
D:/ruanjian/anaconda/envs/system/python.exe tools/cache.py
# 期望输出：读写/TTL/删除/统计 全部校验通过
```

---

## 四、Prometheus 指标

### 暴露的指标

| 指标名                                        | 类型        | 标签               | 说明                |
| ------------------------------------------ | --------- | ---------------- | ----------------- |
| `foodagent_requests_total`                 | Counter   | endpoint, status | HTTP 请求总数         |
| `foodagent_request_duration_seconds`       | Histogram | endpoint         | 请求耗时分布            |
| `foodagent_agent_duration_seconds`         | Histogram | agent, stage     | Agent 阶段耗时        |
| `foodagent_llm_calls_total`                | Counter   | model, result    | LLM 调用次数（含降级）     |
| `foodagent_tool_degraded_total`            | Counter   | tool             | **工具降级次数（核心告警项）** |
| `foodagent_cache_operations_total`         | Counter   | op, result       | 缓存命中率             |
| `foodagent_order_status_transitions_total` | Counter   | to_status        | 订单状态跃迁分布          |
| `foodagent_active_requests`                | Gauge     | —                | 当前并发请求数           |

> **`result` 标签取值**：`success`（主模型成功）／**`success_fallback`（主模型失败后切换备用模型成功，需求「主模型不可用切备用模型」）**／`degraded`（降级规则引擎）／`error`（主模型与备用模型均失败）。
> `model` 标签一律用**主模型名**，便于按「服务」聚合；是否走了备用模型看 `result`。
> 因此告警表达式 `result="degraded"` 的占比**不包含**已验证可用的备用模型路径。

### 启用步骤

```bash
# 1. 安装依赖
D:/ruanjian/anaconda/envs/system/python.exe -m pip install prometheus-client

# 2. 修改配置
[prometheus]
enabled = true
metrics_path = /metrics
namespace = foodagent
node_level_metrics = true

# 3. 重启服务，验证
curl --noproxy '*' http://127.0.0.1:8090/metrics | grep foodagent_
# ⚠️ 本机 curl 必须加 --noproxy '*'，否则系统代理会拦截 localhost 请求
#    （表现为 upstream connect failed 或空返回）

# 未启用时的表现：端点仍返回 200，但内容是一段说明文本（不是 404）——
# 这样 Prometheus 抓取端能区分「服务没跑」和「指标没开」。
```

### 端点接入方式

`/metrics` 路由定义在 `api/app.py`：

```python
@app.get(config.PROMETHEUS_METRICS_PATH, include_in_schema=False)
async def metrics():
    from tools.metrics import is_enabled, render
    if not is_enabled():
        return Response(content="# Prometheus 指标未启用\n...", media_type="text/plain; charset=utf-8")
    body, content_type = render()      # render() 内部调 prometheus_client.generate_latest()
    return Response(content=body, media_type=content_type)
```

### Prometheus 抓取配置

```yaml
# prometheus.yml
scrape_configs:
  - job_name: 'foodagent'
    metrics_path: '/metrics'
    static_configs:
      - targets: ['foodagent-api:8090']
    scrape_interval: 15s
```

### 建议告警规则

```yaml
groups:
  - name: foodagent
    rules:
      # 工具降级率突增 → 外部依赖（FSS/Milvus）可能挂了
      - alert: HighToolDegradation
        expr: rate(foodagent_tool_degraded_total[5m]) > 1
        for: 5m
        annotations:
          summary: "工具降级频繁：{{ $labels.tool }}"

      # P95 延迟超过 5 秒 → 用户体验受损
      - alert: SlowRequests
        expr: histogram_quantile(0.95, rate(foodagent_request_duration_seconds_bucket[5m])) > 5
        for: 5m
        annotations:
          summary: "P95 延迟过高"

      # LLM 降级占比过高 → 检查 API Key / 额度
      - alert: HighLLMDegradation
        expr: rate(foodagent_llm_calls_total{result="degraded"}[5m])
              / rate(foodagent_llm_calls_total[5m]) > 0.3
        for: 5m
        annotations:
          summary: "LLM 降级率超过 30%"
```

### 验证

```bash
D:/ruanjian/anaconda/envs/system/python.exe tools/metrics.py
# 期望输出：no-op 调用校验通过（未启用时不抛异常）
```

---

## 五、Locust 压测

### 启用步骤

```bash
# 1. 安装依赖
D:/ruanjian/anaconda/envs/system/python.exe -m pip install locust

# 2. 确保服务已启动（另开一个终端）
D:/ruanjian/anaconda/envs/system/python.exe main.py

# 3. Web UI 模式（推荐，可视化曲线）
D:/ruanjian/anaconda/envs/system/python.exe -m locust \
    -f scripts/locustfile.py --host http://127.0.0.1:8090
# 浏览器打开 http://localhost:8089，填并发数后 Start

# 4. 无头模式（CI / 出报告）
D:/ruanjian/anaconda/envs/system/python.exe -m locust \
    -f scripts/locustfile.py --host http://127.0.0.1:8090 \
    --headless -u 50 -r 5 -t 120s \
    --html logs/loadtest_report.html --csv logs/loadtest
```

### 压测画像

| 用户类             | 权重 | 模拟对象 | 行为                           |
| --------------- | -- | ---- | ---------------------------- |
| `FoodAgentUser` | 主体 | 服务员  | 对话（40% 单意图 + 8% 全流程）、排产、健康检查 |
| `KitchenUser`   | 辅助 | 后厨主管 | 排产、库存、后厨问答                   |

语料覆盖 **9 类意图 + 1 类 Pipeline**，保证路由分支都被压到。

### 关键注意

- **压测会真实写库**（门店偏好、食材消耗、合规留痕），  
  默认使用独立门店 `store_loadtest`，**切勿指向生产门店**
- 压测前建议先跑一轮 `scripts/selfcheck.py` 确认基线正常
- Pipeline 场景（全流程）是最重的请求，权重刻意压到 8%，避免拖垮整轮压测

### 关注指标

| 指标       | 健康阈值      | 说明                   |
| -------- | --------- | -------------------- |
| P95 响应时间 | < 3000 ms | 无 LLM 时应 < 800 ms    |
| 失败率      | < 1%      | 401/5xx 都算失败         |
| RPS      | 视部署规模     | 单副本无 LLM 约 20~50 RPS |

---

## 六、Kubernetes 部署

详见 [`docs/deploy/k8s/README.md`](./deploy/k8s/README.md) 与  
[`docs/deploy/k8s/foodagent.yaml`](./deploy/k8s/foodagent.yaml)。

### 一句话结论

**本地开发不需要 K8s。** 只有当需要「多副本 + 自动扩缩容」时才有必要，  
且必须先开启 `PG_ENABLED=true`（多副本共享状态的前提）。

### 多副本的三个坑（务必注意）

1. **进程内锁失效** —— `memory/store.py` 的 `_WRITE_LOCKS` 是进程内的，  
   多副本下不同 Pod 各有一把锁，无法互斥 → 必须开 PG 用数据库原子写
2. **SSE 断流** —— `/api/chat/stream` 长连接需会话保持  
   （`sessionAffinity: ClientIP` 或 Ingress cookie 亲和）
3. **FileStore 不共享** —— 不同 Pod 的 `data/memory_store/*.json` 是各自独立的，  
   PG 关闭时多副本数据会分裂 → 生产必须 `PG_ENABLED=true`

---

## 七、一键自检

所有组件的自检脚本，可在开启前后各跑一次对比：

```bash
cd D:/heima/FoodAgent
export PYTHONPATH=/d/heima/FoodAgent          # ⚠️ 不加会报 ModuleNotFoundError: No module named 'base'
PY=D:/ruanjian/anaconda/envs/system/python.exe

$PY tools/tracing.py     # LangSmith
$PY tools/cache.py       # Redis / 缓存
$PY tools/metrics.py     # Prometheus
$PY scripts/selfcheck.py --no-e2e             # 全系统自检（含节点数/工具数/表结构）
$PY scripts/verify_observability.py           # 可观测性汇总（63 项）
```

### 预期输出

| 脚本                      | 正常输出                             |
| ----------------------- | -------------------------------- |
| `tools/tracing.py`      | `自检通过。`（含初始化结果 + 恒等装饰器校验）        |
| `tools/cache.py`        | `自检通过。`（含读写/TTL/删除/统计）           |
| `tools/metrics.py`      | `自检通过。`（含 no-op 调用校验）            |
| `scripts/selfcheck.py`  | `通过 48 · 警告 0 · 失败 0` → `可正常启动`  |
| `verify_observability.py` | `63 PASS / 0 FAIL`               |

`scripts/verify_observability.py` 会打印所有组件的「配置开关 / 依赖可用性 / 运行时状态」
三态矩阵，用于快速判断「是没开开关，还是没装依赖」：

```
  组件            配置开关      依赖可用      运行状态
  LangSmith     True      True      True
  Redis         True      True      True
  Prometheus    True      True      True
  Locust        True      True      False     <- 纯脚本，无「运行状态」概念
  K8s           False     False     False
```

> **说明**：该脚本的断言**不依赖当前开关值**（会临时改配置验证关闭态行为再还原），
> 因此无论你把 `enabled` 拨到 `true` 还是 `false`，结论都正确。

---

## 八、配置项速查

`config.ini` 的五个新段（初始默认 `enabled = false`）：

```ini
[langsmith]      enabled / api_key / project / endpoint / sample_rate
[redis]          enabled / host / port / db / password / default_ttl / key_prefix
[prometheus]     enabled / metrics_path / namespace / node_level_metrics
[loadtest]       enabled / users / spawn_rate / run_time / tenant_id / user_id
[k8s]            enabled / namespace / replicas / cpu_* / memory_* / hpa_*
```

所有配置项都支持 **环境变量覆盖**（优先级：`.env` > `config.ini` > 代码 fallback），  
K8s 环境通过 ConfigMap/Secret 注入环境变量即可，无需挂载配置文件。

环境变量名与属性名同名，例如：

| 属性                 | 环境变量                  |
| ------------------ | --------------------- |
| `REDIS_ENABLED`    | `REDIS_ENABLED`       |
| `REDIS_PORT`       | `REDIS_PORT`          |
| `K8S_REPLICAS`     | `K8S_REPLICAS`        |
| `LANGSMITH_API_KEY` | `LANGSMITH_API_KEY`  |

### 本机当前状态备忘

| 组件          | `enabled` | 依赖 | 实际生效 | 备注                              |
| ----------- | --------- | -- | ---- | ------------------------------- |
| LangSmith   | `true`    | 已装 | 是    | project=`foodagent-dev`         |
| Redis       | `true`    | 已装 | 是    | 复用本机已有实例，`db=2`，密码 `123456`     |
| Prometheus  | `true`    | 已装 | 是    | `/metrics` 可访问                 |
| Locust      | `false`   | 已装 | —    | 需要时才开                          |
| K8s         | `false`   | —  | —    | 本地开发不需要                         |

> **Redis db 分库约定（本机）**：`db0` = 其他项目 / `db1` = FoodSafetySystem /
> `db2` = FoodAgent。共用一个实例时必须遵守，否则 key 会互相干扰。

---

## 九、记忆清理任务的外部调度

需求 3.6.5「记忆清理策略」：**对话 90 天归档 / 门店偏好永久 / 顾客画像永久 /
评价分析 2 年**（合规留痕另按 3 年备查）。清理**不常驻进程**，由外部定时任务调用
`scripts/purge_expired.py` 即可（服务无需重启、无新增依赖）。

### 保留口径与配置

| 数据域 | 默认保留 | 配置项（`config.ini` 的 `[memory]` / 环境变量） |
| --- | --- | --- |
| `conversation`（对话记忆，Checkpointer） | 90 天 | `conversation_retention_days` / `MEMORY_RETENTION_CONVERSATION_DAYS` |
| `review_analysis`（评价分析） | 730 天（2 年） | `review_retention_days` / `MEMORY_RETENTION_REVIEW_DAYS` |
| `order_status_log`（订单状态流转日志） | 90 天 | `order_log_retention_days` / `MEMORY_RETENTION_ORDER_LOG_DAYS` |
| `compliance_log`（合规留痕） | 3 年 | `[compliance] log_years` / `COMPLIANCE_LOG_YEARS` |
| `store_preferences`（门店偏好） | **永久**（0） | `store_pref_retention_days`（0 = 永久，不要改） |
| `customer_profiles`（顾客画像） | **永久**（0） | `customer_retention_days`（0 = 永久，不要改） |

> **值 `0` = 永久保留，不清理**。门店偏好与顾客画像在代码里被**强制**跳过
> （`memory/store.py` 的 `PERMANENT_DOMAINS`），即使有人误配成非 0 也不会被删。

### 手动执行（先干跑再实跑）

```bash
cd D:/heima/FoodAgent
export PYTHONPATH=/d/heima/FoodAgent              # ⚠️ 不加会报 No module named 'base'
PY=D:/ruanjian/anaconda/envs/test_env/python.exe  # 见「本机当前状态备忘」的实际解释器路径

$PY scripts/purge_expired.py --dry-run            # 只统计不删除（先看会删多少）
$PY scripts/purge_expired.py                      # 全部门店/全表按策略清理
$PY scripts/purge_expired.py --tenant store_001   # 只清理指定门店
```

退出码：`0` = 全部成功；`1` = 有数据域清理失败（详见输出与 `logs/app.log`）。

### 挂到外部定时任务

**Windows 计划任务**（每天凌晨 03:30 清理，一行注册）：

```powershell
schtasks /Create /TN "FoodAgent-PurgeExpired" /SC DAILY /ST 03:30 /TR "cmd /c cd /d D:\heima\FoodAgent && set PYTHONPATH=D:\heima\FoodAgent && D:\ruanjian\anaconda\envs\system\python.exe scripts\purge_expired.py >> logs\purge_expired.log 2>&1" /F
```

**cron / Linux 容器**（每天 03:30 清理，一行 crontab）：

```bash
30 3 * * * cd /opt/FoodAgent && PYTHONPATH=/opt/FoodAgent /usr/bin/python3 scripts/purge_expired.py >> logs/purge_expired.log 2>&1
```

> **K8s 部署**：多副本下 FileStore 不共享，清理任务应挂成独立 `CronJob`
> （或只在单副本上执行），且生产必须先开 `PG_ENABLED=true`，让清理走 SQL 全表删除。
> **建议**：任务上线前先跑一周 `--dry-run` 观察条数，确认没有误删再切换为实跑。

