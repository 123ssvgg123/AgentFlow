"""
    订单状态机：memory/order_state.py

    职责：定义订单的合法状态与合法跃迁，作为全项目「订单生命周期唯一权威」。
        任何状态变更都必须经过 transition() 校验，非法跃迁一律拒绝并记录。

    需求依据：需求文档 3.7「全链路可追溯」——
        订单从创建到归档必须有明确的状态流转，任何一环可回溯。

    状态定义（7 态）：
        created        已创建（OrderAgent 校验通过、生成摘要后）
        manual_review  人工待处理（校验超限转入，终态之一，不下单）
        submitted      已下单（POS 受理成功）
        dispatched     已下发后厨（KDS 收到任务）
        cooking        制作中（后厨开始制作）
        served         已出餐
        reviewed       已评价归档（终态）
        cancelled      已取消（终态）

    合法跃迁图：
        created       → submitted | manual_review | cancelled
        manual_review → created（人工修正后重新提交）| cancelled
        submitted     → dispatched | cancelled
        dispatched    → cooking | cancelled
        cooking       → served | cancelled
        served        → reviewed
        reviewed      → （终态，无出边）
        cancelled     → （终态，无出边）

    设计：纯函数模块，零依赖，可被 Agent / API / 脚本任意调用。
"""
from typing import Any, Dict, List, Optional, Tuple

# ==================================================================
# 状态常量
# ==================================================================
STATUS_CREATED = "created"
STATUS_MANUAL_REVIEW = "manual_review"
STATUS_SUBMITTED = "submitted"
STATUS_DISPATCHED = "dispatched"
STATUS_COOKING = "cooking"
STATUS_SERVED = "served"
STATUS_REVIEWED = "reviewed"
STATUS_CANCELLED = "cancelled"

#: 全部合法状态
ALL_STATUSES: Tuple[str, ...] = (
    STATUS_CREATED, STATUS_MANUAL_REVIEW, STATUS_SUBMITTED,
    STATUS_DISPATCHED, STATUS_COOKING, STATUS_SERVED,
    STATUS_REVIEWED, STATUS_CANCELLED,
)

#: 状态中文名（供前端展示与留痕）
STATUS_NAMES: Dict[str, str] = {
    STATUS_CREATED: "已创建",
    STATUS_MANUAL_REVIEW: "待人工处理",
    STATUS_SUBMITTED: "已下单",
    STATUS_DISPATCHED: "已下发后厨",
    STATUS_COOKING: "制作中",
    STATUS_SERVED: "已出餐",
    STATUS_REVIEWED: "已评价归档",
    STATUS_CANCELLED: "已取消",
}

# ==================================================================
# 合法跃迁表
# ==================================================================
#: {当前状态: (允许到达的下一个状态, ...)}
TRANSITIONS: Dict[str, Tuple[str, ...]] = {
    STATUS_CREATED: (STATUS_SUBMITTED, STATUS_MANUAL_REVIEW, STATUS_CANCELLED),
    STATUS_MANUAL_REVIEW: (STATUS_CREATED, STATUS_CANCELLED),
    STATUS_SUBMITTED: (STATUS_DISPATCHED, STATUS_CANCELLED),
    STATUS_DISPATCHED: (STATUS_COOKING, STATUS_CANCELLED),
    STATUS_COOKING: (STATUS_SERVED, STATUS_CANCELLED),
    STATUS_SERVED: (STATUS_REVIEWED,),
    STATUS_REVIEWED: (),
    STATUS_CANCELLED: (),
}

#: 终态集合（无出边）
TERMINAL_STATUSES = frozenset(
    s for s, targets in TRANSITIONS.items() if not targets
)


# ==================================================================
# 校验接口
# ==================================================================

def is_valid_status(status: str) -> bool:
    """判断是否为合法状态值"""
    return status in ALL_STATUSES


def can_transition(from_status: str, to_status: str) -> bool:
    """
    判断跃迁是否合法。

    规则：
        1) 两个状态都必须是合法状态值；
        2) to_status 必须在 TRANSITIONS[from_status] 中；
        3) 同状态「跃迁」视为幂等，返回 False（由调用方决定是否忽略）。
    """
    if not is_valid_status(from_status) or not is_valid_status(to_status):
        return False
    if from_status == to_status:
        return False
    return to_status in TRANSITIONS.get(from_status, ())


