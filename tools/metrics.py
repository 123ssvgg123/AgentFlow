"""
    Prometheus 指标：tools/metrics.py

    职责：暴露业务与性能指标，供 Prometheus 抓取、Grafana 展示。

    设计原则（与「重型组件默认关闭」一致）：
        · 未启用 / 未装 prometheus-client → 全部 API 为 no-op，零开销
        · 提供上下文管理器 `observe()` 统一统计耗时与错误
        · 指标命名遵循 Prometheus 规范（_total 后缀、_seconds 单位）

    暴露指标：
        foodagent_requests_total{endpoint,status}          请求总数
        foodagent_request_duration_seconds{endpoint}       请求耗时直方图
        foodagent_agent_duration_seconds{agent,stage}      Agent 阶段耗时
        foodagent_llm_calls_total{model,result}            LLM 调用次数
                                                          （result: success / success_fallback /
                                                            degraded / error）
        foodagent_tool_degraded_total{tool}                工具降级次数
        foodagent_cache_operations_total{op,result}        缓存命中/未命中
        foodagent_order_status_transitions_total{to_status} 订单状态跃迁次数
        foodagent_active_requests                          当前并发请求数

    启用步骤（见 docs/OBSERVABILITY.md）：
        1) pip install prometheus-client
        2) config.ini 的 [prometheus] enabled = true
        3) 服务暴露 GET /metrics
"""
import time
from contextlib import contextmanager
from typing import Any, Dict, Optional

from base.config import config
from base.logger import logger

#: 指标容器（未启用时为 None）
_METRICS: Dict[str, Any] = {}
_ENABLED = False


def init_metrics() -> Dict[str, Any]:
    """
    初始化指标注册表（幂等）。

    返回 {"enabled": bool, "reason": str}
    """
    global _ENABLED, _METRICS
    if _ENABLED:
        return {"enabled": True, "reason": "already_initialized"}

    if not config.PROMETHEUS_ENABLED:
        logger.info("[指标] Prometheus 未启用（[prometheus] enabled=false），跳过初始化")
        return {"enabled": False, "reason": "disabled_by_config"}

    try:
        from prometheus_client import Counter, Gauge, Histogram
    except ImportError:
        logger.warning("[指标] prometheus-client 未安装，指标不会生效；"
                       "请执行：pip install prometheus-client")
        return {"enabled": False, "reason": "missing_dependency"}

    ns = config.PROMETHEUS_NAMESPACE
    try:
        _METRICS["requests_total"] = Counter(
            f"{ns}_requests_total", "Total HTTP requests", ["endpoint", "status"])
        _METRICS["request_duration"] = Histogram(
            f"{ns}_request_duration_seconds", "HTTP request duration", ["endpoint"],
            buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0))
        _METRICS["agent_duration"] = Histogram(
            f"{ns}_agent_duration_seconds", "Agent stage duration", ["agent", "stage"],
            buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0))
        _METRICS["llm_calls"] = Counter(
            f"{ns}_llm_calls_total", "LLM calls", ["model", "result"])
        _METRICS["tool_degraded"] = Counter(
            f"{ns}_tool_degraded_total", "Tool degradation count", ["tool"])
        _METRICS["cache_ops"] = Counter(
            f"{ns}_cache_operations_total", "Cache operations", ["op", "result"])
        _METRICS["status_transitions"] = Counter(
            f"{ns}_order_status_transitions_total", "Order status transitions", ["to_status"])
        _METRICS["active_requests"] = Gauge(
            f"{ns}_active_requests", "Currently active requests")
        _ENABLED = True
        logger.info(f"[指标] Prometheus 指标已启用：namespace={ns} "
                    f"path={config.PROMETHEUS_METRICS_PATH}")
        return {"enabled": True, "reason": "ok"}
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[指标] 指标注册失败: {exc}")
        return {"enabled": False, "reason": str(exc)}


# ==================== 记录接口（未启用时全部 no-op） ====================

def record_request(endpoint: str, status: str, duration: float) -> None:
    """记录一次 HTTP 请求"""
    if not _ENABLED:
        return
    try:
        _METRICS["requests_total"].labels(endpoint=endpoint, status=status).inc()
        _METRICS["request_duration"].labels(endpoint=endpoint).observe(duration)
    except Exception:  # noqa: BLE001
        pass


