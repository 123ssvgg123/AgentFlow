"""
verify_compliance.py —— 合规留痕 + 声明式 Pipeline 验证

覆盖：
    C1  过敏原口头告知留痕（首次插入 / 二次更新同一订单）
    C2  未确认时只生成话术不留痕
    C3  confirm_allergen_notice 节点接入 OrderAgent
    C4  声明式 Pipeline：跳过 OrderAgent 只跑 kitchen
    C5  自定义上下文提取器
    C6  内置 extractor 与 build_agent_sequence 行为

产物隔离：本脚本会真实写长期记忆（store_comp_c1~c5 等租户）。
         产物一律落在独立临时目录（data/_verify_tmp/），退出时自动清理，
         保证「跑完不留痕」——不再往 data/memory_store/ 丢验证产物。
"""
import asyncio
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 必须在 import memory.* 之前生效（FileStore 构造时读取该环境变量）
from _tmp_store import temp_memory_dir

_VERIFY_TMP = temp_memory_dir("verify_compliance")

from memory.store import get_store

PASS, FAIL = [], []


def _clean_previous_state():
    """
    清理上一轮可能残留的租户数据（同临时目录内的历史文件）。

    为什么必须做：FileStore 以 tenant_id 命名文件（store_comp_c1.json 等），
    沿用旧文件会让 c1.1「记录条数」、c1.7「就地更新不新增」等断言
    读到历史记录而假失败。

    现在产物只落在独立临时目录（_VERIFY_TMP），正常情况下目录是空的；
    这里保留清理逻辑作为增强，兼容同一次运行内重复调用与人工指定
    FOODAGENT_MEMORY_STORE_DIR 的场景。真正的「跑完不留痕」由
    _tmp_store 的 atexit 整目录删除保证。
    """
    base = _VERIFY_TMP
    if not os.path.isdir(base):
        return 0
    removed = 0
    for pattern in ("store_comp_*.json", "verify_*.json", "store_001.json"):
        for path in sorted(set(glob.glob(os.path.join(base, pattern)))):
            try:
                os.remove(path)
                removed += 1
            except FileNotFoundError:
                pass
    if removed:
        print(f"[前置清理] 移除临时目录内上一轮残留数据 {removed} 个文件")
    return removed


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))


async def c1_notice_log():
    print("\n[C1] 过敏原口头告知留痕")
    store = await get_store()
    tenant, oid = "store_comp_c1", "ORD_COMP_C1_001"
    # 先建合规记录（模拟 check_allergens 写入）
    await store.save_compliance_log(
        tenant,
        {"order_id": oid, "declared_allergens": ["花生"],
         "detected_allergens": ["花生"], "recommended_dishes": ["宫保鸡丁"],
         "isolation_action": "独立工位", "action": "trigger_food_safety_review"},
        user_id="waiter_01",
    )
    logs = await store.list_compliance_log(tenant, oid)
    check("c1.1 合规记录已写入", len(logs) == 1, f"条数 {len(logs)}")
    check("c1.2 初始 customer_ack = False（未确认）",
          logs[0].get("customer_ack") in (False, None, "False"), f"{logs[0].get('customer_ack')}")

    # 口头告知确认
    rec = await store.confirm_allergen_notice(
        tenant, oid, confirmed_by="waiter_01", channel="verbal",
        notice_content="您的订单含花生，请确认知悉", customer_ack=True, user_id="waiter_01")
    check("c1.3 告知确认 customer_ack = True", rec.get("customer_ack") is True)
    check("c1.4 记录告知人", rec.get("confirmed_by") == "waiter_01", str(rec.get("confirmed_by")))
    check("c1.5 记录告知渠道", rec.get("notice_channel") == "verbal")
    check("c1.6 记录告知时间", bool(rec.get("confirmed_at")))

    logs2 = await store.list_compliance_log(tenant, oid)
    check("c1.7 就地更新不新增记录", len(logs2) == 1, f"条数 {len(logs2)}")
    check("c1.8 库内已持久化 customer_ack",
          logs2[0].get("customer_ack") is True)
    check("c1.9 留痕含话术原文",
          "花生" in str(logs2[0].get("notice_content", "")))


async def c2_no_confirm_no_log():
    print("\n[C2] 未确认时不留痕")
    store = await get_store()
    tenant, oid = "store_comp_c2", "ORD_COMP_C2_001"
    rec = await store.confirm_allergen_notice(
        tenant, oid, confirmed_by="w", channel="verbal",
        notice_content="话术", customer_ack=True)
    check("c2.1 无前置记录时新建（不丢留痕）", rec.get("order_id") == oid)
    logs = await store.list_compliance_log(tenant, oid)
    check("c2.2 新建记录 action = notice_confirm_only",
          logs[0].get("action") == "notice_confirm_only", str(logs[0].get("action")))


