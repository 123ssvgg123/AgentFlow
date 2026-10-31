"""
    LangSmith 全链路追踪接入：tools/tracing.py

    职责：为 Agent 节点、LLM 调用、工具调用提供统一的可观测性接入点。

    设计原则（与 config.ini 的「重型组件默认关闭」一致）：
        · LangSmith 未启用 / 未装依赖 → 全部函数为 no-op，零副作用
        · 启用后自动注入环境变量，LangGraph 与 LangChain 自动上报
        · 不引入硬依赖：所有 import 都在函数内延迟执行

    启用步骤（见 docs/OBSERVABILITY.md）：
        1) pip install langsmith langchain-core
        2) config.ini 的 [langsmith] enabled = true，并填 api_key
        3) 重启服务，日志会打印「LangSmith 追踪已启用」
"""
from datetime import datetime
from typing import Any, Dict, Optional

from base.config import config
from base.logger import logger

#: 是否已成功初始化（幂等）
_INITIALIZED = False
#: 运行时可用性（依赖是否装上）
_SDK_AVAILABLE = False


def init_tracing() -> Dict[str, Any]:
    """
    初始化追踪（幂等，可在 app 启动时安全调用多次）。

    返回状态字典：
        {"enabled": bool, "sdk_available": bool, "project": str, "reason": str}
    """
    global _INITIALIZED, _SDK_AVAILABLE

    if not config.LANGSMITH_ENABLED:
        logger.info("[追踪] LangSmith 未启用（config.ini [langsmith] enabled=false），跳过初始化")
        return {"enabled": False, "sdk_available": False, "project": "",
                "reason": "disabled_by_config"}

    if not config.LANGSMITH_API_KEY:
        logger.warning("[追踪] LangSmith 已启用但未配置 api_key，追踪不会生效")
        return {"enabled": False, "sdk_available": False, "project": "",
                "reason": "missing_api_key"}

    try:
        import langsmith  # noqa: F401
        from langsmith import Client  # noqa: F401
        _SDK_AVAILABLE = True
    except ImportError as exc:
        logger.warning(f"[追踪] LangSmith 依赖未安装（{exc}），"
                       f"请执行：pip install langsmith langchain-core")
        return {"enabled": False, "sdk_available": False, "project": "",
                "reason": "missing_dependency"}

    if not _INITIALIZED:
        # 注入标准环境变量（LangChain/LangGraph 会自动读取）
        import os
        os.environ["LANGCHAIN_TRACING_V2"] = "true"
        os.environ["LANGCHAIN_API_KEY"] = config.LANGSMITH_API_KEY
        os.environ["LANGCHAIN_PROJECT"] = config.LANGSMITH_PROJECT
        os.environ["LANGCHAIN_ENDPOINT"] = config.LANGSMITH_ENDPOINT
        os.environ["LANGCHAIN_SAMPLE_RATE"] = str(config.LANGSMITH_SAMPLE_RATE)
        _INITIALIZED = True
        logger.info(f"[追踪] LangSmith 追踪已启用：project={config.LANGSMITH_PROJECT} "
                    f"endpoint={config.LANGSMITH_ENDPOINT} "
                    f"sample_rate={config.LANGSMITH_SAMPLE_RATE}")

    return {"enabled": True, "sdk_available": _SDK_AVAILABLE,
            "project": config.LANGSMITH_PROJECT, "reason": "ok"}


def trace_config() -> Optional[Dict[str, Any]]:
    """
    返回可传给 LangGraph `.ainvoke(..., config=...)` 的追踪配置。

    未启用时返回 None（不影响调用方）；启用时返回含 callbacks/metadata 的 dict。
    """
    if not config.LANGSMITH_ENABLED or not _SDK_AVAILABLE:
        return None
    return {
        "metadata": {
            "project": config.LANGSMITH_PROJECT,
            "service": "FoodAgent",
            "ts": datetime.now().isoformat(),
        },
        "tags": ["foodagent", "langgraph"],
    }


def traceable(fn_name: str = "", run_type: str = "chain"):
    """
    装饰器：为任意函数加上 LangSmith 追踪。

    未启用时**原样返回函数**（零开销），启用时包装为 langsmith.traceable。
    用法：
        @traceable("allergen_detection", "tool")
        def allergen_detection(...): ...
    """
    def _decorator(func):
        if not config.LANGSMITH_ENABLED:
            return func
        try:
            from langsmith import traceable as _ls_traceable
            return _ls_traceable(run_type=run_type, name=fn_name or func.__name__)(func)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[追踪] traceable 包装失败（{fn_name}）: {exc}")
            return func
    return _decorator


def status() -> Dict[str, Any]:
    """返回追踪当前状态（供 /health 或 /api/observability 展示）"""
    return {
        "enabled": config.LANGSMITH_ENABLED,
        "initialized": _INITIALIZED,
        "sdk_available": _SDK_AVAILABLE,
        "project": config.LANGSMITH_PROJECT if config.LANGSMITH_ENABLED else "",
        "sample_rate": config.LANGSMITH_SAMPLE_RATE,
    }


if __name__ == '__main__':
    print("=" * 60)
    print("LangSmith 追踪模块自检")
    print("=" * 60)
    print("配置开关：", config.LANGSMITH_ENABLED)
    r = init_tracing()
    print("初始化结果：", r)
    print("追踪配置：", trace_config())
    print("状态：", status())

    # 未启用时 traceable 应为恒等装饰器
    @traceable("demo", "tool")
    def demo(x):
        return x * 2

    assert demo(21) == 42, "traceable 装饰后函数行为不应改变"
    print("\n恒等装饰器校验通过（未启用时不改变函数行为）")
    print("\n自检通过。")
