"""
    工具层包：tools/

    15 个工具按「四类分工」组织，对应需求文档 3.4 工具调用清单：

        llm_tools.py        LLM 工具（4 个）   —— 需要「理解自然语言」的任务
        rule_tools.py       规则引擎（4 个）   —— 纯数学/精确匹配，代码算又快又准
        mcp_tools.py        MCP 工具（4 个）   —— 知识库检索 / 联网搜索
        external_api.py     外部 API 封装（3 个）—— 对接甲方业务系统
        llm_client.py       DeepSeek 客户端（底层依赖，非工具本身）
"""
