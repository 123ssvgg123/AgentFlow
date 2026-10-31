"""
    Agent 层包：agents/

    对应需求文档 3.3「Agent（LangGraph 状态图）详细说明」，按 AGENT 结构组织：

        state.py           共享 State 定义（BaseState + 初始 State 工厂）
        base_agent.py      Agent 基类：LangGraph 图构建 + 无 langgraph 时的降级执行器
        orchestrator.py    Orchestrator 编排器：意图路由 + 多 Agent 串联 Pipeline

        order/             OrderAgent（智能点餐，10 节点）
            state.py / prompts.py / nodes.py / graph.py
        kitchen/           KitchenAgent（后厨调度，7 节点）
            state.py / prompts.py / nodes.py / graph.py
        review/            ReviewAgent（评价分析，7 节点）
            state.py / prompts.py / nodes.py / graph.py
        safety/            SafetyAgent（食安知识问答，9 节点）
            state.py / prompts.py / nodes.py / graph.py

        order_agent.py / kitchen_agent.py / review_agent.py / safety_agent.py
            向后兼容的 re-export shim（既有调用方无需改动）

    共 33 个节点（10 + 7 + 7 + 9）。
    说明：需求文档 3.1 记为 22 个（OrderAgent 8 节点）；后续为满足过敏原
    「口头告知 + 留痕」的合规要求，OrderAgent 新增 confirm_allergen_notice 节点，
    故实际为 10 节点。SafetyAgent 为补齐「食安知识问答」能力新增的第 4 个 Agent。
    差异已在 docs/TOOL_NAME_MAPPING.md 中登记。
"""
