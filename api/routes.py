"""
    RESTful 路由：api/routes.py

    职责：定义全部 RESTful 接口（需求文档：18 个接口），包括：
        · 认证（2）      登录、当前用户
        · 对话（3）      创建任务、对话（非流式）、对话（SSE 流式）、历史查询/清除
        · 订单（3）      下单、查询订单、订单列表
        · 后厨（2）      待处理订单、排产看板
        · 评价（2）      评价分析、差评回复审核
        · 记忆（3）      门店偏好读写、顾客画像读写
        · 元信息（4）    健康检查、工具清单、Agent 清单、意图清单、快捷问题、集成状态

    接口总览（18 个业务接口 + 辅助）见文件末尾 ENDPOINTS 常量与 /api/endpoints 接口。

    设计约束：本层只做「参数校验 + 转发」，业务逻辑全部在 agents/ 与 memory/ 中。
"""
import asyncio
import inspect
import json
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from agents.kitchen_agent import kitchen_agent
from agents.order_agent import order_agent
from agents.orchestrator import orchestrator
from agents.review_agent import review_agent
from agents.safety_agent import safety_agent
from agents.state import initial_state
from api.auth import auth_service
from api.models import (ChatRequest, ChatResponse, LoginRequest, LoginResponse,
                        MemoryUpdateRequest, OrderCreateRequest, SimpleResponse,
                        StorePreferenceRequest)
from base.config import config
from base.logger import logger
from memory.checkpointer import CheckpointerHolder
from memory.store import get_store
from prompts.templates import Prompts
from tools.external_api import get_order_detail, list_pending_orders
from tools.registry import tool_registry

router = APIRouter(prefix="/api", tags=["FoodAgent"])


# ==================================================================
# 一、认证接口（2 个）
# ==================================================================

@router.post("/auth/login", response_model=LoginResponse, summary="① 登录")
async def login(req: LoginRequest):
    """
    门店员工登录，返回 JWT Token。
    演示账号：waiter_01 / waiter_02 / kitchen_01 / manager_01，密码均为 123456
    """
    account = auth_service.authenticate(req.username, req.password)
    if not account:
        raise HTTPException(status_code=401, detail="工号或密码错误")
    token = auth_service.create_token(account)
    return LoginResponse(
        access_token=token,
        expires_in=config.JWT_EXPIRE_MINUTES * 60,
        user={
            "tenant_id": account["tenant_id"], "user_id": account["user_id"],
            "role": account["role"], "name": account["name"],
        },
    )


@router.get("/auth/me", summary="② 当前用户信息")
async def current_user(user: Dict[str, Any] = Depends(auth_service.current_user)):
    """返回当前登录用户信息（从 Token 解析）"""
    return {"user": user}


# ==================================================================
# 二、对话接口（3 个）
# ==================================================================

@router.post("/tasks", response_model=SimpleResponse, summary="③ 创建任务")
async def create_task(user: Dict[str, Any] = Depends(auth_service.current_user)):
    """
    创建新任务（对应需求文档 4.1「左侧边栏 → 新建任务按钮」）。
    返回 thread_id，后续对话带上该 ID 即可实现多轮记忆。
    """
    from memory.checkpointer import build_thread_id
    thread_id = build_thread_id(user["tenant_id"], user["user_id"])
    return SimpleResponse(message="任务已创建", data={"thread_id": thread_id,
                                                     "tenant_id": user["tenant_id"]})


@router.post("/chat", response_model=ChatResponse, summary="④ 对话（非流式）")
async def chat(req: ChatRequest, user: Dict[str, Any] = Depends(auth_service.current_user)):
    """
    核心对话接口：Orchestrator 意图路由 → Agent 执行 → 结果汇总。

    支持：
        · 单 Agent 直达（如"推荐菜品"→ OrderAgent）
        · 多 Agent 串联 Pipeline（如"从推荐到评价全流程处理"）
        · 多轮记忆（传 thread_id）
        · Human-in-the-Loop（传 human_confirmed / manager_confirmed 继续执行）
    """
    start = time.time()
    tenant_id = req.tenant_id or user["tenant_id"]
    logger.info(f"[API] /api/chat 门店={tenant_id} 员工={user['user_id']} "
                f"query={req.query[:60]}")

    result = await orchestrator.run(
        user_input=req.query,
        tenant_id=tenant_id,
        user_id=user["user_id"],
        role=user["role"],
        thread_id=req.thread_id,
        customer_id=req.customer_id or "",
        human_confirmed=req.human_confirmed,
        manager_confirmed=req.manager_confirmed,
        manager_request_revise=req.manager_request_revise,
        priority_adjustment=req.priority_adjustment,
        allergen_notice_confirmed=req.allergen_notice_confirmed,
        notice_channel=req.notice_channel or "verbal",
    )
    result["processing_time"] = round(time.time() - start, 2)
    logger.info(f"[API] /api/chat 完成 耗时 {result['processing_time']}s "
                f"意图={result.get('intents')} 降级={result.get('degraded_tools')}")
    return ChatResponse(**{k: v for k, v in result.items() if k in ChatResponse.model_fields})


