# FoodAgent 运行与部署手册

本文档面向**部署与运维**，包含环境准备、三种运行方式、PostgreSQL 集成、知识层与可选组件、故障排查。

---

## 一、环境准备

### 1.1 依赖检查

```bash
cd D:\heima\FoodAgent
python main.py --check
```

输出示例（全部 `[OK]` 即可启动）：

```
[OK]   Python 版本 3.11
[OK]   依赖 fastapi          Web 框架
[OK]   依赖 uvicorn          ASGI 服务器
[OK]   依赖 pydantic         数据校验
[OK]   依赖 jwt              JWT 认证（PyJWT）
[OK]   依赖 dotenv           环境变量加载
[OK]   可选 langgraph        LangGraph 状态图
[OK]   可选 openai           LLM 调用
[OK]   可选 httpx            HTTP 客户端
[OK]   可选 psycopg          PostgreSQL 驱动
[OK]   配置文件 config.ini
[OK]   配置文件 .env
[OK]   LLM API Key 已配置
[OK]   意图路由 9 类 · 过敏原库 65 条
```

### 1.2 依赖安装（含虚拟环境）

```bash
python -m venv venv
venv\Scripts\activate                       # Windows
pip install -r requirements.txt
```

**最小依赖集**（若只需跑通主流程）：
```bash
pip install fastapi uvicorn pydantic python-dotenv PyJWT openai httpx
```
`langgraph` / `psycopg` 均为可选——缺失时自动降级。

> **本机现成环境（推荐直接用）**：`D:\ruanjian\anaconda\envs\system`（Python 3.10.22）已装齐
> `fastapi 0.115.12 / uvicorn 0.41.0 / langgraph 1.0.8 / openai / httpx / psycopg 3.2.10 / PyJWT / python-dotenv`，
> 可直接：
> ```powershell
> cd D:\heima\FoodAgent
> D:\ruanjian\anaconda\envs\system\python.exe main.py --check
> D:\ruanjian\anaconda\envs\system\python.exe main.py
> ```
> 注意：该环境里同时装有 RAG 项目的重型依赖（`torch 2.10.0+cu128`、`transformers 4.45.0`、
> `sentence-transformers 3.0.1`、`FlagEmbedding 1.3.5`、`pymilvus 2.5.4`）。FoodAgent **不 import** 它们，
> `MILVUS_ENABLED` 默认关闭，检索走 `tools/reranker.py` 的纯 Python 精排；不要误以为本项目依赖 torch。

---

## 二、三种运行方式

### 方式一：Web 服务（推荐）

```bash
python main.py                    # 默认 http://127.0.0.1:8090
python main.py --port 9000        # 自定义端口
python main.py --reload           # 开发热重载
python main.py --no-banner        # 跳过自检直接启动
```

### 方式二：终端交互（调试用）

```bash
python run_cli.py
python run_cli.py --tenant store_001 --user waiter_01 --role waiter
```

交互命令：`/quit` 退出、`/new` 新建任务、`/info` 查看会话信息。

### 方式三：Docker 一键编排

```bash
# 启动 FoodAgent + PostgreSQL
docker compose -f docs/deploy/docker-compose.yml up -d

# 查看日志 / 停止
docker compose -f docs/deploy/docker-compose.yml logs -f foodagent
docker compose -f docs/deploy/docker-compose.yml down
```

> 知识层为内置知识库，无需额外容器；Redis 可选（未起时降级进程内缓存）。

---

## 三、PostgreSQL 集成（启用完整三层记忆）

### 3.1 配置

编辑 `.env`：

```ini
PG_ENABLED=true
PG_HOST=localhost
PG_PORT=5432
PG_USER=foodagent
PG_PASSWORD=your_password
PG_DATABASE=foodagent
```

> 注意端口冲突：本机若已装 PostgreSQL，Docker 编排里映射的是宿主 **5433**，容器内经 `db:5432` 内网访问。

### 3.2 建表

**方式 A —— 程序自动建表（推荐）**：启动服务时 `PostgresStore.init()` 会自动执行幂等 DDL。

**方式 B —— 手工执行**：

```bash
psql -U foodagent -d foodagent -f docs/deploy/schema.sql
```

