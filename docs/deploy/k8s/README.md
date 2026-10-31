# Kubernetes 部署清单

> 状态：**可选组件，默认不启用**（`config.ini` 的 `[k8s] enabled = false`）
> 适用场景：需要多副本水平扩展、自动扩缩容的生产环境。
> 本地开发请直接用 `docs/deploy/docker-compose.yml`，不需要 K8s。

## 文件清单

| 文件 | 说明 |
| --- | --- |
| `namespace.yaml` | 命名空间 `foodagent` |
| `configmap.yaml` | 非敏感配置（从 `config.ini` 派生） |
| `secret.yaml` | 敏感配置（LLM API Key / PG 密码 / JWT secret），**示例值必须替换** |
| `deployment.yaml` | FoodAgent 主服务（默认 2 副本） |
| `service.yaml` | ClusterIP 服务 + 会话保持 |
| `ingress.yaml` | 外部访问入口（含 SSE 长连接超时配置） |
| `hpa.yaml` | 水平自动扩缩容（CPU 70%，2~10 副本） |
| `pdb.yaml` | 中断预算（滚动更新时保证可用副本数） |

## 部署步骤

```bash
# 1. 构建镜像（复用项目根目录 Dockerfile）
docker build -t foodagent:1.0.0 .

# 2. 推送镜像到你的仓库（按需替换地址）
docker tag foodagent:1.0.0 registry.example.com/foodagent:1.0.0
docker push registry.example.com/foodagent:1.0.0

# 3. 修改 deployment.yaml 的 image 字段为上面的镜像地址

# 4. 修改 secret.yaml 的真实密钥（切勿提交明文到 Git）

# 5. 按顺序应用
kubectl apply -f docs/deploy/k8s/namespace.yaml
kubectl apply -f docs/deploy/k8s/configmap.yaml
kubectl apply -f docs/deploy/k8s/secret.yaml
kubectl apply -f docs/deploy/k8s/deployment.yaml
kubectl apply -f docs/deploy/k8s/service.yaml
kubectl apply -f docs/deploy/k8s/ingress.yaml
kubectl apply -f docs/deploy/k8s/hpa.yaml
kubectl apply -f docs/deploy/k8s/pdb.yaml

# 6. 验证
kubectl -n foodagent get pods -w
kubectl -n foodagent logs -f deploy/foodagent-api
```

## 关键设计说明

### 1. SSE 流式接口与 Session Affinity

`/api/chat/stream` 是 SSE 长连接。多副本下若请求被轮询到不同 Pod，
前端会看到「打字机效果中断」。因此 `service.yaml` 设置了：

```yaml
sessionAffinity: ClientIP
sessionAffinityConfig:
  clientIP:
    timeoutSeconds: 10800   # 3 小时
```

**注意**：若前端经过 CDN/负载均衡，ClientIP 可能被改写，此时应改用
Nginx Ingress 的 `nginx.ingress.kubernetes.io/affinity: cookie`。

### 2. 有状态依赖（Postgres / Redis / Milvus）

本清单**只部署无状态 API 副本**，PG/Redis/Milvus 假定为外部服务：
- 生产推荐使用云托管 PG（RDS）/ Redis（云缓存），避免自建运维成本
- 若必须自建，建议单独用 StatefulSet + PVC，不要让 API Pod 承担数据持久化

### 3. 多副本下的共享状态

FoodAgent 的并发写保护（`memory/store.py` 的 `_WRITE_LOCKS`）是**进程内锁**。
多副本场景下必须满足其一，否则会出现「跨副本并发写覆盖」：

- **方案 A（推荐）**：数据库层解决 —— 用 PG 的 `INSERT ... ON CONFLICT DO UPDATE`
  做原子 upsert（本项目 `append_store_preferences` 的 PG 实现已按此思路设计）
- **方案 B**：启用 Redis 分布式锁替代进程内 `asyncio.Lock`
- **方案 C**：Checkpointer 与长期记忆全部走 Postgres（PG_ENABLED=true），
  进程内锁仅作为单副本内的额外优化

> 当前代码在 `PG_ENABLED=false` 且多副本时会退化为 FileStore，
> **不同 Pod 的 JSON 文件不共享**，仅适用于演示环境。生产必须开 PG。

### 4. 探针配置

```yaml
livenessProbe:    # 存活：进程卡死则重启
  httpGet: {path: /health, port: 8090}
  initialDelaySeconds: 30   # 等待模型/向量库初始化
  periodSeconds: 20
readinessProbe:   # 就绪：未就绪则摘除流量
  httpGet: {path: /health, port: 8090}
  initialDelaySeconds: 10
  periodSeconds: 10
```

`initialDelaySeconds: 30` 是为了给「MCP 探活 + 记忆初始化」留时间。

### 5. 资源建议

| 场景 | CPU request | CPU limit | Memory request | Memory limit |
| --- | --- | --- | --- | --- |
| 轻载（<10 QPS，无 LLM 本地推理） | 500m | 2000m | 1Gi | 2Gi |
| 重载（20+ QPS） | 1000m | 4000m | 2Gi | 4Gi |

**重要**：若使用本地 LLM / 本地 Embedding 模型（非 API 调用），
必须额外挂载 GPU 资源（`nvidia.com/gpu: 1`）并大幅提高内存 limit。

## 验证清单

- [ ] `kubectl -n foodagent get pods` 全部 Running 且 Ready
- [ ] `curl http://<ingress>/health` 返回 200 且 `pg_enabled` 符合预期
- [ ] 多副本下 `/api/chat` 的 `thread_id` 能跨请求复用（依赖 PG Checkpointer）
- [ ] HPA 在压测下成功扩容：`kubectl -n foodagent get hpa -w`
- [ ] 滚动更新期间无 5xx（`kubectl rollout restart deploy/foodagent-api`）