@router.get("/chat/stream", summary="⑤ 对话（SSE 流式）")
async def chat_stream(query: str, thread_id: Optional[str] = None,
                      customer_id: Optional[str] = None,
                      human_confirmed: bool = False,
                      manager_confirmed: bool = False,
                      manager_request_revise: bool = False,
                      priority_adjustment: Optional[str] = None,
                      allergen_notice_confirmed: bool = False,
                      notice_channel: str = "verbal",
                      token: Optional[str] = None):
    """
    SSE 流式对话接口（需求文档 4.1「流式输出：SSE 逐字输出，打字机效果」）。

    事件类型：
        intent  意图识别结果
        stage   Agent 阶段进度
        token   回答分片（逐字）
        result  最终结果元数据
        error   错误

    说明：EventSource 无法自定义 Header，故 Token 通过 query 参数传入。
    """
    # 校验 Token
    if not token:
        raise HTTPException(status_code=401, detail="未提供登录凭证")
    user = auth_service.decode_token(token)

    async def event_generator():
        try:
            async for event_type, data in orchestrator.run_stream(
                user_input=query,
                tenant_id=user["tenant_id"],
                user_id=user["user_id"],
                role=user.get("role", "waiter"),
                thread_id=thread_id,
                customer_id=customer_id or "",
                human_confirmed=human_confirmed,
                manager_confirmed=manager_confirmed,
                manager_request_revise=manager_request_revise,
                priority_adjustment=priority_adjustment,
                allergen_notice_confirmed=allergen_notice_confirmed,
                notice_channel=notice_channel,
            ):
                yield f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[API] SSE 异常: {exc}")
            yield f"event: error\ndata: {json.dumps({'message': str(exc)}, ensure_ascii=False)}\n\n"
        finally:
            yield "event: done\ndata: {}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                 "X-Accel-Buffering": "no"},
    )


@router.delete("/chat/history/{thread_id}", response_model=SimpleResponse, summary="⑥ 清除任务历史")
async def clear_history(thread_id: str, user: Dict[str, Any] = Depends(auth_service.current_user)):
    """
    清除指定任务的对话历史。

    修复（二次审计）：原实现只打一行日志就返回「已清除」，Checkpointer 里的 State
    完全没动——前端以为清空了，模型下一轮仍能读到上下文，属于「接口说谎」。
    现在真正调用 Checkpointer 的 delete_thread，并兼容 delete_thread / adelete_thread
    两种命名（不同 langgraph 版本），无 Checkpointer 时如实返回未清除。
    """
    tenant_id = user["tenant_id"]
    logger.info(f"[API] 清除任务历史 {thread_id}（门店 {tenant_id}）")

    deleted, detail = False, ""
    checkpointer = CheckpointerHolder.get()
    if checkpointer is None:
        detail = "未安装 langgraph，无持久化状态可清除"
    else:
        for method_name in ("delete_thread", "adelete_thread"):
            method = getattr(checkpointer, method_name, None)
            if method is None:
                continue
            try:
                ret = method(thread_id)
                if inspect.isawaitable(ret):
                    await ret
                deleted, detail = True, f"checkpointer.{method_name}"
                break
            except Exception as exc:  # noqa: BLE001
                detail = f"{method_name} 失败: {exc}"
        if not deleted and not detail:
            detail = "当前 Checkpointer 不支持 delete_thread"

    return SimpleResponse(
        status="success" if deleted else "partial",
        message=(f"任务 {thread_id} 的历史已清除（{detail}）" if deleted
                 else f"任务 {thread_id} 未清除：{detail}"),
        data={"thread_id": thread_id, "deleted": deleted, "detail": detail},
    )


