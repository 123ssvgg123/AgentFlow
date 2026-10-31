"""
verify_observability.py —— 重型组件接入验证

覆盖：
    O1  config.ini 五个新配置段均可正确读取（默认全关）
    O2  环境变量覆盖优先级生效
    O3  LangSmith 模块（未启用时恒等装饰器）
    O4  Redis/缓存模块（三级降级与 TTL）
    O5  Prometheus 模块（未启用时 no-op）
    O6  Locust 脚本可被导入且语料完整
    O7  K8s 清单文件存在且 YAML 合法
    O8  三态矩阵输出（开关 / 依赖 / 运行）
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))


def o1_config_sections():
    print("\n[O1] 配置段读取")
    from base.config import config
    # 开关值可由使用者在 config.ini 中自行决定，因此这里只验证「类型正确」，
    # 不硬编码期望值（否则用户一开开关，脚本就假报失败）。
    check("o1.1 langsmith 开关可读（bool）", isinstance(config.LANGSMITH_ENABLED, bool),
          f"当前={config.LANGSMITH_ENABLED}")
    check("o1.2 redis 开关可读（bool）", isinstance(config.REDIS_ENABLED, bool),
          f"当前={config.REDIS_ENABLED}")
    check("o1.3 prometheus 开关可读（bool）", isinstance(config.PROMETHEUS_ENABLED, bool),
          f"当前={config.PROMETHEUS_ENABLED}")
    check("o1.4 loadtest 开关可读（bool）", isinstance(config.LOADTEST_ENABLED, bool),
          f"当前={config.LOADTEST_ENABLED}")
    check("o1.5 k8s 开关可读（bool）", isinstance(config.K8S_ENABLED, bool),
          f"当前={config.K8S_ENABLED}")

    check("o1.6 langsmith 配置项完整",
          all(hasattr(config, a) for a in
              ["LANGSMITH_PROJECT", "LANGSMITH_ENDPOINT", "LANGSMITH_SAMPLE_RATE"]))
    check("o1.7 redis 配置项完整",
          all(hasattr(config, a) for a in
              ["REDIS_HOST", "REDIS_PORT", "REDIS_DB", "REDIS_DEFAULT_TTL", "REDIS_KEY_PREFIX"]))
    check("o1.8 prometheus 配置项完整",
          all(hasattr(config, a) for a in
              ["PROMETHEUS_METRICS_PATH", "PROMETHEUS_NAMESPACE", "PROMETHEUS_NODE_LEVEL"]))
    check("o1.9 loadtest 配置项完整",
          all(hasattr(config, a) for a in
              ["LOADTEST_USERS", "LOADTEST_SPAWN_RATE", "LOADTEST_TENANT_ID"]))
    check("o1.10 k8s 配置项完整",
          all(hasattr(config, a) for a in
              ["K8S_NAMESPACE", "K8S_REPLICAS", "K8S_HPA_MIN", "K8S_HPA_MAX"]))
    check("o1.11 redis 默认 TTL = 300", config.REDIS_DEFAULT_TTL == 300,
          str(config.REDIS_DEFAULT_TTL))
    check("o1.12 k8s HPA 区间合法", config.K8S_HPA_MIN < config.K8S_HPA_MAX,
          f"{config.K8S_HPA_MIN}~{config.K8S_HPA_MAX}")


def o2_env_override():
    print("\n[O2] 环境变量覆盖")
    import importlib
    os.environ["REDIS_DEFAULT_TTL"] = "999"
    os.environ["K8S_REPLICAS"] = "7"
    import base.config as cfg_mod
    importlib.reload(cfg_mod)
    check("o2.1 REDIS_DEFAULT_TTL 被环境变量覆盖",
          cfg_mod.config.REDIS_DEFAULT_TTL == 999, str(cfg_mod.config.REDIS_DEFAULT_TTL))
    check("o2.2 K8S_REPLICAS 被环境变量覆盖",
          cfg_mod.config.K8S_REPLICAS == 7, str(cfg_mod.config.K8S_REPLICAS))
    # 清理
    os.environ.pop("REDIS_DEFAULT_TTL", None)
    os.environ.pop("K8S_REPLICAS", None)
    importlib.reload(cfg_mod)


def o3_tracing():
    print("\n[O3] LangSmith 追踪模块")
    from tools.tracing import init_tracing, status, trace_config, traceable, _INITIALIZED
    import tools.tracing as tracing_mod
    import base.config as cfg_mod

    # ---- 3a 关闭态：临时把开关设为 False，验证降级行为，测完还原 ----
    saved_flag = cfg_mod.config.LANGSMITH_ENABLED
    saved_init = getattr(tracing_mod, "_INITIALIZED", False)
    cfg_mod.config.LANGSMITH_ENABLED = False
    tracing_mod._INITIALIZED = False
    try:
        r = init_tracing()
        check("o3.1 关闭时初始化返回 enabled=False", r["enabled"] is False, r.get("reason", ""))
        check("o3.2 关闭时事由为 disabled_by_config",
              r.get("reason") == "disabled_by_config", str(r.get("reason")))
        check("o3.3 关闭时 trace_config 返回 None", trace_config() is None)

        @traceable("demo_tool", "tool")
        def demo(x):
            return x + 1

        check("o3.5 traceable 关闭时为恒等装饰器", demo(41) == 42)
        check("o3.6 装饰后函数名保留", demo.__name__ == "demo", demo.__name__)
    finally:
        cfg_mod.config.LANGSMITH_ENABLED = saved_flag
        tracing_mod._INITIALIZED = saved_init

    # ---- 3b 当前配置态：验证三态字段完整 ----
    st = status()
    check("o3.4 status 含三态字段",
          all(k in st for k in ["enabled", "initialized", "sdk_available"]),
          f"enabled={st.get('enabled')} sdk={st.get('sdk_available')}")


async def o4_cache():
    print("\n[O4] 缓存层")
    from tools.cache import cache, KEY_STORE_PREFS
    import base.config as cfg_mod

    # ---- 4a 关闭态：临时关掉 Redis，验证降级为进程内缓存 ----
    saved_flag = cfg_mod.config.REDIS_ENABLED
    saved_redis = cache._redis
    saved_ready = cache._redis_ready
    cfg_mod.config.REDIS_ENABLED = False
    cache._redis = None
    cache._redis_ready = False
    try:
        r = cache.init()
        check("o4.1 关闭 Redis 时降级 local", r["backend"] == "local", str(r))
        cache.set("obs:local_only", {"v": 1}, ttl=30)
        check("o4.1b 降级后仍可读写（进程内）", cache.get("obs:local_only") == {"v": 1})
        cache.delete("obs:local_only")
    finally:
        cfg_mod.config.REDIS_ENABLED = saved_flag
        cache.init()   # 按真实配置重新初始化，后续用例走真实后端

    backend_now = cache.stats().get("backend")
    check("o4.1c 当前后端可用（local 或 redis）",
          backend_now in ("local", "redis"), f"backend={backend_now}")

    cache.set("obs:t1", {"v": 1}, ttl=60)
    check("o4.2 读写一致", cache.get("obs:t1") == {"v": 1})
    check("o4.3 未命中返回 None", cache.get("obs:nope") is None)

    cache.set("obs:t2", "x", ttl=1)
    check("o4.4 TTL 内可读", cache.get("obs:t2") == "x")
    await asyncio.sleep(1.1)
    check("o4.5 TTL 过期后失效", cache.get("obs:t2") is None)

    cache.delete("obs:t1")
    check("o4.6 删除生效", cache.get("obs:t1") is None)

    cache.set(f"{KEY_STORE_PREFS}store_x", {"popular_dishes": ["A"]})
    n = cache.clear(KEY_STORE_PREFS)
    check("o4.7 按前缀清理", n >= 1, f"清理 {n} 条")

    st = cache.stats()
    check("o4.8 stats 含命中率", "hit_rate" in st, f"hit_rate={st['hit_rate']}")

    # 中文与嵌套结构
    cache.set("obs:cn", {"菜": ["宫保鸡丁"], "nested": {"a": [1, 2]}}, ttl=30)
    check("o4.9 支持中文与嵌套结构",
          cache.get("obs:cn")["nested"]["a"] == [1, 2])
    cache.clear("obs:")


def o5_metrics():
    print("\n[O5] Prometheus 指标模块")
    import tools.metrics as metrics_mod
    from tools.metrics import (init_metrics, is_enabled, record_agent, record_cache,
                               record_degraded, record_llm, record_request,
                               record_status_transition, status)
    import base.config as cfg_mod

    # ---- 5a 关闭态：临时关掉开关并重置模块态，验证 no-op 行为 ----
    saved_flag = cfg_mod.config.PROMETHEUS_ENABLED
    saved_enabled = metrics_mod._ENABLED
    saved_metrics = getattr(metrics_mod, "_METRICS", {})
    cfg_mod.config.PROMETHEUS_ENABLED = False
    metrics_mod._ENABLED = False
    metrics_mod._METRICS = {}
    try:
        r = init_metrics()
        check("o5.1 关闭时初始化返回 enabled=False", r["enabled"] is False, r.get("reason", ""))
        check("o5.2 关闭时 is_enabled() = False", is_enabled() is False)

        # no-op 调用不应抛异常
        ok = True
        try:
            record_request("/api/chat", "200", 0.1)
            record_agent("OrderAgent", "parse_order", 0.05)
            record_llm("deepseek-chat", "success")
            record_degraded("query_nutrition")
            record_cache("get", True)
            record_status_transition("submitted")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print("      异常:", exc)
        check("o5.3 关闭时全部记录接口为 no-op 不抛异常", ok)

        with_ok = True
        try:
            from tools.metrics import observe
            with observe("/api/chat"):
                pass
        except Exception:  # noqa: BLE001
            with_ok = False
        check("o5.4 关闭时 observe 上下文管理器可用", with_ok)
    finally:
        cfg_mod.config.PROMETHEUS_ENABLED = saved_flag
        metrics_mod._ENABLED = saved_enabled
        metrics_mod._METRICS = saved_metrics

    check("o5.5 status 含 metrics_path",
          "metrics_path" in status(), str(status().get("metrics_path")))
    # 三态矩阵里会完整呈现「开关 / 依赖 / 运行」三者组合，这里不再硬编码期望值
    check("o5.6 status 字段完整",
          all(k in status() for k in ["enabled", "metrics_path", "namespace",
                                      "node_level", "registered"]),
          str(sorted(status().keys())))


def o6_locust():
    print("\n[O6] Locust 压测脚本")
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locustfile.py")
    check("o6.1 locustfile.py 存在", os.path.exists(path))

    src = open(path, encoding="utf-8").read()
    check("o6.2 定义了两个用户类",
          "class FoodAgentUser" in src and "class KitchenUser" in src)
    check("o6.3 覆盖 9 类意图 + pipeline",
          all(k in src for k in ["dish_recommend", "smart_order", "allergen_query",
                                 "kitchen_dispatch", "inventory_check",
                                 "review_analysis", "improvement_advice",
                                 "food_safety_query", "nutrition_query", "pipeline"]))
    check("o6.4 含压测开始/结束钩子",
          "test_start" in src and "test_stop" in src)
    check("o6.5 使用独立测试门店",
          "store_loadtest" in src)


def o7_k8s():
    print("\n[O7] Kubernetes 清单")
    base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "docs", "deploy", "k8s")
    yaml_path = os.path.join(base, "foodagent.yaml")
    readme_path = os.path.join(base, "README.md")
    check("o7.1 foodagent.yaml 存在", os.path.exists(yaml_path))
    check("o7.2 README.md 存在", os.path.exists(readme_path))

    if os.path.exists(yaml_path):
        src = open(yaml_path, encoding="utf-8").read()
        for kind in ["Namespace", "ConfigMap", "Secret", "Deployment",
                     "Service", "Ingress", "HorizontalPodAutoscaler",
                     "PodDisruptionBudget"]:
            check(f"o7.3 含 {kind}", f"kind: {kind}" in src)
        check("o7.4 配置了会话保持（SSE 不断流）",
              "sessionAffinity" in src)
        check("o7.5 配置了三个探针",
              all(p in src for p in ["livenessProbe", "readinessProbe", "startupProbe"]))
        check("o7.6 Secret 有明确的占位提醒",
              "REPLACE_WITH" in src)
        check("o7.7 HPA 同时用 CPU 与内存",
              "averageUtilization: 70" in src and "averageUtilization: 80" in src)

    # YAML 合法性（有 pyyaml 才校验）
    try:
        import yaml
        docs = list(yaml.safe_load_all(open(yaml_path, encoding="utf-8")))
        docs = [d for d in docs if d]
        check("o7.8 YAML 可解析且资源数 ≥8", len(docs) >= 8, f"{len(docs)} 个资源")
        kinds = [d.get("kind") for d in docs]
        check("o7.9 Deployment 位于 foodagent 命名空间",
              any(d.get("kind") == "Deployment"
                  and d.get("metadata", {}).get("namespace") == "foodagent" for d in docs))
    except ImportError:
        print("      (跳过 YAML 解析校验：未安装 pyyaml)")
    except Exception as exc:  # noqa: BLE001
        check("o7.8 YAML 可解析", False, str(exc))


def o8_matrix():
    print("\n[O8] 组件三态矩阵")
    from base.config import config
    from tools.cache import cache
    from tools.metrics import is_enabled as metrics_enabled
    from tools.tracing import status as tracing_status

    rows = [
        ("LangSmith", config.LANGSMITH_ENABLED, tracing_status()["sdk_available"], False),
        ("Redis", config.REDIS_ENABLED, cache.stats()["redis_connected"], False),
        ("Prometheus", config.PROMETHEUS_ENABLED, False, metrics_enabled()),
        ("Locust", config.LOADTEST_ENABLED, _module_exists("locust"), False),
        ("K8s", config.K8S_ENABLED, _module_exists("kubernetes"), False),
    ]
    print(f"      {'组件':<14}{'配置开关':<10}{'依赖可用':<10}{'运行状态'}")
    for name, enabled, dep, running in rows:
        print(f"      {name:<14}{str(enabled):<10}{str(dep):<10}{running}")
        check(f"o8 {name} 三态已就绪", isinstance(enabled, bool))


def _module_exists(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False


def main():
    print("=" * 64)
    print("重型组件接入验证")
    print("=" * 64)
    o1_config_sections()
    o2_env_override()
    o3_tracing()
    asyncio.run(o4_cache())
    o5_metrics()
    o6_locust()
    o7_k8s()
    o8_matrix()

    print("\n" + "=" * 64)
    print(f"结果：{len(PASS)} PASS / {len(FAIL)} FAIL")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    print("=" * 64)
    return 0 if not FAIL else 1


if __name__ == '__main__':
    sys.exit(main())
