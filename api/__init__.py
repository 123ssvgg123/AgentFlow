"""
    API 层包：api/

    对应需求文档「开发 Web 服务与前端对接：FastAPI + SSE 流式输出 + JWT 认证，
    编写 RESTful API 18 个接口」，包含：

        auth.py       JWT 认证：签发/校验 Token，多租户（tenant_id + user_id + role）
        models.py     请求/响应 Pydantic 模型
        routes.py     RESTful 路由（18 个接口）
        app.py        FastAPI 应用装配（创建应用、挂载路由与静态资源）
"""