建表后确认：

```sql
\dt
-- store_preferences / customer_profiles / review_analysis / ingredient_usage / compliance_log
```

### 3.3 验证隔离生效

```bash
curl -H "Authorization: Bearer <token>" http://127.0.0.1:8090/api/memory/preferences
# store_001 与 store_002 返回的数据应互不可见
```

---

## 四、知识层与可选组件

### 4.1 知识层（内置，无需部署）

食安法规与营养知识内置在项目内，启动即可用：

| 文件 | 内容 |
|------|------|
| `tools/safety_knowledge.py` | 食品安全法规要点（GB 31654 / GB 7718 / GB 31621 / 食品安全法 / 留样规范）+ 过敏原管理规范 |
| `tools/menu_data.py` | 菜品库（配料/过敏原/价格）+ 营养成分表 |

检索链路：分类 → 查内置知识 → 命中则生成答案；未命中走联网兜底（失败用 24h 缓存）；
仍无结果则转人工（附 `service_phone`），**不硬编答案**。

```bash
# 验证知识问答（走 SafetyAgent 9 节点流程）
curl -s -X POST http://127.0.0.1:8090/api/chat \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <TOKEN>" \
  -d "{\"query\":\"凉菜间消毒有什么标准\"}"
```

### 4.2 换成外部 RAG / 向量库

只改 `tools/mcp_tools.py` 中 4 个函数的数据源，Agent 节点与接口层不用动：
`search_menu_knowledge` / `query_food_safety_law` / `query_nutrition` / `web_search`。
Milvus 连接参数已在 `config.ini` 的 `[milvus]` 段预留。

### 4.3 可选组件（默认关闭，开启即生效）

| 组件 | 开关（`.env` / `config.ini`） | 缺失/关闭时 |
|------|------------------------------|-------------|
| Redis 缓存 | `REDIS_ENABLED=true` | 降级进程内字典（单机可用，重启失效） |
| LangSmith 追踪 | `LANGSMITH_ENABLED=true` + `LANGSMITH_API_KEY` | 追踪装饰器退化为恒等，零开销 |
| Prometheus 指标 | `PROMETHEUS_ENABLED=true` | 记录函数为 no-op，`/metrics` 返回说明文本 |
| Locust 压测 | `LOADTEST_ENABLED=true` | 不影响主流程 |

### 4.4 记忆清理（合规归档）

```bash
python scripts/purge_expired.py --dry-run    # 预演
python scripts/purge_expired.py              # 实际清理
```

保留天数在 `config.ini [memory]` 配置（对话 90 天 / 评价 730 天 / 订单日志 90 天 /
门店偏好与顾客画像 0=永久）。生产环境请挂 cron 或 Windows 计划任务。

---

## 五、配置项说明

配置读取优先级：**环境变量(.env) > config.ini > 代码内 fallback**

| 配置项 | 默认 | 说明 |
|--------|------|------|
| `APP_PORT` | 8090 | 服务端口 |
| `LLM_API_KEY` | 空 | DeepSeek API Key（必填，`sk-` 开头） |
| `LLM_MODEL` | deepseek-chat | 主模型名 |
| `LLM_BASE_URL` | https://api.deepseek.com/v1 | OpenAI 兼容地址 |
| `LLM_ENABLED` | true | 设 false 可完全离线运行 |
| `LLM_TIMEOUT` | 30 | 单次调用超时（秒） |
| `LLM_FALLBACK_ENABLED` | true | 是否启用备用模型切换 |
| `LLM_FALLBACK_MODEL` | 空 | 备用模型名（**空=未启用**，配了才生效） |
| `PG_ENABLED` | false | 是否启用 PostgreSQL |
| `JWT_SECRET` | dev 默认值 | **生产必须覆盖** |
| `JWT_EXPIRE_MINUTES` | 120 | Token 有效期 |
| `KITCHEN_INTERRUPT_THRESHOLD` | 10 | 排队超此值触发人工确认 |
| `COMPLIANCE_LOG_YEARS` | 3 | 过敏原留痕保留年限 |

---

## 六、故障排查

