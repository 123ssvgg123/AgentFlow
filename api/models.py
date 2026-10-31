"""
    请求/响应模型：api/models.py

    职责：定义 RESTful 接口的 Pydantic 请求体与响应体，提供入参校验与 OpenAPI 文档。
"""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ==================================================================
# 认证相关
# ==================================================================

class LoginRequest(BaseModel):
    """登录请求"""
    username: str = Field(..., description="门店工号", examples=["waiter_01"])
    password: str = Field(..., description="密码", examples=["123456"])


class LoginResponse(BaseModel):
    """登录响应"""
    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(..., description="有效期（秒）")
    user: Dict[str, Any] = Field(..., description="用户信息（tenant_id/user_id/role/name）")


class UserInfo(BaseModel):
    """当前用户信息"""
    tenant_id: str
    user_id: str
    role: str
    name: str = ""


# ==================================================================
# 对话相关
# ==================================================================

class ChatRequest(BaseModel):
    """对话请求（核心接口）"""
    query: str = Field(..., description="用户输入的自然语言指令")
    thread_id: Optional[str] = Field(None, description="任务 ID（不传则自动生成，用于多轮记忆）")
    customer_id: Optional[str] = Field(None, description="顾客 ID（用于加载顾客画像）")
    tenant_id: Optional[str] = Field(None, description="门店 ID（不传则取 Token 中的）")
    human_confirmed: bool = Field(False, description="后厨主管是否已确认排产方案")
    manager_confirmed: bool = Field(False, description="店长是否已确认差评回复")
    manager_request_revise: bool = Field(False, description="店长要求重写差评回复（回到 generate_report）")
    priority_adjustment: Optional[str] = Field(None, description="主管对排产优先级的调整意见，如「把 B12 提到最前」")
    allergen_notice_confirmed: bool = Field(
        False, description="服务员是否已完成过敏原口头告知并获顾客确认（用于合规留痕）")
    notice_channel: Optional[str] = Field(
        "verbal", description="告知渠道：verbal（口头）/ sms / system")


class ChatResponse(BaseModel):
    """对话响应"""
    answer: str
    thread_id: str
    tenant_id: str
    intents: List[str] = []
    primary_intent: Optional[str] = None
    intent_reason: str = Field("", description="意图识别理由（LLM 分类或规则降级）")
    is_pipeline: bool = False
    agents_invoked: List[str] = []
    agent_results: Dict[str, Any] = {}
    order_id: str = ""
    allergen_risk: str = "none"
    allergen_result: Dict[str, Any] = {}
    pending_actions: List[Dict[str, Any]] = []
    # 修复（二次审计）：Orchestrator 的 _aggregate 会返回这两个字段，但原模型没有声明，
    # routes 又按 model_fields 过滤，导致「Pipeline 中途待确认的阶段」「需人工复核」
    # 两个信息在 HTTP 响应里被静默丢弃——前端永远看不到流程卡在哪一步。
    pipeline_pending_stages: List[Dict[str, Any]] = []
    needs_manual_review: bool = False
    degraded_tools: List[str] = []
    errors: List[str] = []
    processing_time: float = 0.0


# ==================================================================
# 订单相关
# ==================================================================

class OrderCreateRequest(BaseModel):
    """下单请求（直接下单，不经过对话）"""
    dishes: List[Dict[str, Any]] = Field(..., description="菜品列表 [{dish_name, quantity}]")
    guest_count: int = Field(0, description="用餐人数")
    table_no: str = Field("", description="桌号")
    allergens: List[str] = Field(default_factory=list, description="顾客声明的过敏原")
    member_level: str = Field("普通", description="会员等级")


class OrderDetail(BaseModel):
    """订单详情"""
    订单号: str = ""
    门店名称: str = ""
    桌号: str = ""
    人数: int = 0
    状态: str = ""
    菜品列表: List[Dict[str, Any]] = []
    总金额: str = ""
    特殊要求: str = ""


# ==================================================================
# 工具与 Agent 元信息
# ==================================================================

class ToolInfo(BaseModel):
    """工具信息"""
    name: str
    category: str
    category_name: str
    agent: str
    description: str


class AgentInfo(BaseModel):
    """Agent 信息"""
    name: str
    node_count: int
    nodes: List[str]
    edges: List[str] = []
    conditional_edges: List[str] = []
    engine: str


# ==================================================================
# 记忆相关
# ==================================================================

class MemoryUpdateRequest(BaseModel):
    """记忆更新请求"""
    customer_id: str
    dietary_restrictions: List[str] = Field(default_factory=list)
    allergens: List[str] = Field(default_factory=list)
    favorite_dishes: List[str] = Field(default_factory=list)
    member_level: str = "普通"


class StorePreferenceRequest(BaseModel):
    """门店偏好更新请求"""
    popular_dishes: List[str] = Field(default_factory=list)
    peak_hours: List[str] = Field(default_factory=list)
    avg_order_value: float = 0.0


# ==================================================================
# 通用响应
# ==================================================================

class SimpleResponse(BaseModel):
    """通用简单响应"""
    status: str = "success"
    message: str = ""
    data: Optional[Any] = None
