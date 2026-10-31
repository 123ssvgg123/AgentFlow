"""
    全系统自检脚本：scripts/selfcheck.py

    用途：一键验证项目完整性，覆盖 6 个维度：
        1. 环境与依赖        —— Python 版本、必需/可选依赖
        2. 配置自检          —— 密钥、意图、过敏原库
        3. 工具注册          —— 15 个工具分类与可达性
        4. Agent 图构建      —— 4 个 Agent / 33 个节点
        5. 记忆系统          —— 仓库与 Checkpointer 就绪
        6. 端到端流程        —— 5 类典型场景实跑（含意图路由与降级追踪）

    用法：
        python scripts/selfcheck.py
        python scripts/selfcheck.py --offline      # 强制离线模式（LLM/联网全降级）
        python scripts/selfcheck.py --no-e2e       # 跳过端到端实跑

    产物隔离：本脚本会真实写长期记忆（租户隔离验证 / 端到端实跑），
             产物一律落在独立临时目录（data/_verify_tmp/），退出时自动清理，
             保证「跑完不留痕」——不再往 data/memory_store/ 丢验证产物。
"""
import argparse
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
# 临时记忆目录助手（与本脚本同目录）
if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _tmp_store import temp_memory_dir

# 必须在任何 memory.* 模块被导入之前生效（FileStore 构造时读取该环境变量）
temp_memory_dir("selfcheck")

PASS, FAIL, WARN = "[PASS]", "[FAIL]", "[WARN]"
_results = {"pass": 0, "fail": 0, "warn": 0}


def log(status: str, msg: str):
    print(f"{status} {msg}")
    if status == PASS:
        _results["pass"] += 1
    elif status == FAIL:
        _results["fail"] += 1
    else:
        _results["warn"] += 1


def section(title: str):
    print("\n" + "=" * 72)
    print(f"  {title}")
    print("=" * 72)


# ==================================================================
# 1. 环境与依赖
# ==================================================================
def check_environment():
    section("① 环境与依赖")

    major, minor = sys.version_info[:2]
    if (major, minor) >= (3, 10):
        log(PASS, f"Python {major}.{minor}")
    else:
        log(FAIL, f"Python {major}.{minor} 过低（要求 >= 3.10）")

    for mod, desc, required in [
        ("fastapi", "Web 框架", True),
        ("uvicorn", "ASGI 服务器", True),
        ("pydantic", "数据校验", True),
        ("jwt", "JWT 认证", True),
        ("dotenv", "环境变量", True),
        ("langgraph", "LangGraph 状态图", False),
        ("openai", "LLM 调用", False),
        ("httpx", "HTTP 客户端", False),
        ("psycopg", "PostgreSQL 驱动", False),
    ]:
        try:
            __import__(mod)
            log(PASS, f"依赖 {mod:<12} {desc}")
        except ImportError:
            log(FAIL if required else WARN,
                f"依赖 {mod:<12} {desc} —— {'必需，请安装' if required else '缺失，将自动降级'}")


# ==================================================================
# 2. 配置自检
# ==================================================================
def check_config():
    section("② 配置自检")
    from base.config import config

    report = config.validate()
    llm = report["llm"]
    log(PASS if llm["api_key_present"] else WARN,
        f"LLM API Key {'已配置（格式合法）' if llm['api_key_present'] else '未配置或格式非法（将降级为规则引擎）'}")
    log(PASS, f"LLM 模型      : {llm['model']} @ {llm['base_url']}")
    log(PASS, f"PG 启用       : {report['postgres']['enabled']}（{report['postgres']['dsn']}）")

    intents = report["intents"]
    if len(intents) == 9:
        log(PASS, f"意图路由      : {len(intents)} 类 {intents}")
    else:
        log(FAIL, f"意图路由应为 9 类，实际 {len(intents)} 类")

    allergens = report["allergen_count"]
    if allergens >= 50:
        log(PASS, f"过敏原库      : {allergens} 条")
    else:
        log(WARN, f"过敏原库仅 {allergens} 条（建议 >= 50）")

    return config


# ==================================================================
# 3. 工具注册
# ==================================================================
def check_tools():
    section("③ 工具注册（15 个）")
    from tools.registry import tool_registry

    tools = tool_registry.describe()
    counts = tool_registry.count()
    total = sum(counts.values())

    for t in tools:
        log(PASS, f"[{t['category_name']:<8}] {t['name']:<28} → {t['agent']}")

    print("-" * 72)
    log(PASS, f"分类统计：{counts}")
    if total == 15:
        log(PASS, f"工具总数：{total}（符合需求 15 个）")
    else:
        log(FAIL, f"工具总数：{total}（应为 15）")

    # 四类各应满足：LLM 4 / 规则 4 / MCP 4 / 外部 3
    expected = {"LLM 工具": 4, "规则引擎": 4, "MCP 工具": 4, "外部 API 封装": 3}
    for cat, exp in expected.items():
        got = counts.get(cat, 0)
        if got == exp:
            log(PASS, f"  {cat:<14} 工具数 {got}（符合 {exp}）")
        else:
            log(FAIL, f"  {cat:<14} 工具数 {got}（应为 {exp}）")
    return counts


