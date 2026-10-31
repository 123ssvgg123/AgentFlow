"""
verify_fsm.py —— 订单状态机接入验证

覆盖：
    S1  状态机模块自身（主干链路 / 跳步 / 终态）
    S2  OrderAgent 下单 → created/submitted 落库
    S3  OrderAgent 校验超限 → manual_review 终态（不下单）
    S4  KitchenAgent 下发 → submitted → dispatched
    S5  ReviewAgent 评价 → served/reviewed（规则层）
    S6  非法跃迁拒绝且不写库

产物隔离：本脚本会真实写长期记忆（store_fsm_s2/s3/s6 等租户）。
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

_VERIFY_TMP = temp_memory_dir("verify_fsm")

from memory import order_state as sm
from memory.store import get_store

PASS, FAIL = [], []


def _clean_previous_state():
    """
    清理上一轮可能残留的租户数据（同临时目录内的历史文件）。

    为什么必须做：FileStore 以 tenant_id 命名文件（store_fsm_s2.json 等），
    若同名文件已存在，上一轮的订单状态会被 load_order_status() 读到，
    导致「created 已有值 → 幂等判断走 idempotent 分支」等假失败。

    现在产物只落在独立临时目录（_VERIFY_TMP），正常情况下目录是空的；
    这里保留清理逻辑作为增强，兼容：
        · 同一次运行内重复调用（幂等）
        · 人工指定 FOODAGENT_MEMORY_STORE_DIR 时目录里残留的同名文件
    真正的「跑完不留痕」由 _tmp_store 的 atexit 整目录删除保证。
    """
    base = _VERIFY_TMP
    if not os.path.isdir(base):
        return 0
    removed = 0
    for pattern in ("store_fsm_*.json", "verify_*.json", "store_001.json"):
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


async def s1_module():
    print("\n[S1] 状态机模块")
    check("s1.1 主干链路合法",
          all(sm.can_transition(a, b) for a, b in
              [("created", "submitted"), ("submitted", "dispatched"),
               ("dispatched", "cooking"), ("cooking", "served"),
               ("served", "reviewed")]))
    check("s1.2 跳步被拒", not sm.can_transition("created", "served"))
    check("s1.3 终态无出边", not sm.can_transition("reviewed", "cancelled")
          and not sm.can_transition("cancelled", "created"))
    check("s1.4 同状态幂等拒绝", not sm.can_transition("created", "created"))
    check("s1.5 非法状态值拒绝", not sm.can_transition("foo", "created"))
    ok, detail = sm.transition("created", "served", order_id="X", reason="t")
    check("s1.6 transition() 返回非法原因", (not ok) and detail.startswith("illegal:"))


async def s2_order_submit():
    print("\n[S2] OrderAgent 下单状态落库")
    from agents.order_agent import OrderAgent
    from agents.state import initial_state

    agent = OrderAgent()
    tenant, oid = "store_fsm_s2", "ORD_FSM_S2_001"
    state = dict(initial_state(tenant, "u1", user_input="来一份宫保鸡丁"))
    state.update({
        "order_id": oid,
        "recommended_dishes": [{"dish_name": "宫保鸡丁", "quantity": 1, "price": 38}],
        "price_detail": {"total": 38, "per_person": 38,
                         "display": {"subtotal": "¥38", "discount": "-¥0", "total": "¥38"}},
        "customer_needs": {"table_no": "A1", "guest_count": 2},
    })
    await agent._ensure_created(state, oid, tenant)
    store = await get_store()
    st1 = await store.load_order_status(tenant, oid)
    check("s2.1 await _ensure_created 后状态 = created", st1 == "created", f"实际 {st1}")

    r = await agent.submit_order(state)
    check("s2.2 submit_order 返回 submitted", r.get("order_status") == "submitted",
          f"实际 {r.get('order_status')}")
    st2 = await store.load_order_status(tenant, oid)
    check("s2.3 库内状态 = submitted", st2 == "submitted", f"实际 {st2}")

    events = await store.list_order_status_events(tenant, oid)
    chain = [(e["from_status"], e["to_status"]) for e in events]
    check("s2.4 流转链完整 created→submitted",
          chain == [("", "created"), ("created", "submitted")], f"实际 {chain}")
    return tenant, oid


async def s3_manual_review():
    print("\n[S3] 校验超限 → manual_review 终态")
    from agents.order_agent import OrderAgent
    from agents.state import initial_state

    agent = OrderAgent()
    tenant, oid = "store_fsm_s3", "ORD_FSM_S3_001"
    state = dict(initial_state(tenant, "u2", user_input="来一份花生菜"))
    state.update({
        "order_id": oid,
        "recommended_dishes": [{"dish_name": "花生米", "quantity": 1, "price": 12}],
        "validation_issues": ["过敏原未排除", "食材售罄"],
        "price_detail": {"display": {"total": "¥12"}},
        "allergen_result": {"has_allergen": True, "allergens_found": ["花生"]},
    })
    r = await agent.require_manual_review(state)
    check("s3.1 节点返回 manual_review", r.get("order_status") == "manual_review")
    check("s3.2 needs_manual_review = True", r.get("needs_manual_review") is True)
    check("s3.3 未提交 POS/KDS",
          r["submit_result"]["pos"]["submitted"] is False
          and r["submit_result"]["kds"]["dispatched"] is False)

    store = await get_store()
    st = await store.load_order_status(tenant, oid)
    check("s3.4 库内状态 = manual_review", st == "manual_review", f"实际 {st}")

    # manual_review 不允许直接 submitted
    ok, detail = await store.transition_order_status(
        tenant, oid, "submitted", "test", "绕过校验直接下单")
    check("s3.5 manual_review 禁止直接下单", (not ok) and detail.startswith("illegal:"), detail)


async def s4_kitchen_dispatch():
    print("\n[S4] KitchenAgent 下发 → dispatched")
    from agents.kitchen_agent import KitchenAgent

    agent = KitchenAgent()
    tenant, oid = "store_fsm_s2", "ORD_FSM_S2_001"   # 复用 S2 的 submitted 订单
    state = {"tenant_id": tenant, "user_id": "u1",
             "station_assignment": {"stations": {"热菜间": {"tasks": [{"order_id": oid}]}}}}
    await agent._mark_dispatched(state, tenant, [{"order_id": oid, "dish_name": "宫保鸡丁"}])
    store = await get_store()
    st = await store.load_order_status(tenant, oid)
    check("s4.1 submitted → dispatched", st == "dispatched", f"实际 {st}")


async def s5_served_reviewed():
    print("\n[S5] 出餐 → 评价归档")
    store = await get_store()
    tenant, oid = "store_fsm_s2", "ORD_FSM_S2_001"
    for target in ["cooking", "served", "reviewed"]:
        ok, detail = await store.transition_order_status(tenant, oid, target, "system", "S5")
        check(f"s5.{target} 跃迁成功", ok, detail)
    st = await store.load_order_status(tenant, oid)
    check("s5.4 终态 = reviewed", st == "reviewed", f"实际 {st}")

    events = await store.list_order_status_events(tenant, oid)
    chain = [e["to_status"] for e in events]
    check("s5.5 全链路完整",
          chain == ["created", "submitted", "dispatched", "cooking", "served", "reviewed"],
          f"实际 {chain}")


async def s6_reject_no_write():
    print("\n[S6] 非法跃迁不写库")
    store = await get_store()
    tenant, oid = "store_fsm_s6", "ORD_FSM_S6_001"
    await store.transition_order_status(tenant, oid, "created", "test", "init")
    before = len(await store.list_order_status_events(tenant, oid))
    ok, detail = await store.transition_order_status(tenant, oid, "served", "test", "跳步")
    after = len(await store.list_order_status_events(tenant, oid))
    check("s6.1 非法跃迁被拒", not ok, detail)
    check("s6.2 事件数未增加", before == after, f"{before} → {after}")
    st = await store.load_order_status(tenant, oid)
    check("s6.3 状态未被污染", st == "created", f"实际 {st}")


async def main():
    print("=" * 64)
    print("订单状态机接入验证")
    print("=" * 64)
    _clean_previous_state()
    await s1_module()
    await s2_order_submit()
    await s3_manual_review()
    await s4_kitchen_dispatch()
    await s5_served_reviewed()
    await s6_reject_no_write()

    print("\n" + "=" * 64)
    print(f"结果：{len(PASS)} PASS / {len(FAIL)} FAIL")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    print("=" * 64)
    return 0 if not FAIL else 1


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