# ==================================================================
# 三、订单接口（4 个）
# ==================================================================

@router.post("/orders", response_model=SimpleResponse, summary="⑦ 创建订单")
async def create_order(req: OrderCreateRequest,
                       user: Dict[str, Any] = Depends(auth_service.current_user)):
    """
    直接下单（不走对话，供平板点餐终端调用）。
    内部走 OrderAgent 的规则引擎（过敏原检测 + 价格计算 + 提交）。
    """
    state = initial_state(
        tenant_id=user["tenant_id"], user_id=user["user_id"], role=user["role"],
        user_input="直接下单",
        customer_needs={"guest_count": req.guest_count, "allergens": req.allergens,
                        "table_no": req.table_no, "dietary_restrictions": [],
                        "taste_preferences": [], "budget_per_person": 0, "budget_total": 0,
                        "occasion": "日常用餐", "special_requests": [], "mentioned_dishes": []},
        recommended_dishes=[{"dish_name": d.get("dish_name", ""), "quantity": d.get("quantity", 1),
                             "price": d.get("price", 0),
                             "allergens": d.get("allergens", [])} for d in req.dishes],
        customer_history={"member_level": req.member_level},
    )
    result = await order_agent.ainvoke(state, thread_id=f"{user['tenant_id']}_direct")
    formatted = order_agent.format_result(result)
    return SimpleResponse(message="订单已提交", data={
        "order_id": formatted["order_id"],
        "allergen_risk": formatted["allergen_risk"],
        "allergen_result": formatted["allergen"],
        "price": formatted["price"],
        "summary": formatted["answer"],
    })


@router.get("/orders/{order_id}", summary="⑧ 查询订单详情")
async def get_order(order_id: str, user: Dict[str, Any] = Depends(auth_service.current_user)):
    """查询订单详情（对接甲方 POS 系统；未接入时返回 Mock 数据）"""
    detail = await get_order_detail(order_id)
    return {"order": detail, "tenant_id": user["tenant_id"]}


@router.get("/orders", summary="⑨ 订单列表")
async def list_orders(limit: int = 20, user: Dict[str, Any] = Depends(auth_service.current_user)):
    """查询待处理订单列表（对接 POS 系统）"""
    orders = await list_pending_orders(user["tenant_id"], limit=limit)
    return {"orders": orders, "total": len(orders)}


# ==================================================================
# 四、后厨接口（2 个）
# ==================================================================

@router.get("/kitchen/pending", summary="⑩ 待处理订单（后厨）")
async def kitchen_pending(user: Dict[str, Any] = Depends(auth_service.current_user)):
    """后厨待处理订单列表（对应需求文档 4.1「后厨主管端」面板）"""
    orders = await list_pending_orders(user["tenant_id"], limit=20)
    return {"orders": orders, "total": len(orders)}


@router.post("/kitchen/schedule", summary="⑪ 生成排产方案")
async def kitchen_schedule(human_confirmed: bool = False,
                           priority_adjustment: Optional[str] = None,
                           user: Dict[str, Any] = Depends(auth_service.current_user)):
    """
    触发 KitchenAgent 生成后厨排产方案。
    高峰期/超阈值时返回 need_human_confirm=True，需主管确认后再次调用（human_confirmed=true）。
    主管若要调整顺序，可传 priority_adjustment（如「把 B12 提到最前」），
    图会回到 schedule_tasks 重新排产。
    """
    state = initial_state(
        tenant_id=user["tenant_id"], user_id=user["user_id"], role=user["role"],
        user_input="帮我安排后厨出餐顺序",
        human_confirmed=human_confirmed,
        priority_adjustment=priority_adjustment or "",
    )
    result = await kitchen_agent.ainvoke(state, thread_id=f"{user['tenant_id']}_kitchen")
    return kitchen_agent.format_result(result)


# ==================================================================
# 五、评价接口（2 个）
# ==================================================================

@router.post("/reviews/analyze", summary="⑫ 评价分析")
async def review_analyze(user: Dict[str, Any] = Depends(auth_service.current_user)):
    """触发 ReviewAgent 分析多平台评价（需求量较大，前端应展示加载态）"""
    state = initial_state(
        tenant_id=user["tenant_id"], user_id=user["user_id"], role=user["role"],
        user_input="帮我分析一下本周的顾客评价",
    )
    result = await review_agent.ainvoke(state, thread_id=f"{user['tenant_id']}_review")
    return review_agent.format_result(result)


