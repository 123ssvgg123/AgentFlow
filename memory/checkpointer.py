"""
    LangGraph Checkpointer 工厂：memory/checkpointer.py

    职责：创建 LangGraph 的状态持久化器（Checkpointer），对应需求文档 3.6 ②。

    Checkpointer 的作用（需求 3.6 ②）：
        · 按 thread_id 隔离 State——不同 thread 的对话历史与中间状态完全独立
        · 多轮对话记忆——同一 thread_id 内的后续消息能记住前面的上下文
        · 断点续传——Human-in-the-Loop 场景，后厨主管确认优先级后从断点继续

    实现选择：
        PostgresSaver —— 生产实现（langgraph.checkpoint.postgres），需 PG 与 langgraph
        MemorySaver   —— 降级实现（内存），零依赖，用于开发自测
        若 langgraph 未安装，则返回 None，由 Agent 层走「无 Checkpointer」的纯函数模式

    连接管理说明（重要）：
        langgraph-checkpoint-postgres 的 PostgresSaver.from_conn_string() 是 **上下文管理器**，
        直接 `saver = PostgresSaver.from_conn_string(dsn)` 拿到的是 _GeneratorContextManager，
        再调 saver.setup() 必定 AttributeError，结果就是「开了 PG 却仍静默降级为 MemorySaver」。
        因此这里改用 psycopg_pool.ConnectionPool 构造（PostgresSaver 官方支持传池），
        既避开上下文管理器的生命周期问题，又复用连接、不必每次请求重连。
"""
import uuid
from typing import Any, Optional

from base.config import config
from base.logger import logger

# 模块级连接池（PostgresSaver 与它共享生命周期，供 close_checkpointer 统一释放）
_POOL: Optional[Any] = None


def build_thread_id(tenant_id: str, user_id: str = "", task_id: Optional[str] = None) -> str:
    """
    生成 thread_id（需求 3.6 ④ 读写策略中的命名约定）。

    格式：{tenant_id}_{user_id}_{task_id}
    作用：Checkpointer 按此 ID 隔离 State，天然实现门店 + 员工级隔离。
    """
    task = task_id or uuid.uuid4().hex[:12]
    return f"{tenant_id}_{user_id or 'anon'}_{task}"


def _build_postgres_saver():
    """
    用连接池构造 PostgresSaver（模块内单例池，避免重复建池与重复 TCP 握手）。

    三个连接参数都必须显式指定，否则会踩坑：
        autocommit=True       —— setup() 要执行 CREATE TABLE/INDEX，必须在自动提交事务下运行
        prepare_threshold=0   —— 关闭预编译语句缓存，兼容 PgBouncer 等连接池中间件
        row_factory=dict_row  —— Checkpointer 内部按字典取值
    """
    global _POOL

    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    if _POOL is None:
        _POOL = ConnectionPool(
            conninfo=config.PG_DSN,
            min_size=1,
            max_size=10,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
            open=True,
            timeout=10,
        )
        logger.info(f"[Checkpointer] 已建立 PG 连接池 @ {config.PG_HOST}:{config.PG_PORT}"
                    f"/{config.PG_DATABASE}（min=1, max=10）")

    saver = PostgresSaver(_POOL)
    # setup() 内部全是 CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS，可重复调用。
    # 这里不吞异常：建表失败说明 PG 不可用，交由上层降级。
    saver.setup()
    return saver


def close_checkpointer() -> None:
    """关闭 Checkpointer 连接池（服务 shutdown 时调用；幂等，可重复执行）"""
    global _POOL
    if _POOL is not None:
        try:
            _POOL.close()
            logger.info("[Checkpointer] PG 连接池已关闭")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[Checkpointer] 关闭连接池时出错（忽略）: {exc}")
        finally:
            _POOL = None
    CheckpointerHolder._instance = None
    CheckpointerHolder._initialized = False


def get_checkpointer():
    """
    创建并返回 Checkpointer 实例。

    优先级：
        1. PostgreSQL（config.PG_ENABLED=true 且 langgraph 已安装）→ PostgresSaver
        2. 内存（langgraph 已安装但无 PG）→ MemorySaver
        3. 无（langgraph 未安装）→ None（Agent 层降级为无状态执行）
    """
    # 尝试导入 langgraph 的 checkpointer
    try:
        from langgraph.checkpoint.memory import MemorySaver
    except ImportError:
        logger.warning(
            "[Checkpointer] 未安装 langgraph，状态持久化不可用。"
            "Agent 将降级为无状态执行（多轮记忆由 memory/store.py 承担）。"
            "安装：pip install langgraph langgraph-checkpoint-postgres psycopg-pool"
        )
        return None

    # 优先 PostgreSQL
    if config.PG_ENABLED:
        try:
            saver = _build_postgres_saver()
            logger.info("[Checkpointer] 使用 PostgresSaver（按 thread_id 隔离，持久化到 PostgreSQL）")
            return saver
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[Checkpointer] PostgresSaver 初始化失败（{exc}），降级为 MemorySaver")

    # 降级：内存 Checkpointer
    logger.info("[Checkpointer] 使用 MemorySaver（内存态，重启后丢失；生产环境请启用 PostgreSQL）")
    return MemorySaver()


class CheckpointerHolder:
    """
    Checkpointer 单例持有者。
    避免每次请求重复创建（尤其是 PostgresSaver 的连接开销）。
    """

    _instance: Optional[Any] = None
    _initialized = False

    @classmethod
    def get(cls):
        if not cls._initialized:
            cls._instance = get_checkpointer()
            cls._initialized = True
        return cls._instance

    @classmethod
    def available(cls) -> bool:
        return cls.get() is not None


if __name__ == '__main__':
    print("=" * 60)
    print("Checkpointer 自检")
    print("=" * 60)
    print("PG_ENABLED:", config.PG_ENABLED)
    cp = CheckpointerHolder.get()
    print("Checkpointer 类型：", type(cp).__name__ if cp else "None（langgraph 未安装）")
    print("thread_id 示例：", build_thread_id("store_001", "waiter_001"))
