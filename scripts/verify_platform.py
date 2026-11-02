"""
    平台自检：scripts/verify_platform.py

    职责：离线校验「平台层 + 能力包」是否自洽。**不调用任何远程模型**，
        纯静态扫描 + 契约校验，跑一次成本为 0。

    校验内容：
        1. 能力包契约合法（工具名唯一、归属真实、意图可达、护栏覆盖）
        2. 声明的事实与代码事实一致（节点数、工具清单、工具分类）
        3. 关键规模指标符合预期（4 Agent / 33 节点 / 15 工具 / 9 意图）

    用法：
        python scripts/verify_platform.py            # 人读
        python scripts/verify_platform.py --json     # 机读（CI）
        python scripts/verify_platform.py --expect-nodes 33

    退出码：0 通过 / 1 失败
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agentflow.contracts import PackValidationError  # noqa: E402
from agentflow.pack_loader import cross_check, load_pack, discover_pack_files  # noqa: E402
from agentflow.runtime import get_runtime  # noqa: E402


def _self_test_contract(base_pack) -> list:
    """
    契约的反向自测：故意造 4 种违规，确认校验器真的会拦。

    只做正例校验是不够的——一个「永远返回 True」的校验器也能让正例全绿。
    这 4 条覆盖了平台最在意的失效面：重名、幽灵归属、写操作裸奔、确定性工具走模型。
    """
    import copy

    cases = []

    def mutate(fn):
        p = copy.deepcopy(base_pack)
        fn(p)
        return p

    def dup_tool(p):
        p.tools.append(copy.deepcopy(p.tools[0]))

    def ghost_owner(p):
        # frozen dataclass 不能就地赋值，需重建
        from agentflow.contracts import ToolSpec

        t = p.tools[0]
        p.tools[0] = ToolSpec(
            name=t.name, category=t.category, owners=["nonexistent_agent"],
            guarantee=t.guarantee, description=t.description,
            has_side_effect=t.has_side_effect,
        )

    def naked_write(p):
        spec = copy.deepcopy(p.tools[0])
        # frozen dataclass 需要重建
        from agentflow.contracts import ToolSpec

        p.tools[0] = ToolSpec(
            name=spec.name, category=spec.category, owners=spec.owners,
            guarantee="best_effort", description=spec.description, has_side_effect=True,
        )
        p.guards = [g for g in p.guards if g.type != "human_in_the_loop"]

    def exact_without_rule_guard(p):
        p.guards = [g for g in p.guards if g.type != "rule_engine"]

    for name, fn in [
        ("重复工具名", dup_tool),
        ("工具归属幽灵 Agent", ghost_owner),
        ("副作用工具无人工卡点", naked_write),
        ("确定性工具无规则引擎护栏", exact_without_rule_guard),
    ]:
        try:
            mutate(fn).validate()
            cases.append((name, False, "未被拦截"))
        except PackValidationError:
            cases.append((name, True, "已拦截"))
        except Exception as exc:  # noqa: BLE001
            cases.append((name, False, f"异常类型不符：{type(exc).__name__}"))
    return cases


def main() -> int:
    parser = argparse.ArgumentParser(description="AgentFlow 平台与能力包自检")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    parser.add_argument("--expect-nodes", type=int, default=33, help="期望的节点总数")
    parser.add_argument("--expect-tools", type=int, default=15, help="期望的工具总数")
    parser.add_argument("--expect-intents", type=int, default=9, help="期望的意图总数")
    args = parser.parse_args()

    errors = []
    warnings = []
    lines = []

    def emit(text=""):
        lines.append(text)

    # ---------- ① 逐个能力包做契约 + 漂移校验 ----------
    files = discover_pack_files()
    if not files:
        errors.append("未发现任何能力包（verticals/*/pack.json）")

    emit("=" * 72)
    emit("  AgentFlow 平台自检（离线，零模型调用）")
    emit("=" * 72)

    loaded = []
    for path in files:
        try:
            pack = load_pack(path)
        except PackValidationError as exc:
            errors.append(f"{os.path.basename(os.path.dirname(path))}: 契约违规 — {exc}")
            continue
        loaded.append(pack)

        report = cross_check(pack)
        errors.extend(f"[{pack.pack_id}] {e}" for e in report["errors"])
        warnings.extend(f"[{pack.pack_id}] {w}" for w in report["warnings"])

        s = pack.summary()
        emit(f"\n能力包 {pack.pack_id}（{pack.display_name}）v{pack.version}")
        emit(f"  契约校验：通过")
        emit(f"  Agent {s['agents']} 个 / 节点 {s['nodes']} 个 / 工具 {s['tools']} 个 / "
             f"意图 {s['intents']} 个 / 护栏 {s['guards']} 个")
        emit(f"  确定性工具（禁止走模型）：{len(pack.exact_tool_names)} 个 "
             f"→ {', '.join(pack.exact_tool_names) or '无'}")
        emit(f"  人工卡点护栏："
             f"{', '.join(g.id for g in pack.guards if g.type == 'human_in_the_loop') or '无'}")

        # ---------- ② 规模断言（在能力包维度上做，避免多包互相抵消）----------
        if pack.pack_id == "catering":
            checks = [
                ("Agent 数", s["agents"], 4),
                ("节点总数", s["nodes"], args.expect_nodes),
                ("工具数", s["tools"], args.expect_tools),
                ("意图数", s["intents"], args.expect_intents),
            ]
            for label, actual, expect in checks:
                if expect and actual != expect:
                    errors.append(f"[{pack.pack_id}] {label} 期望 {expect}，实际 {actual}")

    # ---------- ③ 契约反向自测 ----------
    if loaded:
        emit("\n" + "-" * 72)
        emit("  契约反向自测（故意造违规，确认校验器会拦）")
        emit("-" * 72)
        for name, passed, detail in _self_test_contract(loaded[0]):
            emit(f"  [{'PASS' if passed else 'FAIL'}] {name}：{detail}")
            if not passed:
                errors.append(f"契约反向自测未通过：{name}（{detail}）")

    # ---------- ④ 运行时装配（走一遍真实 bootstrap 路径）----------
    emit("\n" + "-" * 72)
    emit("  运行时装配")
    emit("-" * 72)
    try:
        rt = get_runtime()
        summary = rt.summary()
        emit(f"  平台：{summary['platform']}（{summary['tier']}）")
        emit(f"  已加载能力包：{len(summary['packs'])} 个，当前激活：{summary['active_pack']}")
        merged = rt.describe_tools()
        emit(f"  工具视图合并：{len(merged)} 条（pack 规格 + 运行时注册表）")
        if not merged:
            warnings.append("启动期未能合并工具视图（运行时注册表可能因缺依赖不可用）")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"运行时装配失败：{exc}")

    # ---------- ④ 结论 ----------
    emit("\n" + "=" * 72)
    for w in warnings:
        emit(f"[WARN] {w}")
    for e in errors:
        emit(f"[FAIL] {e}")
    emit(f"结论：{'通过' if not errors else '未通过'}"
         f"（错误 {len(errors)} 项，告警 {len(warnings)} 项）")
    emit("=" * 72)

    if args.json:
        print(json.dumps({
            "ok": not errors,
            "errors": errors,
            "warnings": warnings,
        }, ensure_ascii=False, indent=2))
    else:
        print("\n".join(lines))

    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