@router.get("/reviews/history", summary="⑬ 评价分析历史")
async def review_history(limit: int = 50, user: Dict[str, Any] = Depends(auth_service.current_user)):
    """查询历史评价分析记录（按 tenant_id 隔离）"""
    store = await get_store()
    records = await store.list_review_analysis(user["tenant_id"], limit=limit)
    return {"records": records, "total": len(records)}


# ==================================================================
# 六、记忆接口（3 个）
# ==================================================================

@router.get("/memory/preferences", summary="⑭ 查询门店偏好")
async def get_preferences(user: Dict[str, Any] = Depends(auth_service.current_user)):
    """查询门店偏好记忆（按 tenant_id 隔离）"""
    store = await get_store()
    prefs = await store.load_store_preferences(user["tenant_id"])
    return {"preferences": prefs, "tenant_id": user["tenant_id"]}


@router.put("/memory/preferences", response_model=SimpleResponse, summary="⑮ 更新门店偏好")
async def update_preferences(req: StorePreferenceRequest,
                             user: Dict[str, Any] = Depends(auth_service.current_user)):
    """更新门店偏好记忆"""
    store = await get_store()
    await store.save_store_preferences(user["tenant_id"], {
        "popular_dishes": req.popular_dishes,
        "peak_hours": req.peak_hours,
        "avg_order_value": req.avg_order_value,
    })
    return SimpleResponse(message="门店偏好已更新")


@router.get("/memory/customer/{customer_id}", summary="⑯ 查询顾客画像")
async def get_customer(customer_id: str, user: Dict[str, Any] = Depends(auth_service.current_user)):
    """查询顾客画像（按 tenant_id + user_id 双层隔离）"""
    store = await get_store()
    profile = await store.load_customer_profile(user["tenant_id"], customer_id, user["user_id"])
    return {"customer": profile or {}, "found": profile is not None}


@router.put("/memory/customer/{customer_id}", response_model=SimpleResponse, summary="⑰ 更新顾客画像")
async def update_customer(customer_id: str, req: MemoryUpdateRequest,
                          user: Dict[str, Any] = Depends(auth_service.current_user)):
    """更新顾客画像（饮食限制 / 过敏原 / 偏好菜品）"""
    store = await get_store()
    await store.upsert_customer_profile(user["tenant_id"], customer_id, {
        "dietary_restrictions": req.dietary_restrictions,
        "allergens": req.allergens,
        "favorite_dishes": req.favorite_dishes,
        "member_level": req.member_level,
    }, user["user_id"])
    return SimpleResponse(message=f"顾客 {customer_id} 画像已更新")


# ==================================================================
# 七、元信息接口（⑱ + 辅助）
# ==================================================================

@router.get("/agents", summary="⑱ Agent 清单与结构")
async def list_agents():
    """返回 4 个 Agent 的节点、边结构（对应需求文档 3.3）"""
    agents = [order_agent.info(), kitchen_agent.info(), review_agent.info(),
              safety_agent.info()]
    total_nodes = sum(a["node_count"] for a in agents)
    return {"agents": agents, "total_agents": len(agents), "total_nodes": total_nodes}


@router.get("/tools", summary="工具清单（15 个）")
async def list_tools():
    """返回 15 个工具的分类与归属 Agent（对应需求文档 3.4）"""
    return {"tools": tool_registry.describe(), "counts": tool_registry.count(),
            "total": sum(tool_registry.count().values())}


@router.get("/intents", summary="意图清单（9 类）")
async def list_intents():
    """返回 9 类餐饮意图与路由目标（前 7 类对应需求文档 3.5，后 2 类为 SafetyAgent 新增）"""
    return {"intents": [
        {"intent": k, "agent": v["agent"], "complex": v["complex"], "description": v["desc"]}
        for k, v in config.INTENT_ROUTES.items()
    ], "total": len(config.INTENT_ROUTES)}