def record_agent(agent: str, stage: str, duration: float) -> None:
    """记录一次 Agent 阶段耗时（node_level_metrics=false 时聚合成 stage='total'）"""
    if not _ENABLED:
        return
    try:
        _METRICS["agent_duration"].labels(
            agent=agent, stage=stage if config.PROMETHEUS_NODE_LEVEL else "total"
        ).observe(duration)
    except Exception:  # noqa: BLE001
        pass


def record_llm(model: str, result: str = "success") -> None:
    """
    记录一次 LLM 调用。

    result 取值：
        success           主模型调用成功
        success_fallback  主模型失败后**切换备用模型**调用成功（需求「主模型不可用切备用模型」）
        degraded          降级到规则引擎
        error             主模型与备用模型均失败
    """
    if not _ENABLED:
        return
    try:
        _METRICS["llm_calls"].labels(model=model or "unknown", result=result).inc()
    except Exception:  # noqa: BLE001
        pass


def record_degraded(tool: str) -> None:
    """记录一次工具降级"""
    if not _ENABLED:
        return
    try:
        _METRICS["tool_degraded"].labels(tool=tool).inc()
    except Exception:  # noqa: BLE001
        pass


def record_cache(op: str, hit: bool) -> None:
    """记录一次缓存操作（op: get / set）"""
    if not _ENABLED:
        return
    try:
        _METRICS["cache_ops"].labels(op=op, result="hit" if hit else "miss").inc()
    except Exception:  # noqa: BLE001
        pass


def record_status_transition(to_status: str) -> None:
    """记录一次订单状态跃迁"""
    if not _ENABLED:
        return
    try:
        _METRICS["status_transitions"].labels(to_status=to_status).inc()
    except Exception:  # noqa: BLE001
        pass


def inc_active(delta: int = 1) -> None:
    """增减当前并发请求数"""
    if not _ENABLED:
        return
    try:
        _METRICS["active_requests"].inc(delta)
    except Exception:  # noqa: BLE001
        pass


@contextmanager
def observe(endpoint: str, status: str = "200"):
    """上下文管理器：统计一段代码的耗时与并发"""
    start = time.time()
    inc_active(1)
    try:
        yield
    finally:
        inc_active(-1)
        record_request(endpoint, status, time.time() - start)


def is_enabled() -> bool:
    return _ENABLED


def status() -> Dict[str, Any]:
    """指标状态（供 /health 展示）"""
    return {
        "enabled": _ENABLED,
        "metrics_path": config.PROMETHEUS_METRICS_PATH,
        "namespace": config.PROMETHEUS_NAMESPACE,
        "node_level": config.PROMETHEUS_NODE_LEVEL,
        "registered": sorted(_METRICS.keys()),
    }


def render() -> tuple:
    """
    渲染 Prometheus 文本格式，供 `GET /metrics` 端点直接返回。

    :return: (body: bytes, content_type: str)
             未启用时返回 (b"", "text/plain")，端点据此返回降级说明。
    """
    if not _ENABLED:
        return b"", "text/plain; charset=utf-8"
    try:
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
        return generate_latest(), CONTENT_TYPE_LATEST
    except ImportError:
        return b"", "text/plain; charset=utf-8"


if __name__ == '__main__':
    print("=" * 60)
    print("Prometheus 指标模块自检")
    print("=" * 60)
    print("配置开关：", config.PROMETHEUS_ENABLED)
    print("初始化：", init_metrics())
    print("状态：", status())

    # 未启用时全部 no-op，不应抛异常
    record_request("/api/chat", "200", 0.35)
    record_agent("OrderAgent", "parse_order", 0.12)
    record_llm("deepseek-chat", "success")
    record_degraded("query_nutrition")
    record_cache("get", True)
    record_status_transition("submitted")
    with observe("/api/chat"):
        time.sleep(0.01)
    print("\nno-op 调用校验通过（未启用时不抛异常、无副作用）")
    print("\n自检通过。")
