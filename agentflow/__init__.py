"""
    平台层：platform/

    把「编排运行时」与「垂直领域能力」拆开：

        agentflow/          —— 与业务无关的装配能力（契约、加载、校验、自述）
        verticals/<id>/    —— 垂直领域能力包（数据驱动，一个场景一个包）
        agents/ tools/     —— 当前能力包的参考实现

    一句话：换业务场景 = 换一个 pack + 提供对应实现，而不是复制整个仓库。

    对外只暴露三样东西：
        get_runtime()          取平台运行时（含当前激活包）
        VerticalPack           能力包规格（契约）
        PackValidationError    装配期契约违规
"""
from agentflow.contracts import (
    AgentSpec,
    GuardSpec,
    IntentSpec,
    PackValidationError,
    ToolSpec,
    VerticalPack,
)
from agentflow.runtime import PLATFORM_NAME, PlatformRuntime, get_runtime

__all__ = [
    "AgentSpec",
    "GuardSpec",
    "IntentSpec",
    "PackValidationError",
    "ToolSpec",
    "VerticalPack",
    "PlatformRuntime",
    "PLATFORM_NAME",
    "get_runtime",
]