@router.get("/config", summary="前端配置（快捷问题/角色/集成状态）")
async def get_config():
    """返回前端所需的配置：快捷问题（通用 + 按角色）、角色引导。

    快捷问题分两套：
        · quick_questions        —— 通用固定列表（无角色信息时兜底，保持向后兼容）
        · quick_by_role          —— 按角色 key 索引的完整自然句，前端据 user.role 选取
    """
    return {
        "quick_questions": Prompts.quick_questions(),
        "quick_by_role": Prompts.quick_questions_by_role(),
        "role_hints": Prompts.role_hints(),
        "service_phone": config.SERVICE_PHONE,
        "default_top_k": config.DEFAULT_RECOMMEND_TOP_K,
    }


@router.get("/health", summary="健康检查（含依赖状态）")
async def health():
    """
    健康检查：返回配置自检、各依赖（LLM/PG/Milvus）状态。
    用于运维监控与前端降级提示。

    LLM 段额外暴露主/备模型切换的观测字段：
        primary_model     主模型名
        fallback_enabled  备用模型是否已启用
        fallback_model    备用模型名（未配置为空串）
        active_model      当前生效模型（未切换 = 主模型）
        using_fallback    当前是否已切换到备用模型
    api_key 一律经 mask_key() 脱敏，绝不输出明文密钥。
    """
    from tools.llm_client import llm_client

    return {
        "status": "healthy",
        "service": "FoodAgent AI 智能餐饮 Agent 系统",
        "config": config.validate(),
        "dependencies": {
            "llm": {"available": llm_client.available, "model": llm_client.model,
                    "primary_model": llm_client.primary_model,
                    "fallback_enabled": llm_client.fallback_enabled,
                    "fallback_model": llm_client.fallback_model,
                    "active_model": llm_client.active_model,
                    "using_fallback": llm_client.using_fallback,
                    "api_key": type(llm_client).mask_key(config.LLM_API_KEY)},
            "checkpointer": _checkpointer_type(),
        },
    }


@router.get("/endpoints", summary="接口清单（18 个）")
async def list_endpoints():
    """列出全部业务接口（自文档化）"""
    return {"endpoints": ENDPOINTS, "total": len(ENDPOINTS)}


def _checkpointer_type() -> str:
    """返回 Checkpointer 类型（惰性导入，避免启动期报错）"""
    try:
        from memory.checkpointer import CheckpointerHolder
        cp = CheckpointerHolder.get()
        return type(cp).__name__ if cp else "none"
    except Exception as exc:  # noqa: BLE001
        return f"error: {exc}"


# ==================================================================
# 接口清单（18 个业务接口）
# ==================================================================
ENDPOINTS = [
    {"method": "POST", "path": "/api/auth/login", "desc": "① 登录"},
    {"method": "GET", "path": "/api/auth/me", "desc": "② 当前用户信息"},
    {"method": "POST", "path": "/api/tasks", "desc": "③ 创建任务"},
    {"method": "POST", "path": "/api/chat", "desc": "④ 对话（非流式）"},
    {"method": "GET", "path": "/api/chat/stream", "desc": "⑤ 对话（SSE 流式）"},
    {"method": "DELETE", "path": "/api/chat/history/{thread_id}", "desc": "⑥ 清除任务历史"},
    {"method": "POST", "path": "/api/orders", "desc": "⑦ 创建订单"},
    {"method": "GET", "path": "/api/orders/{order_id}", "desc": "⑧ 查询订单详情"},
    {"method": "GET", "path": "/api/orders", "desc": "⑨ 订单列表"},
    {"method": "GET", "path": "/api/kitchen/pending", "desc": "⑩ 待处理订单（后厨）"},
    {"method": "POST", "path": "/api/kitchen/schedule", "desc": "⑪ 生成排产方案"},
    {"method": "POST", "path": "/api/reviews/analyze", "desc": "⑫ 评价分析"},
    {"method": "GET", "path": "/api/reviews/history", "desc": "⑬ 评价分析历史"},
    {"method": "GET", "path": "/api/memory/preferences", "desc": "⑭ 查询门店偏好"},
    {"method": "PUT", "path": "/api/memory/preferences", "desc": "⑮ 更新门店偏好"},
    {"method": "GET", "path": "/api/memory/customer/{customer_id}", "desc": "⑯ 查询顾客画像"},
    {"method": "PUT", "path": "/api/memory/customer/{customer_id}", "desc": "⑰ 更新顾客画像"},
    {"method": "GET", "path": "/api/agents", "desc": "⑱ Agent 清单与结构"},
]
