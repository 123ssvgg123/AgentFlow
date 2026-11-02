"""
    平台运行时：agentflow/runtime.py

    职责：把「能力包（数据）」装配成「运行时视图（对象）」。
        · 启动期加载并校验全部能力包，记录当前激活的 pack
        · 对外提供 /api/platform 所需的平台自述（能力包清单 + 护栏 + 证据）
        · 提供 CI 用的 strict 校验入口

    分层位置：L2 编排层中的「装配器」，不参与单次请求的编排决策。
        真正的节点执行仍在 agents/orchestrator.py 与各 Agent 的 graph.py 里。
"""
from typing import Any, Dict, List, Optional

from base.logger import logger
from agentflow.contracts import PackValidationError, VerticalPack
from agentflow.pack_loader import cross_check, load_all, resolve_pack

PLATFORM_NAME = "AgentFlow"
PLATFORM_TIER = "L2 编排层 · 平台装配器"


class PlatformRuntime:
    """平台运行时：持有全部能力包 + 当前激活包。"""

    def __init__(self, packs: Dict[str, VerticalPack], active: Optional[VerticalPack]):
        self.packs = packs
        self.active = active

    # ---------- 装配 ----------

    @classmethod
    def bootstrap(cls) -> "PlatformRuntime":
        """启动期装配。任何装配失败都不阻断进程，但要明确降级到「无能力包」模式。"""
        try:
            packs = load_all()
        except PackValidationError as exc:
            logger.error(f"[platform] 能力包装配失败，降级为无 pack 模式：{exc}")
            packs = {}

        active = None
        if packs:
            try:
                active = resolve_pack()
            except PackValidationError as exc:
                logger.error(f"[platform] 激活能力包失败：{exc}")

        runtime = cls(packs, active)
        runtime.log_banner()
        return runtime

    def log_banner(self) -> None:
        logger.info("=" * 72)
        logger.info(f"  {PLATFORM_NAME} 平台装配（{PLATFORM_TIER}）")
        logger.info("=" * 72)
        if not self.packs:
            logger.warning("[platform] 未发现任何能力包（verticals/*/pack.json）")
            return
        for pid, pack in self.packs.items():
            s = pack.summary()
            logger.info(
                f"[platform] pack={pid:<12} v{pack.version:<6} "
                f"agents={s['agents']:<2} nodes={s['nodes']:<3} "
                f"tools={s['tools']:<3} intents={s['intents']:<2} "
                f"guards={s['guards']:<2} status={pack.status}"
            )
        if self.active:
            logger.info(f"[platform] 当前激活：{self.active.pack_id}"
                        f"（{self.active.display_name}）")

    # ---------- 自述 ----------

    def summary(self) -> Dict[str, Any]:
        """平台自述，供 /api/platform 与前端「平台信息」面板使用。"""
        return {
            "platform": PLATFORM_NAME,
            "tier": PLATFORM_TIER,
            "active_pack": self.active.pack_id if self.active else None,
            "packs": [
                {
                    **p.summary(),
                    "description": p.description,
                    "guards": [
                        {"id": g.id, "type": g.type, "bypass_llm": g.bypass_llm,
                         "scope": g.scope}
                        for g in p.guards
                    ],
                    "evidence": p.evidence,
                }
                for p in self.packs.values()
            ],
        }

    def describe_tools(self) -> List[Dict[str, Any]]:
        """
        合并「能力包里的工具规格」与「运行时工具注册表的实现描述」。

        取并集的理由：pack 描述可靠性等级与归属（平台视角），
        registry 描述实现与分类（代码视角），面试和排障都需要两边对照着看。
        """
        if not self.active:
            return []
        by_name: Dict[str, Dict[str, Any]] = {
            t.name: {
                "name": t.name,
                "category": t.category,
                "owners": t.owners,
                "guarantee": t.guarantee,
                "has_side_effect": t.has_side_effect,
                "description": t.description,
            }
            for t in self.active.tools
        }
        try:
            from tools.registry import tool_registry

            for item in tool_registry.describe():
                slot = by_name.get(item["name"])
                if slot is None:
                    continue
                slot["runtime_category"] = item["category"]
                slot["runtime_agent"] = item["agent"]
        except Exception as exc:  # noqa: BLE001 - 缺依赖时不影响平台自述
            logger.debug(f"[platform] 工具注册表不可用，跳过实现侧合并：{exc}")
        return list(by_name.values())

    def verify(self, strict: bool = True) -> Dict[str, Any]:
        """
        完整校验：契约 + 声明与代码的一致性。

        Returns: {"ok": bool, "packs": {...}, "errors": [...], "warnings": [...]}
        """
        errors: List[str] = []
        warnings: List[str] = []
        detail: Dict[str, Any] = {}

        for pid, pack in self.packs.items():
            try:
                pack.validate()
            except PackValidationError as exc:
                errors.append(f"[{pid}] 契约违规：{exc}")
            rep = cross_check(pack)
            errors.extend(f"[{pid}] {e}" for e in rep["errors"])
            warnings.extend(f"[{pid}] {w}" for w in rep["warnings"])
            detail[pid] = pack.summary()

        ok = not errors
        if strict and not ok:
            raise PackValidationError("；".join(errors))
        return {"ok": ok, "packs": detail, "errors": errors, "warnings": warnings}


# ==================== 全局单例（懒加载） ====================

_runtime: Optional[PlatformRuntime] = None


def get_runtime() -> PlatformRuntime:
    """获取平台运行时单例（首次调用时装配）。"""
    global _runtime
    if _runtime is None:
        _runtime = PlatformRuntime.bootstrap()
    return _runtime


if __name__ == "__main__":
    rt = get_runtime()
    import json

    print(json.dumps(rt.summary(), ensure_ascii=False, indent=2))