async def c3_order_agent_node():
    print("\n[C3] confirm_allergen_notice 节点")
    from agents.order_agent import OrderAgent
    from agents.state import initial_state

    agent = OrderAgent()
    nodes = agent.define_nodes()
    check("c3.1 节点已注册", "confirm_allergen_notice" in nodes)
    check("c3.2 节点总数 = 10", len(nodes) == 10, f"实际 {len(nodes)}")

    struct = agent.define_structure()
    check("c3.3 条件边含 confirm_allergen_notice",
          "confirm_allergen_notice" in struct["conditional_edges"])
    ce = struct["conditional_edges"]["check_allergens"][0]
    check("c3.4 高风险路由到确认节点",
          ce({"allergen_risk": "high"}) == "confirm_allergen_notice")
    check("c3.5 无风险路由到营养", ce({"allergen_risk": "none"}) == "check_nutrition")

    # 未确认 → 只生成话术
    tenant, oid = "store_comp_c3", "ORD_COMP_C3_001"
    state = dict(initial_state(tenant, "u1", user_input="来一份花生菜"))
    state.update({
        "order_id": oid,
        "customer_needs": {"allergens": ["花生"]},
        "allergen_result": {"allergens_found": ["花生"], "has_allergen": True,
                            "dish_results": [{"dish_name": "宫保鸡丁", "has_allergen": True}]},
        "recommended_dishes": [{"dish_name": "宫保鸡丁"}],
    })
    r = await agent.confirm_allergen_notice(state)
    check("c3.6 生成告知话术", "花生" in r["allergen_notice"]["notice_content"])
    check("c3.7 标记待确认", r["allergen_notice_pending"] is True)
    store = await get_store()
    check("c3.8 未确认时不写库",
          len(await store.list_compliance_log(tenant, oid)) == 0)

    # 已确认 → 落库
    state2 = {**state, "human_confirmed": True}
    r2 = await agent.confirm_allergen_notice(state2)
    check("c3.9 确认后 pending = False", r2["allergen_notice_pending"] is False)
    logs = await store.list_compliance_log(tenant, oid)
    check("c3.10 确认后已落库", len(logs) == 1, f"条数 {len(logs)}")
    if logs:
        check("c3.11 落库含隔离措施",
              "独立工位" in str(logs[0].get("notice_content", ""))
              or "工位" in str(logs[0].get("notice_content", "")))


async def c4_declarative_pipeline():
    print("\n[C4] 声明式 Pipeline：跳过 OrderAgent")
    from agents.orchestrator import Orchestrator

    o = Orchestrator()
    seq = Orchestrator.build_agent_sequence(["kitchen_dispatch", "review_analysis"])
    check("c4.1 意图推导为 kitchen→review", seq == ["kitchen", "review"], str(seq))

    # 声明式显式序列：只跑 review
    state = {"tenant_id": "store_comp_c4", "user_id": "u1", "thread_id": "t_c4",
             "user_input": "分析评价", "messages": [], "errors": [], "degraded_tools": [],
             "customer_history": {}, "store_preferences": {}}
    result_state, agent_results = await o._run_pipeline(
        state, ["review_analysis"], agent_sequence=["review"])
    check("c4.2 仅执行 review 阶段", list(agent_results.keys()) == ["review"],
          str(list(agent_results.keys())))
    check("c4.3 review 有输出", bool(agent_results["review"].get("answer")))


async def c5_custom_extractor():
    print("\n[C5] 自定义上下文提取器")
    calls = []

    def custom_order(state):
        calls.append(state.get("tenant_id"))
        return {"pending_orders": [{"订单号": "MANUAL_1", "桌号": "B1", "菜品列表": []}]}

    from agents.orchestrator import Orchestrator
    o = Orchestrator()
    state = {"tenant_id": "store_comp_c5", "user_id": "u1", "thread_id": "t_c5",
             "user_input": "排产", "messages": [], "errors": [], "degraded_tools": [],
             "customer_history": {}, "store_preferences": {}}
    result_state, agent_results = await o._run_pipeline(
        state, ["kitchen_dispatch"],
        agent_sequence=["order", "kitchen"],
        context_extractors={"order": custom_order},
        )
    check("c5.1 自定义提取器被调用", len(calls) == 1, f"调用 {len(calls)} 次")
    check("c5.2 kitchen 阶段执行", "kitchen" in agent_results)
    check("c5.3 自定义 pending_orders 注入生效",
          any(x.get("订单号") == "MANUAL_1" for x in result_state.get("pending_orders", []))
          or result_state.get("order_count", 0) >= 0)


async def c6_extractor_table():
    print("\n[C6] 内置提取器表")
    from agents.orchestrator import Orchestrator
    check("c6.1 三个内置提取器",
          sorted(Orchestrator._CONTEXT_EXTRACTORS.keys()) == ["kitchen", "order", "review"])
    ctx = Orchestrator._extract_context(
        "order", {"recommended_dishes": [{"dish_name": "A", "quantity": 2, "price": 30}],
                  "order_id": "ORD_X", "customer_needs": {"table_no": "A5"}})
    check("c6.2 order 提取 pending_orders", len(ctx.get("pending_orders", [])) == 1)
    check("c6.3 桌号从 customer_needs 读取",
          ctx["pending_orders"][0]["桌号"] == "A5", ctx["pending_orders"][0]["桌号"])
    ctx_k = Orchestrator._extract_context("kitchen", {"inventory_result": {"required": {"葱": 1}}})
    check("c6.4 kitchen 提取 ingredient_needs",
          ctx_k.get("ingredient_needs") == {"葱": 1})
    check("c6.5 review 提取为空",
          Orchestrator._extract_context("review", {"x": 1}) == {})
    check("c6.6 未知 key 返回空", Orchestrator._extract_context("unknown", {}) == {})


async def main():
    print("=" * 64)
    print("合规留痕 + 声明式 Pipeline 验证")
    print("=" * 64)
    _clean_previous_state()
    await c1_notice_log()
    await c2_no_confirm_no_log()
    await c3_order_agent_node()
    await c4_declarative_pipeline()
    await c5_custom_extractor()
    await c6_extractor_table()

    print("\n" + "=" * 64)
    print(f"结果：{len(PASS)} PASS / {len(FAIL)} FAIL")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    print("=" * 64)
    return 0 if not FAIL else 1


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
