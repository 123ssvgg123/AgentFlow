"""
    Agent 基类：agents/base_agent.py

    职责：封装 LangGraph 状态图的构建流程，并提供一个「无 langgraph 也能跑」的降级执行器。

    设计说明（关键）：
        LangGraph 是本项目的核心框架，但若运行环境未安装 langgraph，
        项目仍应可用——此时降级为「顺序执行器」：按预定义的节点顺序依次执行，
        条件边退化为普通的 if/else 跳转。功能一致，只是失去 Checkpointer 与并行能力。

        这保证了「完整可运行」的硬要求：装不装 langgraph 都能跑通全流程。
"""
from typing import Any, Callable, Dict, List, Optional

from base.logger import logger
from memory.checkpointer import CheckpointerHolder

# 各 Agent 的「终态 stage」约定：状态图跑到这些 stage 才算完整执行。
# 用于检测 LangGraph 因结构问题（如条件边回退到入口）导致的静默终止。
_FINAL_STAGES = {
    "OrderAgent": {"submitted", "manual_review_required"},
    "KitchenAgent": {"dispatched"},
    "ReviewAgent": {"negative_checked", "saved"},
    "SafetyAgent": {"saved"},
}


class BaseAgent:
    """
    Agent 基类。

    子类需实现：
        name             —— Agent 名称（如 "OrderAgent"）
        nodes            —— 节点定义 {节点名: 节点函数}
        entry_point      —— 入口节点名
        edges            —— 固定边 [(from, to), ...]
        conditional_edges—— 条件边 {源节点: (路由函数, {返回值: 目标节点})}
    """

    name: str = "BaseAgent"

    def __init__(self):
        # 节点与图结构（由子类填充）
        self._nodes: Dict[str, Callable] = {}
        self._entry: str = ""
        self._edges: List[tuple] = []
        self._conditional_edges: Dict[str, tuple] = {}
        self._graph = None          # 编译后的 LangGraph 图（langgraph 可用时）
        self._node_order: List[str] = []  # 降级执行器的节点顺序
        self._build()

    # ==================== 子类实现 ====================

    def define_nodes(self) -> Dict[str, Callable]:
        """返回节点定义"""
        raise NotImplementedError

    def define_structure(self) -> Dict[str, Any]:
        """返回图结构：{entry, edges, conditional_edges}"""
        raise NotImplementedError

    def node_order(self) -> List[str]:
        """返回降级执行器的节点顺序（按主流程排列）"""
        return list(self.define_nodes().keys())

    # ==================== 图构建 ====================

    def _build(self):
        """构建 Agent：优先 LangGraph，降级为顺序执行"""
        self._nodes = self.define_nodes()
        structure = self.define_structure()
        self._entry = structure.get("entry", "")
        self._edges = structure.get("edges", [])
        self._conditional_edges = structure.get("conditional_edges", {})
        self._node_order = self.node_order()

        # 尝试构建 LangGraph 图
        try:
            self._graph = self._build_langgraph()
            logger.info(f"[{self.name}] LangGraph 状态图构建完成："
                        f"{len(self._nodes)} 个节点，"
                        f"{len(self._edges)} 条固定边，"
                        f"{len(self._conditional_edges)} 个条件边")
        except ImportError:
            logger.warning(f"[{self.name}] 未安装 langgraph，降级为顺序执行器"
                           f"（{len(self._nodes)} 个节点）")
            self._graph = None
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[{self.name}] LangGraph 构建失败（{exc}），降级为顺序执行器")
            self._graph = None

    def _build_langgraph(self):
        """构建并编译 LangGraph StateGraph"""
        from langgraph.graph import END, StateGraph

        graph = StateGraph(dict)  # 使用 dict 作为 State 载体（兼容 TypedDict 语义）

        # 1. 添加节点
        # 【关键】LangGraph 对 dict schema 的默认行为是「整状态替换」而非「增量合并」：
        # 若节点只返回部分字段，其余字段会被清空，导致条件边读取不到数据（死循环）。
        # 因此这里用 _wrap 把每个节点包装为「合并增量 → 返回完整状态」。
        for node_name, node_func in self._nodes.items():
            graph.add_node(node_name, self._wrap_node(node_func))

        # 2. 设置入口
        graph.set_entry_point(self._entry)

        # 3. 添加固定边
        for src, dst in self._edges:
            graph.add_edge(src, dst)

        # 4. 添加条件边
        for src, (router, mapping) in self._conditional_edges.items():
            graph.add_conditional_edges(src, router, mapping)

        # 5. 编译（绑定 Checkpointer 以支持状态持久化与 interrupt）
        checkpointer = CheckpointerHolder.get()
        return graph.compile(checkpointer=checkpointer) if checkpointer else graph.compile()

    @staticmethod
    def _wrap_node(node_func: Callable) -> Callable:
        """
        包装节点函数：把「返回增量」转换为「返回完整状态」。

        LangGraph 在使用 `dict` 作为状态 schema 时，节点返回值会整体覆盖状态。
        本项目所有节点均采用「返回增量字段」的写法（便于顺序执行器复用），
        故此处统一包装：执行节点 → 将增量合并进入参 state → 返回合并后的完整 state。
        """
        async def _wrapped(state: Dict[str, Any]) -> Dict[str, Any]:
            update = await node_func(state)
            if isinstance(update, dict):
                state.update(update)
            return state
        _wrapped._node_description = getattr(node_func, "_node_description", "")
        return _wrapped

    # ==================== 执行入口 ====================

    async def ainvoke(self, state: Dict[str, Any], thread_id: Optional[str] = None) -> Dict[str, Any]:
        """
        异步执行 Agent。

        有 langgraph：走编译后的状态图（支持条件边 / Checkpointer / interrupt）
        无 langgraph：走顺序执行器（按 node_order 依次执行，条件边退化为简单跳转）
        """
        config = {
            "configurable": {"thread_id": thread_id or state.get("thread_id") or "default"},
            # 递归上限：节点数 × 4，足以覆盖条件边回环（推荐↔校验最多回环 3 次），
            # 同时兜底防止任何意外死循环
            "recursion_limit": max(len(self._nodes) * 4, 25),
        }

        if self._graph is not None:
            try:
                result = await self._graph.ainvoke(state, config=config)
                # 可观测性护栏：LangGraph 在「条件边目标不可达」等结构问题下会
                # 静默结束流（不抛异常），表现为只跑了前几个节点就返回。
                # 这里检测「图未走到终态节点」，必要时补跑一次顺序执行器兜底。
                if not self._reached_terminal(result):
                    logger.warning(f"[{self.name}] 状态图未到达终态"
                                   f"（stage={result.get('current_stage')}），"
                                   f"降级为顺序执行器补跑")
                    return await self._run_sequential(dict(result))
                return result
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[{self.name}] 状态图执行失败（{exc}），降级为顺序执行")
                return await self._run_sequential(state)

        return await self._run_sequential(state)

    def _reached_terminal(self, state: Dict[str, Any]) -> bool:
        """
        判断状态图是否真的跑到了终态。

        终态判定：node_order 中最后一个节点执行后写入的 current_stage。
        各 Agent 的终态 stage 由约定给出；未在表中登记的 Agent 一律视为通过
        （避免误判导致重复执行）。
        """
        final_stages = _FINAL_STAGES.get(self.name)
        if not final_stages:
            return True
        executed = state.get("_executed_nodes")
        stage = state.get("current_stage")
        # 顺序执行器会写 _executed_nodes；LangGraph 路径下靠 stage 判断
        return stage in final_stages

    async def _run_sequential(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """
        降级执行器：按主流程顺序执行节点，并在每个节点后检查条件边的跳转规则。
        逻辑等价于状态图，只是不支持并行与断点续传。
        """
        logger.info(f"[{self.name}] 顺序执行 {len(self._node_order)} 个节点")
        current = self._entry or (self._node_order[0] if self._node_order else None)
        visited = 0
        max_steps = len(self._node_order) * 3  # 防止条件边回环导致死循环

        while current and current != "__end__" and visited < max_steps:
            node_func = self._nodes.get(current)
            if node_func is None:
                logger.warning(f"[{self.name}] 节点 {current} 不存在，结束执行")
                break

            # 执行节点
            try:
                update = await node_func(state)
                if isinstance(update, dict):
                    state.update(update)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[{self.name}] 节点 {current} 执行异常: {exc}")
                state.setdefault("errors", []).append(f"{current}: {exc}")

            visited += 1

            # 决定下一个节点：优先条件边，其次固定边，最后按 node_order 顺序
            current = self._next_node(current, state)

        state["_executed_nodes"] = visited
        return state

    def _next_node(self, current: str, state: Dict[str, Any]) -> Optional[str]:
        """确定下一个节点（条件边 > 固定边 > 顺序表）"""
        # 1. 条件边
        if current in self._conditional_edges:
            router, mapping = self._conditional_edges[current]
            try:
                key = router(state)
                target = mapping.get(key)
                if target:
                    return None if target == "__end__" else target
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[{self.name}] 条件边路由失败: {exc}")

        # 2. 固定边
        for src, dst in self._edges:
            if src == current:
                return None if dst == "__end__" else dst

        # 3. 顺序表
        if current in self._node_order:
            idx = self._node_order.index(current)
            if idx + 1 < len(self._node_order):
                return self._node_order[idx + 1]
        return None

    # ==================== 元信息 ====================

    def info(self) -> Dict[str, Any]:
        """返回 Agent 元信息（供 /api/agents 接口与文档）"""
        return {
            "name": self.name,
            "node_count": len(self._nodes),
            "nodes": list(self._nodes.keys()),
            "edges": [f"{s} → {d}" for s, d in self._edges],
            "conditional_edges": list(self._conditional_edges.keys()),
            "engine": "langgraph" if self._graph is not None else "sequential_fallback",
        }


def node(description: str):
    """
    节点装饰器：为节点函数附加说明（用于文档与可观测性）。

    用法：
        @node("解析顾客点餐需求（人数/口味/过敏原/预算）")
        async def parse_order(state): ...
    """
    def decorator(func: Callable):
        func._node_description = description
        return func
    return decorator
