"""
    统一异常与降级工具：base/exceptions.py

    职责：定义全项目复用的异常类型与「三级降级」装饰器/辅助函数，
        对应需求文档 3.4.1 ④「工具调用的容错与降级策略」：

            外部 API 封装：① 重试 1 次 → ② 切换备用数据源 → ③ 返回友好提示
            LLM 工具     ：① 主模型不可用切备用模式 → ② 超时降级为规则引擎
            MCP 工具     ：① 检索失败降级关键词搜索 → ② 返回缓存结果
            规则引擎     ：不失败，但输入缺失时返回默认值并标记「数据缺失」

    所有降级都必须「可见」——通过 logger.warning 留痕，禁止静默吞异常。
"""
# 导入异步工具
import asyncio
# 导入函数式工具
import functools
# 导入类型标注
from typing import Any, Callable, Optional

# 导入日志
from base.logger import logger


class FoodAgentError(Exception):
    """项目异常基类：所有自定义异常的父类"""

    def __init__(self, message: str, *, tool: Optional[str] = None, detail: Optional[str] = None):
        self.message = message
        self.tool = tool
        self.detail = detail
        super().__init__(message)

    def to_dict(self) -> dict:
        """转成结构化字典，便于 API / 前端统一消费"""
        return {
            'error': True,
            'message': self.message,
            'tool': self.tool,
            'detail': self.detail,
        }


class ToolExecutionError(FoodAgentError):
    """工具执行失败（外部 API / MCP / 规则引擎的业务异常）"""


class LLMUnavailableError(FoodAgentError):
    """LLM 不可用：密钥缺失、超时、配额耗尽等。触发规则引擎降级。

    retryable 标记「再试还有没有意义」：
        · True （默认）网络抖动、超时、429 限流 —— 可以重试/切备用模型
        · False 账户级不可用（余额不足 402 / 鉴权失败 401、403）——
          重试和切备用模型都必然失败，只会白刷请求与日志，调用方应直接降级
    """

    def __init__(self, message: str, *, tool: Optional[str] = None,
                 detail: Optional[str] = None, retryable: bool = True):
        self.retryable = retryable
        super().__init__(message, tool=tool, detail=detail)


class DataMissingError(FoodAgentError):
    """数据缺失：输入不完整（如菜品未标注配方），返回默认值并标记"""


def degrade_result(reason: str, fallback: Any, *, tool: Optional[str] = None, degraded: bool = True) -> dict:
    """
    构造标准化的「降级结果」返回体。

    约定：所有工具返回值统一包含 `degraded` 字段，
        为 True 时表示该结果来自降级路径，前端/Agent 可据此提示用户。
    """
    logger.warning(f"[降级] tool={tool} reason={reason}")
    if isinstance(fallback, dict):
        result = dict(fallback)
        result.setdefault('degraded', degraded)
        result.setdefault('degrade_reason', reason)
        if tool:
            result.setdefault('tool', tool)
        return result
    return {
        'degraded': degraded,
        'degrade_reason': reason,
        'tool': tool,
        'value': fallback,
    }


def with_retry(max_retries: int = 1, delay: float = 0.5,
               fallback: Any = None, tool_name: Optional[str] = None):
    """
    异步重试装饰器（对应「重试 1 次 → 切换备用 → 友好提示」的前两级）。

    用法：
        @with_retry(max_retries=1, fallback={...}, tool_name="get_order_detail")
        async def get_order_detail(order_id): ...
    """

    def decorator(func: Callable):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            last_exc: Optional[Exception] = None
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001 - 需要兜住所有异常以走降级
                    last_exc = exc
                    if attempt < max_retries:
                        logger.warning(
                            f"[重试] tool={tool_name or func.__name__} "
                            f"第 {attempt + 1}/{max_retries} 次失败: {exc}，准备重试"
                        )
                        await asyncio.sleep(delay)
            # 重试耗尽 → 走降级分支
            if fallback is not None:
                return degrade_result(
                    reason=f"重试 {max_retries} 次后仍失败: {last_exc}",
                    fallback=fallback,
                    tool=tool_name or func.__name__,
                )
            raise ToolExecutionError(
                f"工具 {tool_name or func.__name__} 执行失败",
                tool=tool_name or func.__name__,
                detail=str(last_exc),
            )

        return wrapper

    return decorator


def safe_default(value: Any, field: str, *, tool: Optional[str] = None) -> Any:
    """
    规则引擎专用：输入字段缺失时返回默认值 + 打上「数据缺失」标记。
    规则引擎本身不会失败，但输入不完整时必须显式标记而非猜测。
    """
    logger.warning(f"[数据缺失] tool={tool} field={field} 使用默认值 {value!r}")
    return value
