# catering · 连锁餐饮能力包（参考实现）

这是 AgentFlow 的 `status = reference` 能力包：**平台能力的验证场景**。

## 为什么选它当 reference

不是因为它"餐饮"，而是因为它同时命中平台必须处理的三个失效面：

| 失效面 | 具体表现 | 平台的应对 |
|---|---|---|
| **结果不可错的判定** | 过敏原误判 = 食安事故；算错金额 = 赔付 | 4 个工具标记 `must_be_exact`，规则引擎接管，模型不参与 |
| **有副作用的写操作** | 下单 / 改派 / 退款 | `write_ops_hitl` 护栏：interrupt 挂起，人工确认后才落库 |
| **跨 Agent 的诉求切换** | 一句话里既要点餐又要问法规 | Orchestrator 自动切 Pipeline，前序产出注入后序 State |

换成电商、售后、工单，这三个失效面同样存在——所以这套编排是可迁移的，
餐饮只是它的第一个落地形态。

## 包内规模

```
agents  : 4（order 10 / kitchen 7 / review 7 / safety 9）= 33 节点
tools   : 15（llm 4 / rule 4 / mcp 4 / external 3）
intents : 9
guards  : 7（rule_engine ×4 / human_in_the_loop ×1 / schema_validation ×1 / quota ×1）
```

## 契约要点

- 每个 `tool.owners` 必须指向真实存在的 agent，否则启动期报错（防"展示的归属是假的"）
- 每个 agent 至少被一个 intent 指向，否则是永远进不去的死 Agent
- `AgentSpec.intents` 与 `IntentSpec` 双向一致，防止两处各改一半
- 有 `has_side_effect = true` 的工具，包里必须存在 `human_in_the_loop` 护栏
- `must_be_exact` 工具必须被 `bypass_llm` 的 `rule_engine` 护栏覆盖

## 验证

```bash
python scripts/verify_platform.py
```

会同时校验：契约自洽、`pack.json` 与 `agents/*/graph.py` 的节点数是否一致、
与 `tools/*.py` 的工具清单是否一致，并跑 4 条反向自测确认校验器真的会拦。
**全程零模型调用。**