| 现象 | 原因 | 处理 |
|------|------|------|
| `ModuleNotFoundError: No module named 'base'` | 未在项目根目录运行 | `cd D:\heima\FoodAgent` 后重试，或设置 `PYTHONPATH=.` |
| `ModuleNotFoundError: No module named 'httpx'` | 依赖未安装 | `pip install -r requirements.txt` |
| 启动慢约 48 秒 | Redis 配置启用但未启动（连接重试） | `docker start redis`，或设 `REDIS_ENABLED=false` |
| `/health` 中 `llm.available=false` | API Key 缺失或格式非法 | 检查 `.env` 的 `LLM_API_KEY` 是否以 `sk-` 开头 |
| `checkpointer: MemorySaver` | PG 未启用 | 正常降级；需持久化则设 `PG_ENABLED=true` |
| 端口被占用 `Address already in use` | 8090 被占 | `python main.py --port 9000` |
| 前端无响应 / SSE 中断 | 反向代理缓冲了流 | 确认响应头含 `X-Accel-Buffering: no`（已内置） |
| 工具数不是 15 | 工具注册表异常 | 查 `/api/tools`，核对 `tools/registry.py` |

**日志位置**：控制台 + `logs/app.log`。

**排查建议**：先用 `python run_cli.py` 在终端复现，日志更清晰；再用 `/api/health` 确认各依赖状态。

---

## 七、生产部署清单

- [ ] `.env` 中设置强随机 `JWT_SECRET`（`openssl rand -hex 32`）
- [ ] `LLM_API_KEY` 通过环境变量注入，不落盘到镜像
- [ ] `PG_ENABLED=true` 并配置独立数据库账号
- [ ] CORS `allow_origins` 从 `["*"]` 收敛为门店域名白名单
- [ ] 前置 Nginx/网关：HTTPS + SSE 需关闭缓冲（`proxy_buffering off`）
- [ ] 日志接入集中采集（ELK / Loki）
- [ ] 合规留痕表配置定期备份（保留 3 年）
- [ ] 演示账号体系替换为甲方门店管理系统账号服务
- [ ] 为 `KITCHEN_INTERRUPT_THRESHOLD` 按门店实际产能调优

---

## 八、密钥与运行策略（2026-10-30 更新）

### 1. 密钥已更换（名称 = tool）

对话模型仍接 DeepSeek，密钥换成新的一把，名称标记为 `tool`：

| 位置 | 键名 | 说明 |
|---|---|---|
| `.env` | `LLM_API_KEY` | 运行时生效 |
| `.env` | `LLM_API_KEY_NAME` | 名称标识（不参与鉴权） |
| `config.ini` | `[llm] api_key_name` | 名称留痕；`api_key` 保持留空，密钥只走 `.env` |
| `.env.example` | `LLM_API_KEY` | 模板占位，勿填真钥 |

### 2. 运行策略：只做最小验证，不跑完整一轮 Agent 流程

对话模型是 **DeepSeek 远程 API（非本地部署、按 token 计费）**。
本系统一轮完整端到端会串 33 个节点，LLM 工具被多次触发，全量跑一次成本不可控。
因此新增硬限制模块 **`base/run_policy.py`**：

| 限制 | 默认值 | 环境变量 |
|---|---|---|
| 单次运行样本/场景上限 | 2 条 | `SMOKE_MAX_SAMPLES` |
| 单次运行远程调用预算 | 6 次 | `SMOKE_MAX_LLM_CALLS` |
| 全量运行 | 禁止 | `ALLOW_FULL_RUN=0` |

落地位置：

- `tools/llm_client.py` 的 `chat()` / `chat_stream()`：调用前扣预算；超限转成
  `LLMUnavailableError`，由上层**降级到规则引擎**——既守住成本，也不破坏既有的三级降级链路
- `scripts/selfcheck.py`：端到端只跑 1 类场景（原 5 类），Pipeline 专项默认跳过
- `main.py` / `selfcheck.py`：启动即打印策略横幅；传 `--allow-full` 但环境变量没开会被拒绝启动

需要全量时（两道闸门缺一不可，跑完改回）：

```bash
set ALLOW_FULL_RUN=1
python scripts/selfcheck.py --allow-full
set ALLOW_FULL_RUN=0
```