# ==================================================================
# 4. Agent 图构建
# ==================================================================
def check_agents():
    section("④ Agent 图构建（4 个 Agent / 33 个节点）")
    from agents.kitchen_agent import kitchen_agent
    from agents.order_agent import order_agent
    from agents.review_agent import review_agent
    from agents.safety_agent import safety_agent

    total_nodes = 0
    # 结构完整性校验（二次审计新增）：节点数对、边数对 ≠ 流程完整。
    # LangGraph 对「没有出边的节点」不报错，而是把图当作正常结束，所以这里用
    # scripts/check_graph_structure.py 的纯 AST 检查补上「无出边/不可达/到不了终态」三类断言。
    from pathlib import Path
    try:
        from check_graph_structure import _check as _check_structure
        from check_graph_structure import _parse_structure
    except ImportError:  # 脚本被单独拷走时降级，不影响其余自检项
        _check_structure = _parse_structure = None

    # OrderAgent 10 节点：在需求文档 8 节点基础上，新增
    #   · confirm_allergen_notice（过敏原口头告知留痕，合规要求）
    #   · require_manual_review（校验失败转人工，拦截不可下单订单）
    # SafetyAgent 9 节点：食安/营养知识问答（轻量 RAG）
    # 详见 docs/TOOL_NAME_MAPPING.md 与 agents/order_agent.py 模块头说明。
    for agent, expected_nodes, agent_key in [(order_agent, 10, "order"),
                                             (kitchen_agent, 7, "kitchen"),
                                             (review_agent, 7, "review"),
                                             (safety_agent, 9, "safety")]:
        info = agent.info()
        total_nodes += info["node_count"]
        engine = info["engine"]
        if info["node_count"] == expected_nodes:
            log(PASS, f"{info['name']:<14} {info['node_count']} 节点 / "
                      f"{len(info['edges'])} 固定边 / {len(info['conditional_edges'])} 条件边 "
                      f"[引擎 {engine}]")
        else:
            log(FAIL, f"{info['name']:<14} 节点数 {info['node_count']}（应为 {expected_nodes}）")
        log(PASS, f"   节点：{' → '.join(info['nodes'])}")

        if _check_structure and _parse_structure:
            try:
                graph_file = Path(__file__).resolve().parent.parent / "agents" / agent_key / "graph.py"
                problems = _check_structure(agent_key, _parse_structure(graph_file))
            except Exception as exc:  # noqa: BLE001
                problems = [f"结构校验异常：{exc}"]
            if problems:
                for problem in problems:
                    log(FAIL, f"   结构：{problem}")
            else:
                log(PASS, "   结构：无出边 / 不可达 / 无法收敛 三项检查通过")

    print("-" * 72)
    if total_nodes == 33:
        log(PASS, f"节点总数：{total_nodes}（符合当前实现 33 个）")
    else:
        log(FAIL, f"节点总数：{total_nodes}（应为 33）")
    return total_nodes


# ==================================================================
# 5. 记忆系统
# ==================================================================
async def check_memory():
    section("⑤ 记忆系统")
    from memory.checkpointer import CheckpointerHolder
    from memory.schemas import TABLES
    from memory.store import get_store

    log(PASS, f"表结构定义：{len(TABLES)} 张 {'、'.join(TABLES.keys())}")

    try:
        store = await get_store()
        log(PASS, f"长期记忆仓库：{type(store).__name__}")
    except Exception as exc:  # noqa: BLE001
        log(FAIL, f"长期记忆仓库初始化失败：{exc}")
        return

    cp = CheckpointerHolder.get()
    log(PASS, f"短期记忆检查点器：{type(cp).__name__ if cp else 'none'}")

    # 隔离性验证
    try:
        await store.save_store_preferences("__selfcheck_a__", {"popular_dishes": ["测试菜"]})
        a = await store.load_store_preferences("__selfcheck_a__")
        b = await store.load_store_preferences("__selfcheck_b__")
        if a.get("popular_dishes") == ["测试菜"] and not b.get("popular_dishes"):
            log(PASS, "租户隔离验证：A 门店数据对 B 门店不可见")
        else:
            log(FAIL, "租户隔离验证失败")
    except Exception as exc:  # noqa: BLE001
        log(FAIL, f"隔离性验证异常：{exc}")


