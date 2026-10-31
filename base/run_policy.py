# -*- coding: utf-8 -*-
"""
运行策略模块：base/run_policy.py —— 远程模型「最小验证模式」

为什么要有这个模块
------------------
本项目的对话模型走 **DeepSeek 官方 API**（远程服务、按 token 计费），
**不是本地部署**。而本系统一次完整的端到端流程会串联多个 Agent
（点餐 10 节点 / 后厨 7 节点 / 评价 7 节点 / 食安 9 节点，共 33 个节点），
其中任何一个 LLM 工具被触发都会产生真实调用；跑一轮全量验证动辄几十次请求。

因此本项目立一条硬约定：

    **每次运行程序都不做完整一轮全量流程，只执行最小限度的验证。**

并且由代码强制，不依赖使用者自觉。

三条硬限制
----------
1. 样本上限：单次运行最多 ``SMOKE_MAX_SAMPLES`` 条场景/样本（默认 2）
2. 调用预算：单次运行最多 ``SMOKE_MAX_LLM_CALLS`` 次远程调用（默认 6，
   超出后 LLM 工具自动降级到规则引擎，不抛错给用户）
3. 全量闸门：要跑全量，必须**同时**满足
   「环境变量 ``ALLOW_FULL_RUN=1``」+「命令行 ``--allow-full``」，缺一即拒绝

用法
----
::

    from base.run_policy import run_policy, RunPolicyError

    run_policy.print_banner()          # 启动时打印当前策略
    n = run_policy.clamp_samples(5)    # 5 → 2，并提示
    run_policy.LLM_BUDGET.spend(where="llm_client.chat")   # 每次远程调用前扣一次
"""
from __future__ import annotations

import os
from typing import Optional

# 本模块可能早于 base.config 被导入（例如 main.py 先打印策略横幅），
# 因此自己再兜一次 .env：只补缺失的键，不覆盖已有环境变量
try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))
except ImportError:
    pass

__all__ = ["run_policy", "RunPolicyError", "LLMBudget"]


class RunPolicyError(RuntimeError):
    """违反运行策略（超样本上限 / 超调用预算 / 未授权全量）时抛出"""


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name, "") or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_flag(name: str) -> bool:
    return (os.getenv(name, "") or "").strip().lower() in ("1", "true", "yes", "on")


class LLMBudget:
    """单次运行的远程调用预算计数器（进程级单例，见文件末尾 LLM_BUDGET）"""

    def __init__(self, limit: Optional[int] = None) -> None:
        self.limit = limit if limit is not None else _env_int("SMOKE_MAX_LLM_CALLS", 6)
        self.used = 0
        self.tripped = False

    @property
    def remaining(self) -> int:
        return max(self.limit - self.used, 0)

    def reset(self, limit: Optional[int] = None) -> None:
        if limit is not None:
            self.limit = limit
        self.used = 0
        self.tripped = False

    def spend(self, n: int = 1, *, where: str = "") -> int:
        """调用远程模型前扣一次预算；超限抛 RunPolicyError（先扣后调，不会花超）"""
        if self.used + n > self.limit:
            self.tripped = True
            raise RunPolicyError(
                f"远程模型调用预算已用尽（上限 {self.limit} 次，已用 {self.used} 次）"
                f"{'，触发点：' + where if where else ''}。"
                "本模型为远程 API 按量计费，非本地部署，按运行策略禁止继续调用。"
                "如确需更多调用，请显式设置 ALLOW_FULL_RUN=1 并调整 SMOKE_MAX_LLM_CALLS。"
            )
        self.used += n
        return self.used

    def snapshot(self) -> dict:
        return {"limit": self.limit, "used": self.used,
                "remaining": self.remaining, "tripped": self.tripped}


class RunPolicy:
    """运行策略门面：集中读取环境变量，提供样本钳制与全量闸门"""

    def __init__(self) -> None:
        self.reload()

    def reload(self) -> None:
        self.smoke_only: bool = not _env_flag("ALLOW_FULL_RUN")
        self.max_samples: int = _env_int("SMOKE_MAX_SAMPLES", 2)
        self.max_llm_calls: int = _env_int("SMOKE_MAX_LLM_CALLS", 6)
        self.llm = LLMBudget(self.max_llm_calls)

    def clamp_samples(self, requested: int, *, stage: str = "", minimum: int = 1) -> int:
        """把请求的样本/场景数压到策略允许的上限内（0 或负数视为「全部」）"""
        if not self.smoke_only:
            return requested
        want = requested if requested and requested > 0 else self.max_samples
        allowed = max(min(want, self.max_samples), minimum)
        if want != allowed:
            print(f"[run_policy] 最小验证模式：{stage or '本次运行'} 请求 {want} 条 → "
                  f"压到 {allowed} 条（上限 SMOKE_MAX_SAMPLES={self.max_samples}）")
        return allowed

    def require_full_permission(self, cli_flag: bool, *, stage: str = "") -> bool:
        """校验是否允许全量：命令行 --allow-full + 环境变量 ALLOW_FULL_RUN=1 缺一不可"""
        if not cli_flag:
            return False
        if self.smoke_only:
            raise RunPolicyError(
                f"{stage or '全量运行'}被拒绝：命令行传了 --allow-full，"
                "但环境变量 ALLOW_FULL_RUN 未设为 1。"
                "本模型为远程 API（非本地部署），全量跑批会产生不可控费用，"
                "必须两道闸门同时打开。"
            )
        return True

    def banner(self) -> str:
        mode = "最小验证（默认）" if self.smoke_only else "全量（已人工放开）"
        return "\n".join([
            "=" * 64,
            "运行策略：远程模型最小验证模式",
            f"  模式        : {mode}",
            f"  样本上限    : {self.max_samples} 条/次",
            f"  调用预算    : {self.max_llm_calls} 次/次运行（已用 {self.llm.used}）",
            "  说明        : 对话模型为 DeepSeek 远程 API（按量计费），非本地部署，",
            "                因此默认不跑完整一轮全量 Agent 流程，只做抽验。",
            "  放开全量    : ALLOW_FULL_RUN=1 + 命令行 --allow-full",
            "=" * 64,
        ])

    def print_banner(self) -> None:
        print(self.banner())

    def snapshot(self) -> dict:
        return {
            "smoke_only": self.smoke_only,
            "max_samples": self.max_samples,
            "max_llm_calls": self.max_llm_calls,
            "llm_budget": self.llm.snapshot(),
        }


#: 进程级单例：所有模块共享同一个预算计数器
run_policy = RunPolicy()

#: 预算对象的快捷别名
LLM_BUDGET = run_policy.llm
