"""
    共享 State 定义：agents/state.py

    职责：定义 LangGraph 各 Agent 共享的基础状态结构（BaseState），
    以及供 Orchestrator / API 层使用的初始 State 工厂函数。

    与 AGENT 结构对齐：各 Agent 的专属状态字段已下沉到各自子包
    （agents/order/state.py、agents/kitchen/state.py、agents/review/state.py），
    本文件只保留跨 Agent 共享的基础字段。

    LangGraph 核心概念（需求文档 3.3 小贴士）：
        图由「节点」（处理步骤）和「边」（跳转规则）组成。
        每个节点读取一份共享的「状态数据」（State），做一件事，把结果写回状态。

    设计要点：
        · 所有节点共享同一份 State，前一个节点的输出自动成为后一个节点的输入
        · 使用 TypedDict 描述结构（LangGraph 原生支持），同时提供纯 dict 工厂函数，
          保证在 langgraph 未安装时也能降级运行
        · 隔离字段：tenant_id（门店）/ user_id（员工）/ thread_id（任务）
"""
from datetime import datetime
from typing import Any, Dict, List, Optional, TypedDict


class BaseState(TypedDict, total=False):
    """所有 Agent 共享的基础状态字段"""

    # ---------- 隔离标识（需求 3.6 双层隔离） ----------
    tenant_id: str                 # 门店 ID
    user_id: str                   # 员工 ID
    thread_id: str                 # 任务 ID（Checkpointer 隔离键）
    role: str                      # 角色：waiter / kitchen / manager

    # ---------- 会话与输入 ----------
    messages: List[Any]            # 对话消息（短期记忆，按 thread_id 隔离）
    user_input: str                # 用户原始输入
    intent: str                    # 识别到的意图
    intents: List[str]             # 多意图（Pipeline 场景）

    # ---------- 长期记忆注入（任务启动时读取） ----------
    store_preferences: Dict[str, Any]     # 门店偏好
    customer_history: Dict[str, Any]      # 顾客画像

    # ---------- 阶段与状态流转 ----------
    current_stage: str             # 当前阶段
    errors: List[str]              # 错误收集
    degraded_tools: List[str]      # 降级工具记录（可观测性）


class PipelineState(BaseState, total=False):
    """Orchestrator Pipeline 状态：多 Agent 串联时在各 Agent 间传递上下文"""

    pipeline_stages: List[str]          # 待执行的 Agent 序列
    current_pipeline_index: int
    order_result: Dict[str, Any]        # OrderAgent 输出
    kitchen_result: Dict[str, Any]      # KitchenAgent 输出
    review_result: Dict[str, Any]       # ReviewAgent 输出
    final_summary: str


def initial_state(tenant_id: str, user_id: str = "", role: str = "waiter",
                  thread_id: str = "", user_input: str = "",
                  **extra: Any) -> Dict[str, Any]:
    """
    构造初始 State（纯 dict，LangGraph 与降级执行器通用）。

    使用方式：
        state = initial_state("store_001", "waiter_01", user_input="推荐10人套餐")
    """
    state: Dict[str, Any] = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "role": role,
        "thread_id": thread_id,
        "user_input": user_input,
        "messages": [],
        "errors": [],
        "degraded_tools": [],
        "current_stage": "init",
        "created_at": datetime.now().isoformat(),
        "store_preferences": {},
        "customer_history": {},
    }
    state.update(extra)
    return state


def build_messages(user_input: str, history: Optional[List[Dict[str, Any]]] = None) -> List[Any]:
    """
    构造对话消息列表（短期记忆）。
    优先使用 langchain_core 的 HumanMessage/AIMessage；未安装时降级为普通 dict。
    """
    messages: List[Any] = []
    try:
        from langchain_core.messages import AIMessage, HumanMessage
        for item in history or []:
            if item.get("question"):
                messages.append(HumanMessage(content=item["question"]))
            if item.get("answer"):
                messages.append(AIMessage(content=item["answer"]))
        messages.append(HumanMessage(content=user_input))
        return messages
    except ImportError:
        # 降级：普通 dict 形式（语义等价）
        for item in history or []:
            if item.get("question"):
                messages.append({"role": "user", "content": item["question"]})
            if item.get("answer"):
                messages.append({"role": "assistant", "content": item["answer"]})
        messages.append({"role": "user", "content": user_input})
        return messages


def node_count_summary() -> str:
    """
    自检用：从各 Agent 的 `node_order()` **实际计算**节点数，而不是硬编码。

    口径说明：实现为 OrderAgent 10 + KitchenAgent 7 + ReviewAgent 7 + SafetyAgent 9 = **33 节点**
    （需求文档 3.3.1 的「8 节点」为旧口径，OrderAgent 为满足过敏原
    「口头告知 + 留痕」合规要求新增 confirm_allergen_notice，
    另含 require_manual_review 人工复核节点，详见 agents/__init__.py）。

    循环导入说明：本模块被各 Agent 子包的 state 模块导入，若在**模块顶层**导入
    Agent 会触发循环导入，因此这里放在函数内惰性导入；导入失败（如缺少可选依赖、
    模块尚未就绪）时退化为常量 33 并标注来源，保证自检不会因此崩溃。
    """
    expected = [("OrderAgent", 10), ("KitchenAgent", 7), ("ReviewAgent", 7), ("SafetyAgent", 9)]
    try:
        from agents.kitchen.graph import KitchenAgent
        from agents.order.graph import OrderAgent
        from agents.review.graph import ReviewAgent
        from agents.safety.graph import SafetyAgent

        total = 0
        parts = []
        for label, cls in (("OrderAgent", OrderAgent), ("KitchenAgent", KitchenAgent),
                           ("ReviewAgent", ReviewAgent), ("SafetyAgent", SafetyAgent)):
            count = len(cls().node_order())
            total += count
            parts.append(f"{label} {count}")
        return f"{' / '.join(parts)} = {total}（由 node_order() 实际计算）"
    except Exception as exc:  # noqa: BLE001
        total = sum(count for _, count in expected)
        parts = [f"{label} {count}" for label, count in expected]
        return (f"{' / '.join(parts)} = {total}"
                f"（常量兜底：Agent 模块导入失败 {type(exc).__name__}: {exc}）")


if __name__ == '__main__':
    s = initial_state("store_001", "waiter_01", user_input="推荐10人套餐")
    print("初始 State：")
    import json
    print(json.dumps(s, ensure_ascii=False, indent=2))
    print(f"\n节点数定义：{node_count_summary()}")