# ==================================================================
# 6. 端到端流程
# ==================================================================
async def check_e2e():
    section("⑥ 端到端流程（默认最小验证：只跑 1 类场景）")
    from agents.orchestrator import orchestrator
    from base.run_policy import run_policy

    cases = [
        ("菜品推荐", "帮我推荐适合10人聚餐的套餐，有两位素食，预算800元", "order"),
        ("过敏原查询", "有顾客对花生过敏，帮我确认菜品安全性", "order"),
        ("后厨调度", "现在后厨有多少单在排队，帮我安排出餐顺序", "kitchen"),
        ("评价分析", "帮我分析一下本周的顾客评价", "review"),
        ("多 Agent 全流程", "今天是我们店开业三周年，帮我从菜品推荐到评价分析全流程处理", None),
    ]

    # ⚠️ 运行策略：对话模型是远程 API（非本地部署、按 token 计费）。
    #    默认不跑完整 5 类场景，只抽 1 条验证链路通不通；跑全量需
    #    ALLOW_FULL_RUN=1 + --allow-full（见 base/run_policy.py）。
    if run_policy.smoke_only:
        limit = 1
        print(f"[run_policy] 最小验证模式：5 类场景 → 只跑前 {limit} 类"
              f"（放开全量：ALLOW_FULL_RUN=1 + --allow-full）")
        cases = cases[:limit]

    for idx, (title, query, expect_agent) in enumerate(cases, 1):
        try:
            r = await orchestrator.run(query, tenant_id="store_001", user_id="selfcheck", role="waiter")
        except Exception as exc:  # noqa: BLE001
            log(FAIL, f"[{idx}] {title} 执行异常：{exc}")
            continue

        answer = r.get("answer") or ""
        agents = r.get("agents_invoked", [])
        errors = r.get("errors", [])

        if not answer:
            log(FAIL, f"[{idx}] {title}：回答为空")
            continue
        if errors:
            log(WARN, f"[{idx}] {title}：完成但有错误 {errors}")
            continue

        ok = True
        if expect_agent and expect_agent not in agents:
            log(WARN, f"[{idx}] {title}：期望 {expect_agent}，实际 {agents}")
            ok = False
        else:
            log(PASS, f"[{idx}] {title}：意图 {r['intents']} → Agent {agents} "
                      f"（Pipeline={r['is_pipeline']}，回答 {len(answer)} 字，"
                      f"降级 {len(r['degraded_tools'])} 项）")

    # Pipeline 专项校验（最小验证模式下跳过：一次 Pipeline 会连调多个 Agent，
    # 是单次运行里最贵的一条路径，留给人工放开全量时再验）
    print("-" * 72)
    if run_policy.smoke_only:
        log(WARN, "Pipeline 专项校验已跳过（最小验证模式，避免多 Agent 连调产生额外远程调用）")
        return
    try:
        r = await orchestrator.run(
            "帮我从菜品推荐到后厨安排再到评价分析全流程处理",
            tenant_id="store_001", user_id="selfcheck", role="manager")
        if r.get("is_pipeline") and len(r.get("agents_invoked", [])) >= 2:
            log(PASS, f"多 Agent Pipeline 验证：{' → '.join(r['agents_invoked'])}")
        else:
            log(WARN, f"Pipeline 未触发（意图 {r.get('intents')}，Agent {r.get('agents_invoked')}）")
    except Exception as exc:  # noqa: BLE001
        log(FAIL, f"Pipeline 验证异常：{exc}")


# ==================================================================
# 主流程
# ==================================================================
def main():
    parser = argparse.ArgumentParser(description="FoodAgent 全系统自检")
    parser.add_argument("--offline", action="store_true", help="强制离线（LLM 全降级）")
    parser.add_argument("--no-e2e", action="store_true", help="跳过端到端实跑")
    parser.add_argument("--allow-full", action="store_true",
                        help="允许全量（需同时设置 ALLOW_FULL_RUN=1；默认仅最小验证）")
    args = parser.parse_args()

    # 运行策略：远程模型最小验证（硬限制见 base/run_policy.py）
    from base.run_policy import RunPolicyError, run_policy
    run_policy.print_banner()
    try:
        run_policy.require_full_permission(args.allow_full, stage="selfcheck 全量")
    except RunPolicyError as exc:
        print(f"\n[自检中止] {exc}\n")
        return 2
    print()

    if args.offline:
        os.environ["LLM_ENABLED"] = "false"
        print("[模式] 离线自检：LLM 走降级分支")

    print("#" * 72)
    print("#  FoodAgent AI 智能餐饮 Agent 系统 · 全系统自检")
    print(f"#  项目根目录：{ROOT}")
    print("#" * 72)

    check_environment()
    check_config()
    check_tools()
    check_agents()

    async def _async_part():
        await check_memory()
        if not args.no_e2e:
            await check_e2e()

    asyncio.run(_async_part())

    print("\n" + "#" * 72)
    print(f"#  自检汇总：通过 {_results['pass']} · 警告 {_results['warn']} · 失败 {_results['fail']}")
    print("#" * 72)
    if _results["fail"] == 0:
        print("  结论：系统自检通过，可正常启动。")
        return 0
    print("  结论：存在失败项，请先修复后再启动。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
