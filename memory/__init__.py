"""
    记忆系统包：memory/

    对应需求文档 3.6「用户隔离与记忆机制」，实现三层记忆架构：

        第一层  短期记忆（会话级）—— LangGraph Checkpointer，按 thread_id 隔离
        第二层  长期记忆（门店级）—— PostgreSQL，按 tenant_id + user_id 双层隔离
        第三层  餐饮行业特定记忆 —— 门店偏好 / 顾客画像 / 评价分析 / 食材消耗

    文件分工：
        schemas.py       数据表结构定义 + 建表 SQL（唯一来源）
        store.py         长期记忆读写仓库（PostgreSQL 实现 + 文件降级实现）
        checkpointer.py  LangGraph Checkpointer 工厂（PostgresSaver / MemorySaver 自动切换）
"""