def transition(current_status: str, to_status: str,
               order_id: str = "", reason: str = "",
               logger: Any = None) -> Tuple[bool, str]:
    """
    执行状态跃迁校验（不改状态，只判合法性并记录）。

    :return: (是否允许, 说明信息)
        - (True,  "ok")                          —— 合法，调用方可以落库
        - (False, "unknown_status:xxx")          —— 状态值非法
        - (False, "illegal:created->served")     —— 跃迁非法
        - (False, "idempotent:submitted")        —— 同状态，无需变更
    """
    if not is_valid_status(current_status):
        msg = f"unknown_status:{current_status}"
        _log(logger, f"[订单状态机] 拒绝：状态值非法 {current_status}（订单 {order_id}）")
        return False, msg

    if not is_valid_status(to_status):
        msg = f"unknown_status:{to_status}"
        _log(logger, f"[订单状态机] 拒绝：目标状态非法 {to_status}（订单 {order_id}）")
        return False, msg

    if current_status == to_status:
        return False, f"idempotent:{current_status}"

    if to_status not in TRANSITIONS.get(current_status, ()):
        allowed = "/".join(TRANSITIONS.get(current_status, ())) or "无（终态）"
        msg = f"illegal:{current_status}->{to_status}"
        _log(logger,
             f"[订单状态机] 拒绝非法跃迁 {current_status} → {to_status}"
             f"（订单 {order_id}，允许：{allowed}，原因：{reason or '未说明'}）")
        return False, msg

    return True, "ok"


def _log(logger: Any, msg: str) -> None:
    """安全日志（logger 可选，避免本模块强依赖 base.logger）"""
    if logger is not None:
        try:
            logger.warning(msg)
        except Exception:  # noqa: BLE001
            pass


def next_statuses(current_status: str) -> List[str]:
    """返回当前状态允许到达的状态列表"""
    return list(TRANSITIONS.get(current_status, ()))


def describe_chain() -> str:
    """返回状态流转的文本描述（用于文档与自检输出）"""
    lines = []
    for status in ALL_STATUSES:
        targets = TRANSITIONS.get(status, ())
        arrow = "、".join(f"{t}({STATUS_NAMES[t]})" for t in targets) or "终态"
        lines.append(f"  {status:14s}({STATUS_NAMES[status]:6s}) → {arrow}")
    return "\n".join(lines)


# ==================================================================
# 自检
# ==================================================================
if __name__ == '__main__':
    print("=" * 64)
    print("订单状态机自检")
    print("=" * 64)
    print(describe_chain())
    print(f"\n终态集合：{sorted(TERMINAL_STATUSES)}")

    # 断言：正常主干链路全部合法
    chain = [STATUS_CREATED, STATUS_SUBMITTED, STATUS_DISPATCHED,
             STATUS_COOKING, STATUS_SERVED, STATUS_REVIEWED]
    for a, b in zip(chain, chain[1:]):
        assert can_transition(a, b), f"主干跃迁被误判非法：{a}->{b}"
    print("\n主干链路校验通过：created→submitted→dispatched→cooking→served→reviewed")

    # 断言：跳步一律非法
    assert not can_transition(STATUS_CREATED, STATUS_SERVED)
    assert not can_transition(STATUS_SUBMITTED, STATUS_COOKING)
    assert not can_transition(STATUS_REVIEWED, STATUS_CANCELLED)
    print("非法跃迁校验通过：跳步 / 终态出边均被拒绝")

    # 断言：人工处理分支
    assert can_transition(STATUS_CREATED, STATUS_MANUAL_REVIEW)
    assert can_transition(STATUS_MANUAL_REVIEW, STATUS_CREATED)
    assert not can_transition(STATUS_MANUAL_REVIEW, STATUS_SUBMITTED)
    print("人工处理分支校验通过：created⇄manual_review，且不允许直接下单")

    print("\n自检通过。")
