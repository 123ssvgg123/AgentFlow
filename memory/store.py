"""
    长期记忆仓库：memory/store.py

    职责：实现门店级长期记忆的读写，对应需求文档 3.6「长期记忆」与「读写策略」。

    双层隔离（需求 3.6 ③）：
        所有查询强制带 tenant_id（门店 ID）
        员工级数据（顾客画像 / 评价记录）额外带 user_id
        → 即使是同一供应商的食材数据在不同门店也完全隔离

    双实现策略（保证「有 PG 更强，无 PG 也能跑」）：
        PostgresStore —— 生产实现，psycopg 连接 PostgreSQL，功能完整
        FileStore     —— 降级实现，JSON 文件持久化，零外部依赖

    read/write 时机（需求 3.6 ④）：
        read  ：每次任务启动时，加载门店偏好 + 顾客画像注入 LangGraph State
        write ：节点执行后 Checkpointer 自动保存；任务结束后异步写回长期记忆
"""
# 导入异步与数据工具
import asyncio
import json
import logging
import os
import threading
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from base.config import config
from base.logger import logger
from memory.order_state import STATUS_CREATED, STATUS_MANUAL_REVIEW


# ==================================================================
# 并发写保护（进程内门店级锁）
# 说明：MemorySaver/FileStore 场景下同一进程并发处理多桌订单时，
#       「读-合-写」会互相覆盖（后写的丢掉先写的）。这里给每个门店
#       一把 asyncio.Lock，保证追加类写操作串行化。
# ==================================================================
_WRITE_LOCKS: Dict[str, "asyncio.Lock"] = {}


def _get_write_lock(key: str) -> "asyncio.Lock":
    lock = _WRITE_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _WRITE_LOCKS[key] = lock
    return lock


def _merge_preferences(current: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """
    合并门店偏好（追加语义）：
        popular_dishes —— 去重追加，保留最近 10 项（新菜在前）
        avg_order_value —— 直接覆盖（最新值）
        其余键 —— 覆盖
    """
    merged = dict(current or {})
    for key, value in (patch or {}).items():
        if key == "popular_dishes" and isinstance(value, list):
            existing = merged.get("popular_dishes") or []
            seen = list(dict.fromkeys([*value, *existing]))  # 新值优先，去重
            merged["popular_dishes"] = seen[:10]
        else:
            merged[key] = value
    return merged


_MERGE_USAGE_HELPERS = None


# ==================================================================
# 记忆清理：各数据域的默认保留天数与时间列映射（需求 3.6.5）
# ==================================================================

def default_retention() -> Dict[str, int]:
    """
    各数据域的默认保留天数（唯一来源：base/config.py）。

    约定：值为 0 表示「永久保留，不清理」。
        conversation      对话记忆（LangGraph Checkpointer）：90 天归档
        review_analysis   评价分析：730 天（2 年）
        order_status_log  订单状态流转日志：90 天（同对话归档口径）
        compliance_log    合规留痕：COMPLIANCE_LOG_YEARS = 3 年
        store_preferences 门店偏好：0（永久）
        customer_profiles 顾客画像：0（永久）
    """
    return {
        "conversation": config.MEMORY_RETENTION_CONVERSATION_DAYS,
        "review_analysis": config.MEMORY_RETENTION_REVIEW_DAYS,
        "order_status_log": config.MEMORY_RETENTION_ORDER_LOG_DAYS,
        "compliance_log": config.COMPLIANCE_LOG_YEARS,
        "store_preferences": config.MEMORY_RETENTION_STORE_PREF_DAYS,
        "customer_profiles": config.MEMORY_RETENTION_CUSTOMER_DAYS,
    }


# 入口参数使用的键 -> 单位（"days" 天 / "years" 年 / "permanent" 永久）
# 说明：compliance_log 的保留按「年」计（需求：留痕 3 年备查），故单独用 years。
RETENTION_UNITS: Dict[str, str] = {
    "conversation": "days",
    "review_analysis": "days",
    "order_status_log": "days",
    "compliance_log": "years",
    "store_preferences": "permanent",
    "customer_profiles": "permanent",
}

# 永久保留的数据域（显式列出，purge_expired 内会跳过并说明原因）
PERMANENT_DOMAINS = ("store_preferences", "customer_profiles")

# 各数据域的时间列（PostgresStore 生成 SQL 用；FileStore 用同名键兜底）
# 说明：已核对 memory/schemas.py 的 DDL ——
#       review_analysis.review_date（DATE）、order_status_log.created_at（TIMESTAMP）
#       均已存在，无需补列（compliance_log 的 created_at 亦已存在）。
# 注意：compliance_log 按「年」保留（见 RETENTION_UNITS），不走本表的「按天」循环，
#       故这里**不含** compliance_log——否则同一数据域会被处理两次并覆盖统计结果。
TIME_COLUMNS: Dict[str, str] = {
    "review_analysis": "review_date",
    "order_status_log": "created_at",
}

# FileStore 取值时依次尝试的时间字段（前者缺失才用后者）：
#   review_analysis —— 优先 review_date（业务发生日），无则退回落库时间 created_at
#   order_status_log / compliance_log —— created_at
FILE_TIME_KEYS: Dict[str, Tuple[str, ...]] = {
    "review_analysis": ("review_date", "created_at", "updated_at"),
    "order_status_log": ("created_at",),
    "compliance_log": ("created_at", "confirmed_at", "noticed_at"),
}


def _parse_ts(value: Any) -> Optional[datetime]:
    """
    尽力解析时间戳（清理判断只关心「是否早于截止线」）。

    支持：
        None / 空串          → None（无法判断）
        datetime / date      → 直接使用（date 补 00:00:00）
        数字（int/float）     → 按 Unix 时间戳解析
        字符串               → ISO 8601 优先，其余常见格式兜底

    返回 None 表示**解析失败**——调用方必须保守跳过该条（不删），并打 warning。
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value))
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    # ISO 8601（本仓库 FileStore 写入的统一格式：2026-10-14T10:14:08.123456）
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00").replace("/", "-"))
    except ValueError:
        pass
    # 常见日期格式兜底
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


class BaseStore:
    """长期记忆仓库接口（抽象基类）"""

    # 子类是否需要 base_dir（落盘目录）。
    # FileStore 置 True；PostgresStore 走 SQL 不需要目录——
    # 原实现在 purge_expired 里无条件校验 base_dir，导致启用 PG 后
    # 记忆清理直接抛 NotImplementedError（"未配置记忆存储目录"），此处按实现区分。
    REQUIRES_BASE_DIR = False

    async def init(self) -> None:
        """初始化（建表 / 建目录）"""

    async def load_store_preferences(self, tenant_id: str) -> Dict[str, Any]:
        """读取门店偏好"""
        raise NotImplementedError

    async def save_store_preferences(self, tenant_id: str, data: Dict[str, Any]) -> None:
        """写入门店偏好"""
        raise NotImplementedError

    async def append_store_preferences(self, tenant_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        """
        原子追加门店偏好（并发安全）：
            读取当前值 → 合并 patch → 写回，整个过程在同一把锁/事务内完成。
        默认实现为「读-合-写」，子类应覆盖为真正的原子操作。
        """
        async with self._lock(tenant_id):
            current = await self.load_store_preferences(tenant_id) or {}
            merged = _merge_preferences(current, patch)
            await self.save_store_preferences(tenant_id, merged)
            return merged

    def _lock(self, tenant_id: str):
        """获取门店级写锁（进程内，防止同一门店并发写覆盖）"""
        raise NotImplementedError

    async def load_customer_profile(self, tenant_id: str, customer_id: str,
                                    user_id: str = "") -> Optional[Dict[str, Any]]:
        """读取顾客画像"""
        raise NotImplementedError

    async def upsert_customer_profile(self, tenant_id: str, customer_id: str,
                                      data: Dict[str, Any], user_id: str = "") -> None:
        """写入/更新顾客画像"""
        raise NotImplementedError

    async def save_review_analysis(self, tenant_id: str, items: List[Dict[str, Any]],
                                   user_id: str = "") -> int:
        """批量保存评价分析记录"""
        raise NotImplementedError

    async def list_review_analysis(self, tenant_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        """查询评价分析记录"""
        raise NotImplementedError

    async def save_compliance_log(self, tenant_id: str, log: Dict[str, Any], user_id: str = "") -> None:
        """保存合规留痕（过敏原订单）"""
        raise NotImplementedError

    async def confirm_allergen_notice(self, tenant_id: str, order_id: str, *,
                                      confirmed_by: str = "", channel: str = "verbal",
                                      notice_content: str = "",
                                      customer_ack: bool = True,
                                      user_id: str = "") -> Dict[str, Any]:
        """
        记录一次「过敏原口头告知确认」（需求 3.8 Q8 留痕）。

        合规要求：检出过敏原后，服务员必须**口头告知顾客**并得到知悉确认，
        该动作必须可追溯（谁、何时、以什么措辞告知、顾客是否确认）。

        实现：以 (tenant_id, order_id) 为索引，对该订单最近一条合规记录做**就地更新**；
              若不存在则新建一条（保证「先告知后下单」的极端顺序也不会丢留痕）。
        返回更新/新建后的记录。
        """
        raise NotImplementedError

    async def list_compliance_log(self, tenant_id: str, order_id: str = "",
                                  limit: int = 100) -> List[Dict[str, Any]]:
        """查询合规留痕（可按订单号过滤，按时间倒序）"""
        raise NotImplementedError

    async def save_ingredient_usage(self, tenant_id: str, usage_date: date,
                                    usage: Dict[str, float], order_count: int = 0) -> None:
        """保存食材消耗记录"""
        raise NotImplementedError

    async def add_ingredient_usage(self, tenant_id: str, usage_date: date,
                                   usage: Dict[str, float], order_count: int = 0) -> None:
        """
        原子累加当日食材消耗（并发安全）：
            同一天多桌下发时，用量应累加而不是相互覆盖。
        默认实现为「读-合-写」，子类可覆盖为真正的原子操作。
        """
        async with self._lock(f"{tenant_id}:usage"):
            await self.save_ingredient_usage(tenant_id, usage_date, usage, order_count)

    # ==================== 订单状态机 ====================

    async def load_order_status(self, tenant_id: str, order_id: str) -> Optional[str]:
        """读取订单当前状态（无记录返回 None）"""
        raise NotImplementedError

    async def save_order_status_event(self, tenant_id: str, order_id: str, from_status: str,
                                      to_status: str, operator: str = "system",
                                      reason: str = "", user_id: str = "",
                                      meta: Optional[Dict[str, Any]] = None) -> None:
        """追加一条状态流转事件（不做合法性校验，校验由 transition_order_status 负责）"""
        raise NotImplementedError

    async def list_order_status_events(self, tenant_id: str, order_id: str) -> List[Dict[str, Any]]:
        """查询某订单的完整状态流转历史（按时间正序，供审计与回溯）"""
        raise NotImplementedError

    async def transition_order_status(self, tenant_id: str, order_id: str, to_status: str,
                                      operator: str = "system", reason: str = "",
                                      user_id: str = "",
                                      meta: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
        """
        原子状态跃迁（并发安全 + 合法性校验）：

            读当前状态 → 用 order_state.transition() 校验 → 追加流转事件

        非法跃迁一律拒绝、记录 warning 日志且不写库；
        返回 (是否成功, 说明信息)，说明信息格式见 memory/order_state.py。

        默认实现为「读-校验-写」，整体在订单级锁内完成。
        """
        from memory.order_state import transition as _transition

        async with self._lock(f"{tenant_id}:order:{order_id}"):
            current = await self.load_order_status(tenant_id, order_id)
            # 首次流转：无历史记录，视为从 None 进入（仅允许 created / manual_review）
            if current is None:
                if to_status == STATUS_CREATED:
                    await self.save_order_status_event(
                        tenant_id, order_id, "", to_status, operator, reason, user_id, meta)
                    return True, "ok"
                if to_status == STATUS_MANUAL_REVIEW:
                    await self.save_order_status_event(
                        tenant_id, order_id, "", to_status, operator, reason, user_id, meta)
                    return True, "ok"
                msg = f"illegal:None->{to_status}"
                logger.warning(f"[订单状态机] 拒绝：订单 {order_id} 无初始状态，"
                               f"不允许直接进入 {to_status}（门店 {tenant_id}）")
                return False, msg

            allowed, detail = _transition(current, to_status, order_id=order_id,
                                          reason=reason, logger=logger)
            if not allowed:
                return False, detail

            await self.save_order_status_event(
                tenant_id, order_id, current, to_status, operator, reason, user_id, meta)
            logger.info(f"[订单状态机] 门店 {tenant_id} 订单 {order_id}：{current} → {to_status}"
                        f"（{operator}）")
            return True, detail

    # ==================== 记忆清理/归档（需求 3.6.5） ====================

    async def purge_expired(self, retention: Optional[Dict[str, int]] = None,
                            tenant_id: str = "", dry_run: bool = False,
                            now: Optional[datetime] = None) -> Dict[str, int]:
        """
        按保留策略清理/归档过期长期记忆，返回每个数据域的清理条数。

        需求 3.6.5「记忆清理策略」：对话 90 天归档 / 门店偏好永久 /
        顾客画像永久 / 评价分析 2 年（合规留痕另按 3 年备查）。

        :param retention: 覆盖默认保留策略，键为数据域；不传则取 config 默认值。
                          单位约定（与 RETENTION_UNITS 对应）：
                              conversation / review_analysis / order_status_log → 单位「天」
                              compliance_log → 单位「年」（默认取 COMPLIANCE_LOG_YEARS = 3）
                          值为 0 表示「永久保留，不清理」。
        :param tenant_id: 仅清理指定门店；空串 = 该实现可覆盖的全部门店/全表。
        :param dry_run: 只统计不删除（供 scripts/purge_expired.py --dry-run 使用）。
        :param now: 截止线基准时间，默认取当前时间（注入以便测试）。
        :return: 每个数据域被清理（或 dry-run 下「将」被清理）的条数，
                 例如 {"review_analysis": 3, "order_status_log": 12, "compliance_log": 1}
        """
        # 仅文件实现依赖落盘目录；PG 实现走 SQL，不需要也不应校验 base_dir。
        if self.REQUIRES_BASE_DIR and getattr(self, "base_dir", None) is None:
            raise NotImplementedError(f"{type(self).__name__} 未配置记忆存储目录")

        retention = {**default_retention(), **(retention or {})}

        # 【永久保留】门店偏好 / 顾客画像：需求明确为「永久」，
        # 故无论外部传进来什么值（哪怕非 0）都强制置 0 跳过，绝不清理。
        for domain in PERMANENT_DOMAINS:
            if retention.get(domain):
                logger.warning(f"[记忆清理] {domain} 按需求为永久保留，"
                               f"忽略传入的保留值 {retention[domain]}")
            retention[domain] = 0

        threshold = now or datetime.now()
        purged: Dict[str, int] = {domain: 0 for domain in retention}
        # 日志兜底：base.py 的 logger 依赖 config / logging 初始化，
        # 这里保证调用方拿到的一定是可用的 logger（与模块内既有风格一致）。
        log = logger or logging.getLogger("FoodAgent")

        # 1) 合规留痕（需求：保留 3 年备查）—— 按「年」计算截止时间
        years = int(retention.get("compliance_log") or 0)
        if years > 0:
            cutoff = threshold - timedelta(days=int(365.25 * years))
            purged["compliance_log"] = await self._purge_compliance_log(
                cutoff, tenant_id, dry_run)
            log.info(f"[记忆清理] compliance_log 保留 {years} 年"
                     f"（截止 {cutoff.date()}）→ {'将' if dry_run else '已'}清理 "
                     f"{purged['compliance_log']} 条")

        # 2) 其余数据域按「天」计算截止时间
        for domain in TIME_COLUMNS:
            days = int(retention.get(domain) or 0)
            if days <= 0:
                log.info(f"[记忆清理] {domain} 配置为 0 → 永久保留，跳过")
                continue
            cutoff = threshold - timedelta(days=days)
            purged[domain] = await self._purge_domain(
                domain, days, cutoff, tenant_id, dry_run)
            log.info(f"[记忆清理] {domain} 保留 {days} 天"
                     f"（截止 {cutoff.date()}）→ {'将' if dry_run else '已'}清理 "
                     f"{purged[domain]} 条")

        # 3) 对话记忆归档说明：短期记忆由 LangGraph Checkpointer 管理，
        #    其表结构不在 memory/schemas.py 的 6 张业务表内，故这里只回填 0
        #    （保证返回值形状完整），在 base_agent/orchestrator 侧不做删除。
        purged["conversation"] = 0
        log.info(f"[记忆清理] conversation 归档口径 "
                 f"{retention.get('conversation') or 0} 天（Checkpointer 由 PG 维护，本方法返回 0）")

        # 4) 永久保留域固定返回 0
        for domain in PERMANENT_DOMAINS:
            purged[domain] = 0
        return purged

    async def _purge_domain(self, domain: str, days: int, cutoff: datetime,
                            tenant_id: str, dry_run: bool) -> int:
        """清理一个「按天保留」的数据域（子类覆盖实现真正的删除）"""
        raise NotImplementedError

    async def _purge_compliance_log(self, cutoff: datetime, tenant_id: str,
                                    dry_run: bool) -> int:
        """清理过期的合规留痕（子类覆盖实现真正的删除）"""
        raise NotImplementedError


# ==================================================================
# 实现一：PostgreSQL 仓库（生产实现）
# ==================================================================

class PostgresStore(BaseStore):
    """
    PostgreSQL 长期记忆仓库。

    连接管理（本轮改造）：
        原实现每次 _execute 都 psycopg.connect() 新建一条 TCP 连接 + 一次认证，
        一次点餐请求会触发十几次 _execute，连接开销远大于 SQL 本身。
        改为 psycopg_pool.ConnectionPool（min=1 / max=10），连接复用；
        池在首次使用时懒加载，PG 不可用即抛错，由 get_store() 捕获后整体降级 FileStore。
    """

    def __init__(self, dsn: Optional[str] = None):
        self.dsn = dsn or config.PG_DSN
        self._locks: Dict[str, "asyncio.Lock"] = {}
        self._pool: Any = None
        self._pool_guard = threading.Lock()

    def _lock(self, tenant_id: str):
        return _get_write_lock(tenant_id)

    def _get_pool(self):
        """懒加载连接池（首次调用即验证连通性：open(wait=True) 连不上直接抛错）"""
        if self._pool is not None:
            return self._pool
        with self._pool_guard:
            if self._pool is None:
                from psycopg_pool import ConnectionPool

                pool = ConnectionPool(conninfo=self.dsn, min_size=1, max_size=10, open=False)
                try:
                    pool.open(wait=True, timeout=10)
                except Exception:
                    pool.close()
                    raise
                self._pool = pool
                logger.info(f"[PG] 连接池就绪 @ {config.PG_HOST}:{config.PG_PORT}"
                            f"/{config.PG_DATABASE}（min=1, max=10）")
        return self._pool

    def close(self) -> None:
        """释放连接池（服务 shutdown 调用；幂等）"""
        pool, self._pool = self._pool, None
        if pool is not None:
            try:
                pool.close()
                logger.info("[PG] 长期记忆连接池已关闭")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[PG] 关闭连接池出错（忽略）: {exc}")

    async def init(self) -> None:
        """建表（幂等；单条 DDL 失败不阻断其余表，便于存量库平滑升级）"""
        from memory.schemas import ALL_DDL

        def _create():
            failed = 0
            pool = self._get_pool()
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    for ddl in ALL_DDL:
                        try:
                            cur.execute(ddl)
                            conn.commit()
                        except Exception as exc:  # noqa: BLE001
                            # 典型场景：存量库已有重复数据 → 唯一索引建不上，
                            # 此时只告警不中断（UPSERT 会退化为普通 INSERT，功能仍可用）
                            conn.rollback()
                            failed += 1
                            logger.warning(f"[PG] 建表/建索引语句执行失败（已跳过）: {exc}")
            return failed

        import asyncio

        failed = await asyncio.to_thread(_create)
        suffix = f"，{failed} 条语句失败（详见 warning）" if failed else ""
        logger.info(f"[PG] 长期记忆表初始化完成 @ {config.PG_HOST}:{config.PG_PORT}{suffix}")

    async def _execute(self, sql: str, params: tuple = (), fetch: bool = False) -> Any:
        """统一执行入口（同步 psycopg 包进线程池，连接取自池）"""
        import asyncio

        def _run():
            pool = self._get_pool()
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    if fetch:
                        cols = [d[0] for d in cur.description]
                        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
                    else:
                        rows = None
                conn.commit()
            return rows

        return await asyncio.to_thread(_run)

    async def load_store_preferences(self, tenant_id: str) -> Dict[str, Any]:
        rows = await self._execute(
            "SELECT * FROM store_preferences WHERE tenant_id = %s ORDER BY updated_at DESC LIMIT 1",
            (tenant_id,), fetch=True,
        )
        if not rows:
            logger.info(f"[PG] 门店 {tenant_id} 无偏好记录，返回空偏好")
            return self._empty_preferences(tenant_id)
        return rows[0]

    async def save_store_preferences(self, tenant_id: str, data: Dict[str, Any]) -> None:
        # ON CONFLICT：靠 uq_store_pref_tenant 唯一索引做幂等写入。
        # 原实现是纯 INSERT，每次保存都会新增一行，长期运行表会无限膨胀。
        sql = """
            INSERT INTO store_preferences
                (tenant_id, popular_dishes, seasonal_preferences, customer_feedback_summary,
                 avg_order_value, peak_hours, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (tenant_id) DO UPDATE SET
                popular_dishes            = EXCLUDED.popular_dishes,
                seasonal_preferences      = EXCLUDED.seasonal_preferences,
                customer_feedback_summary = EXCLUDED.customer_feedback_summary,
                avg_order_value           = EXCLUDED.avg_order_value,
                peak_hours                = EXCLUDED.peak_hours,
                updated_at                = NOW()
        """
        await self._execute(sql, (
            tenant_id,
            data.get("popular_dishes", []),
            json.dumps(data.get("seasonal_preferences", {}), ensure_ascii=False),
            json.dumps(data.get("customer_feedback_summary", {}), ensure_ascii=False),
            float(data.get("avg_order_value", 0) or 0),
            json.dumps(data.get("peak_hours", []), ensure_ascii=False),
        ))
        logger.info(f"[PG] 门店 {tenant_id} 偏好已保存")

    async def load_customer_profile(self, tenant_id: str, customer_id: str,
                                    user_id: str = "") -> Optional[Dict[str, Any]]:
        rows = await self._execute(
            "SELECT * FROM customer_profiles WHERE tenant_id = %s AND customer_id = %s LIMIT 1",
            (tenant_id, customer_id), fetch=True,
        )
        return rows[0] if rows else None

    async def upsert_customer_profile(self, tenant_id: str, customer_id: str,
                                      data: Dict[str, Any], user_id: str = "") -> None:
        # ON CONFLICT：靠 uq_customer_tenant 唯一索引真正实现「有则更新、无则插入」。
        # 原实现名为 upsert 实为纯 INSERT，同一顾客反复到店会堆积重复行，
        # 而 load_customer_profile 是 LIMIT 1 且无 ORDER BY，可能读到旧画像（过敏原写丢）。
        sql = """
            INSERT INTO customer_profiles
                (tenant_id, customer_id, user_id, name, dietary_restrictions, allergens,
                 favorite_dishes, visit_count, avg_spending, member_level, last_visit_date, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (tenant_id, customer_id) DO UPDATE SET
                user_id              = COALESCE(NULLIF(EXCLUDED.user_id, ''), customer_profiles.user_id),
                name                 = CASE WHEN EXCLUDED.name = '' THEN customer_profiles.name
                                            ELSE EXCLUDED.name END,
                dietary_restrictions = EXCLUDED.dietary_restrictions,
                allergens            = EXCLUDED.allergens,
                favorite_dishes      = EXCLUDED.favorite_dishes,
                visit_count          = GREATEST(customer_profiles.visit_count, EXCLUDED.visit_count),
                avg_spending         = EXCLUDED.avg_spending,
                member_level         = EXCLUDED.member_level,
                last_visit_date      = GREATEST(COALESCE(customer_profiles.last_visit_date, '1970-01-01'),
                                                COALESCE(EXCLUDED.last_visit_date, '1970-01-01')),
                updated_at           = NOW()
        """
        await self._execute(sql, (
            tenant_id, customer_id, user_id, data.get("name", ""),
            data.get("dietary_restrictions", []), data.get("allergens", []),
            data.get("favorite_dishes", []), int(data.get("visit_count", 0) or 0),
            float(data.get("avg_spending", 0) or 0), data.get("member_level", "普通"),
            data.get("last_visit_date") or date.today(),
        ))

    async def save_review_analysis(self, tenant_id: str, items: List[Dict[str, Any]],
                                   user_id: str = "") -> int:
        sql = """
            INSERT INTO review_analysis
                (tenant_id, user_id, review_id, platform, rating, sentiment, sentiment_score,
                 topics, summary, suggested_reply, reply_status, review_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        count = 0
        for item in items:
            await self._execute(sql, (
                tenant_id, user_id, item.get("review_id", ""), item.get("platform", ""),
                int(item.get("rating", 0) or 0), item.get("sentiment", "neutral"),
                float(item.get("sentiment_score", 0) or 0), item.get("topics", []),
                item.get("summary", ""), item.get("suggested_reply", ""),
                item.get("reply_status", "pending"), item.get("review_date") or date.today(),
            ))
            count += 1
        logger.info(f"[PG] 门店 {tenant_id} 保存 {count} 条评价分析")
        return count

    async def list_review_analysis(self, tenant_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        return await self._execute(
            "SELECT * FROM review_analysis WHERE tenant_id = %s ORDER BY created_at DESC LIMIT %s",
            (tenant_id, limit), fetch=True,
        )

    async def save_compliance_log(self, tenant_id: str, log: Dict[str, Any], user_id: str = "") -> None:
        sql = """
            INSERT INTO compliance_log
                (tenant_id, user_id, order_id, declared_allergens, detected_allergens,
                 recommended_dishes, isolation_action, action, retention_years)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        await self._execute(sql, (
            tenant_id, user_id, log.get("order_id", ""),
            log.get("declared_allergens", []), log.get("detected_allergens", []),
            log.get("recommended_dishes", []), log.get("isolation_action", ""),
            log.get("action", "normal"), config.COMPLIANCE_LOG_YEARS,
        ))
        logger.info(f"[PG][合规留痕] 门店 {tenant_id} 订单 {log.get('order_id')} 已留痕 "
                    f"（保存 {config.COMPLIANCE_LOG_YEARS} 年）")

    async def confirm_allergen_notice(self, tenant_id: str, order_id: str, *,
                                      confirmed_by: str = "", channel: str = "verbal",
                                      notice_content: str = "",
                                      customer_ack: bool = True,
                                      user_id: str = "") -> Dict[str, Any]:
        """记录过敏原口头告知确认（PG 实现：更新最近一条，无则插入）"""
        async with self._lock(f"{tenant_id}:compliance:{order_id}"):
            rows = await self._execute(
                "SELECT id FROM compliance_log WHERE tenant_id = %s AND order_id = %s "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (tenant_id, order_id), fetch=True,
            )
            payload = {
                "notice_channel": channel,
                "notice_content": notice_content,
                "confirmed_by": confirmed_by or user_id,
                "customer_ack": bool(customer_ack),
                "confirmed_at": datetime.now(),
            }
            if rows:
                await self._execute(
                    "UPDATE compliance_log SET notice_channel = %s, notice_content = %s, "
                    "noticed_at = COALESCE(noticed_at, NOW()), confirmed_by = %s, "
                    "confirmed_at = %s, customer_ack = %s WHERE id = %s",
                    (channel, notice_content, payload["confirmed_by"],
                     payload["confirmed_at"], bool(customer_ack), rows[0]["id"]),
                )
            else:
                await self._execute(
                    "INSERT INTO compliance_log (tenant_id, user_id, order_id, action, "
                    "notice_channel, notice_content, noticed_at, confirmed_by, confirmed_at, "
                    "customer_ack, retention_years) VALUES (%s, %s, %s, %s, %s, %s, NOW(), %s, %s, %s, %s)",
                    (tenant_id, user_id, order_id, "notice_confirm_only", channel,
                     notice_content, payload["confirmed_by"], payload["confirmed_at"],
                     bool(customer_ack), config.COMPLIANCE_LOG_YEARS),
                )
            logger.info(f"[PG][合规留痕] 门店 {tenant_id} 订单 {order_id} 过敏原告知已确认"
                        f"（告知人 {payload['confirmed_by']}，渠道 {channel}，"
                        f"顾客确认 {customer_ack}）")
            return {"tenant_id": tenant_id, "order_id": order_id,
                    "notice_channel": channel, "notice_content": notice_content,
                    **{k: v for k, v in payload.items() if k != "notice_content"}}

    async def list_compliance_log(self, tenant_id: str, order_id: str = "",
                                  limit: int = 100) -> List[Dict[str, Any]]:
        """查询合规留痕（PG 实现：可按订单号过滤，按时间倒序）"""
        if order_id:
            return await self._execute(
                "SELECT * FROM compliance_log WHERE tenant_id = %s AND order_id = %s "
                "ORDER BY created_at DESC LIMIT %s",
                (tenant_id, order_id, limit), fetch=True,
            )
        return await self._execute(
            "SELECT * FROM compliance_log WHERE tenant_id = %s ORDER BY created_at DESC LIMIT %s",
            (tenant_id, limit), fetch=True,
        )

    async def save_ingredient_usage(self, tenant_id: str, usage_date: date,
                                    usage: Dict[str, float], order_count: int = 0) -> None:
        """保存某日食材消耗（覆盖语义：同门店+同日+同食材 以本次值为准）"""
        sql = """
            INSERT INTO ingredient_usage (tenant_id, usage_date, ingredient, used_amount, order_count)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, usage_date, ingredient) DO UPDATE SET
                used_amount = EXCLUDED.used_amount,
                order_count = EXCLUDED.order_count
        """
        for ing, amount in (usage or {}).items():
            await self._execute(sql, (tenant_id, usage_date, ing, float(amount), order_count))

    async def add_ingredient_usage(self, tenant_id: str, usage_date: date,
                                   usage: Dict[str, float], order_count: int = 0) -> None:
        """
        原子累加当日食材消耗（PG 实现：交给数据库做增量，不再「读-合-写」）。

        与基类默认实现的差别：基类在进程内锁里读-改-写，多进程部署（uvicorn worker>1、
        k8s 多副本）时锁失效会导致用量互相覆盖；这里用 ON CONFLICT DO UPDATE 让
        PostgreSQL 行锁保证累加正确，单条 SQL 完成。
        """
        sql = """
            INSERT INTO ingredient_usage (tenant_id, usage_date, ingredient, used_amount, order_count)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, usage_date, ingredient) DO UPDATE SET
                used_amount = ingredient_usage.used_amount + EXCLUDED.used_amount,
                order_count = ingredient_usage.order_count + EXCLUDED.order_count
        """
        for ing, amount in (usage or {}).items():
            await self._execute(sql, (tenant_id, usage_date, ing, float(amount), order_count))

    @staticmethod
    def _empty_preferences(tenant_id: str) -> Dict[str, Any]:
        return {
            "tenant_id": tenant_id, "popular_dishes": [], "seasonal_preferences": {},
            "customer_feedback_summary": {}, "avg_order_value": 0, "peak_hours": [],
        }

    # ==================== 订单状态机（PG 实现） ====================

    async def load_order_status(self, tenant_id: str, order_id: str) -> Optional[str]:
        rows = await self._execute(
            "SELECT to_status FROM order_status_log "
            "WHERE tenant_id = %s AND order_id = %s ORDER BY created_at DESC, id DESC LIMIT 1",
            (tenant_id, order_id), fetch=True,
        )
        return rows[0]["to_status"] if rows else None

    async def save_order_status_event(self, tenant_id: str, order_id: str, from_status: str,
                                      to_status: str, operator: str = "system",
                                      reason: str = "", user_id: str = "",
                                      meta: Optional[Dict[str, Any]] = None) -> None:
        sql = """
            INSERT INTO order_status_log
                (tenant_id, user_id, order_id, from_status, to_status, operator, reason, meta)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """
        await self._execute(sql, (
            tenant_id, user_id, order_id, from_status or None, to_status,
            operator, reason, json.dumps(meta or {}, ensure_ascii=False),
        ))

    async def list_order_status_events(self, tenant_id: str, order_id: str) -> List[Dict[str, Any]]:
        return await self._execute(
            "SELECT * FROM order_status_log WHERE tenant_id = %s AND order_id = %s "
            "ORDER BY created_at ASC, id ASC",
            (tenant_id, order_id), fetch=True,
        )

    # ==================== 记忆清理（PG 实现） ====================

    async def _purge_domain(self, domain: str, days: int, cutoff: datetime,
                            tenant_id: str, dry_run: bool) -> int:
        """
        清理一个「按天保留」的数据域。

        时间列选用（已核对 memory/schemas.py 的 DDL，三张表都已有可用时间列，
        无需新增列，因此 docs/deploy/schema.sql 也无需同步修改）：
            review_analysis   → review_date（DATE，业务发生日）
            order_status_log  → created_at（TIMESTAMP）
        复用既有 _execute(sql, params, fetch=...) 入口 + asyncio.to_thread 包装风格。
        """
        column = TIME_COLUMNS.get(domain)
        if not column:
            logger.warning(f"[PG][记忆清理] 未知数据域 {domain}，跳过")
            return 0

        where = f"{column} < now() - interval '%s days'"
        params: List[Any] = [int(days)]
        if tenant_id:
            where += " AND tenant_id = %s"
            params.append(tenant_id)

        if dry_run:
            rows = await self._execute(
                f"SELECT COUNT(*) AS c FROM {domain} WHERE {where}",
                tuple(params), fetch=True)
            return int((rows or [{}])[0].get("c") or 0)

        rows = await self._execute(
            f"DELETE FROM {domain} WHERE {where} RETURNING id",
            tuple(params), fetch=True)
        return len(rows or [])

    async def _purge_compliance_log(self, cutoff: datetime, tenant_id: str,
                                    dry_run: bool) -> int:
        """
        清理超过保留年限的合规留痕。

        需求 3.8 Q8：过敏原订单留痕**保存 3 年备查**，因此「保留期 = retention_years」
        （表内逐行存储，默认取 config.COMPLIANCE_LOG_YEARS）；仅有确实填了正数
        retention_years 的记录才参与清理，NULL / 0 视为永久保留。

        说明：cutoff 参数仅为与 _purge_domain 保持统一签名，实际截止线由 SQL 内的
              now() - make_interval(years => retention_years) 计算（以库时钟为准）。
        """
        where = ("created_at < now() - make_interval(years => COALESCE(retention_years, 0)) "
                 "AND COALESCE(retention_years, 0) > 0")
        params: List[Any] = []
        if tenant_id:
            where += " AND tenant_id = %s"
            params.append(tenant_id)

        if dry_run:
            rows = await self._execute(
                f"SELECT COUNT(*) AS c FROM compliance_log WHERE {where}",
                tuple(params), fetch=True)
            return int((rows or [{}])[0].get("c") or 0)

        rows = await self._execute(
            f"DELETE FROM compliance_log WHERE {where} RETURNING id",
            tuple(params), fetch=True)
        return len(rows or [])


# ==================================================================
# 实现二：文件仓库（零依赖降级实现）
# ==================================================================

class FileStore(BaseStore):
    """
    JSON 文件长期记忆仓库（降级实现）。
    用于无 PostgreSQL 环境的开发自测；接口与 PostgresStore 完全一致。
    数据落地于 data/memory_store/{tenant_id}.json，同样保留 tenant_id 隔离。

    目录可覆盖（供验证脚本把产物写进独立临时目录，保证「跑完不留痕」）：
        new FileStore(base_dir) 或环境变量 FOODAGENT_MEMORY_STORE_DIR
    """

    REQUIRES_BASE_DIR = True

    def __init__(self, base_dir: Optional[str] = None):
        self.base_dir = (base_dir
                         or os.getenv("FOODAGENT_MEMORY_STORE_DIR")
                         or os.path.join(config.DATA_DIR, "memory_store"))
        os.makedirs(self.base_dir, exist_ok=True)
        logger.info(f"[FileStore] 长期记忆降级为文件存储：{self.base_dir}")

    def _lock(self, tenant_id: str):
        return _get_write_lock(tenant_id)

    async def init(self) -> None:
        os.makedirs(self.base_dir, exist_ok=True)

    def _path(self, tenant_id: str) -> str:
        # 文件名做安全处理，避免路径穿越
        safe = "".join(c for c in tenant_id if c.isalnum() or c in "-_") or "default"
        return os.path.join(self.base_dir, f"{safe}.json")

    def _load(self, tenant_id: str) -> Dict[str, Any]:
        path = self._path(tenant_id)
        if not os.path.exists(path):
            return {"store_preferences": {}, "customer_profiles": {},
                    "review_analysis": [], "compliance_log": [], "ingredient_usage": []}
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"[FileStore] 读取 {path} 失败，返回空数据: {exc}")
            return {"store_preferences": {}, "customer_profiles": {},
                    "review_analysis": [], "compliance_log": [], "ingredient_usage": []}

    def _save(self, tenant_id: str, data: Dict[str, Any]) -> None:
        path = self._path(tenant_id)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)

    async def load_store_preferences(self, tenant_id: str) -> Dict[str, Any]:
        data = self._load(tenant_id)
        prefs = data.get("store_preferences") or {}
        if not prefs:
            return PostgresStore._empty_preferences(tenant_id)
        return prefs

    async def save_store_preferences(self, tenant_id: str, data: Dict[str, Any]) -> None:
        store = self._load(tenant_id)
        store["store_preferences"] = {**data, "updated_at": datetime.now().isoformat()}
        self._save(tenant_id, store)
        logger.info(f"[FileStore] 门店 {tenant_id} 偏好已保存")

    async def append_store_preferences(self, tenant_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        """
        原子追加门店偏好（修复并发覆盖）：
            读-合-写整体在门店级锁内完成，并发下单不会互相丢数据。
        """
        async with self._lock(tenant_id):
            store = self._load(tenant_id)
            current = store.get("store_preferences") or {}
            merged = _merge_preferences(current, patch)
            merged["updated_at"] = datetime.now().isoformat()
            store["store_preferences"] = merged
            self._save(tenant_id, store)
            logger.info(f"[FileStore] 门店 {tenant_id} 偏好已原子追加"
                        f"（热门菜 {len(merged.get('popular_dishes', []))} 项）")
            return merged

    async def load_customer_profile(self, tenant_id: str, customer_id: str,
                                    user_id: str = "") -> Optional[Dict[str, Any]]:
        store = self._load(tenant_id)
        return store.get("customer_profiles", {}).get(customer_id)

    async def upsert_customer_profile(self, tenant_id: str, customer_id: str,
                                      data: Dict[str, Any], user_id: str = "") -> None:
        store = self._load(tenant_id)
        profiles = store.setdefault("customer_profiles", {})
        existing = profiles.get(customer_id, {})
        # 合并：已有字段保留，新字段覆盖（避免丢失历史偏好）
        merged = {**existing, **data}
        merged["user_id"] = user_id or existing.get("user_id", "")
        merged["updated_at"] = datetime.now().isoformat()
        profiles[customer_id] = merged
        self._save(tenant_id, store)

    async def save_review_analysis(self, tenant_id: str, items: List[Dict[str, Any]],
                                   user_id: str = "") -> int:
        store = self._load(tenant_id)
        records = store.setdefault("review_analysis", [])
        for item in items:
            records.append({**item, "tenant_id": tenant_id, "user_id": user_id,
                            "created_at": datetime.now().isoformat()})
        self._save(tenant_id, store)
        logger.info(f"[FileStore] 门店 {tenant_id} 保存 {len(items)} 条评价分析")
        return len(items)

    async def list_review_analysis(self, tenant_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        store = self._load(tenant_id)
        records = store.get("review_analysis", [])
        return list(reversed(records))[:limit]

    async def save_compliance_log(self, tenant_id: str, log: Dict[str, Any], user_id: str = "") -> None:
        store = self._load(tenant_id)
        logs = store.setdefault("compliance_log", [])
        logs.append({**log, "tenant_id": tenant_id, "user_id": user_id,
                     "retention_years": config.COMPLIANCE_LOG_YEARS,
                     "created_at": datetime.now().isoformat()})
        self._save(tenant_id, store)
        logger.info(f"[FileStore][合规留痕] 门店 {tenant_id} 订单 {log.get('order_id')} 已留痕")

    async def confirm_allergen_notice(self, tenant_id: str, order_id: str, *,
                                      confirmed_by: str = "", channel: str = "verbal",
                                      notice_content: str = "",
                                      customer_ack: bool = True,
                                      user_id: str = "") -> Dict[str, Any]:
        """记录过敏原口头告知确认（文件实现：更新最近一条，无则插入）"""
        async with self._lock(f"{tenant_id}:compliance:{order_id}"):
            store = self._load(tenant_id)
            logs = store.setdefault("compliance_log", [])
            now = datetime.now().isoformat()
            target = None
            for entry in reversed(logs):
                if entry.get("order_id") == order_id:
                    target = entry
                    break
            if target is not None:
                target.update({
                    "notice_channel": channel,
                    "notice_content": notice_content,
                    "noticed_at": target.get("noticed_at") or now,
                    "confirmed_by": confirmed_by or user_id,
                    "confirmed_at": now,
                    "customer_ack": bool(customer_ack),
                })
                record = target
            else:
                record = {
                    "tenant_id": tenant_id, "user_id": user_id, "order_id": order_id,
                    "action": "notice_confirm_only", "notice_channel": channel,
                    "notice_content": notice_content, "noticed_at": now,
                    "confirmed_by": confirmed_by or user_id, "confirmed_at": now,
                    "customer_ack": bool(customer_ack),
                    "retention_years": config.COMPLIANCE_LOG_YEARS, "created_at": now,
                }
                logs.append(record)
            self._save(tenant_id, store)
        logger.info(f"[FileStore][合规留痕] 门店 {tenant_id} 订单 {order_id} 过敏原告知已确认"
                    f"（告知人 {confirmed_by or user_id}，渠道 {channel}，顾客确认 {customer_ack}）")
        return record

    async def list_compliance_log(self, tenant_id: str, order_id: str = "",
                                  limit: int = 100) -> List[Dict[str, Any]]:
        """查询合规留痕（可按订单号过滤，按时间倒序）"""
        store = self._load(tenant_id)
        logs = store.get("compliance_log", [])
        if order_id:
            logs = [x for x in logs if x.get("order_id") == order_id]
        return list(reversed(logs))[:limit]

    async def save_ingredient_usage(self, tenant_id: str, usage_date: date,
                                    usage: Dict[str, float], order_count: int = 0) -> None:
        store = self._load(tenant_id)
        records = store.setdefault("ingredient_usage", [])
        for ing, amount in (usage or {}).items():
            records.append({"tenant_id": tenant_id, "usage_date": str(usage_date),
                            "ingredient": ing, "used_amount": float(amount),
                            "order_count": order_count})
        self._save(tenant_id, store)

    # ==================== 订单状态机（文件实现） ====================

    async def load_order_status(self, tenant_id: str, order_id: str) -> Optional[str]:
        store = self._load(tenant_id)
        events = [e for e in store.get("order_status_log", [])
                  if e.get("order_id") == order_id]
        return events[-1]["to_status"] if events else None

    async def save_order_status_event(self, tenant_id: str, order_id: str, from_status: str,
                                      to_status: str, operator: str = "system",
                                      reason: str = "", user_id: str = "",
                                      meta: Optional[Dict[str, Any]] = None) -> None:
        store = self._load(tenant_id)
        logs = store.setdefault("order_status_log", [])
        logs.append({
            "tenant_id": tenant_id, "user_id": user_id, "order_id": order_id,
            "from_status": from_status or "", "to_status": to_status,
            "operator": operator, "reason": reason, "meta": meta or {},
            "created_at": datetime.now().isoformat(),
        })
        self._save(tenant_id, store)

    async def list_order_status_events(self, tenant_id: str, order_id: str) -> List[Dict[str, Any]]:
        store = self._load(tenant_id)
        return [e for e in store.get("order_status_log", [])
                if e.get("order_id") == order_id]

    # ==================== 记忆清理（文件实现） ====================

    def _iter_tenant_files(self) -> List[str]:
        """列出记忆目录下所有门店 JSON 文件（供「不指定门店」的全量清理使用）"""
        if not os.path.isdir(self.base_dir):
            return []
        return sorted(
            os.path.join(self.base_dir, name)
            for name in os.listdir(self.base_dir)
            if name.endswith(".json") and os.path.isfile(os.path.join(self.base_dir, name))
        )

    async def purge_expired(self, retention: Optional[Dict[str, int]] = None,
                            tenant_id: str = "", dry_run: bool = False,
                            now: Optional[datetime] = None) -> Dict[str, int]:
        """
        按保留策略清理门店 JSON（文件实现）。

        与基类不同之处：门店级 JSON 的写入必须**沿用既有的门店级写锁**
        （见模块头 _WRITE_LOCKS 说明），否则清理与业务写并发时会互相覆盖，
        因此这里整体覆写，把「每个门店：读 → 过滤 → 写回」放在该门店的锁内完成。
        """
        if not tenant_id:
            # 未指定门店 → 遍历 data/memory_store/*.json 逐个清理后汇总
            total: Dict[str, int] = {}
            for path in self._iter_tenant_files():
                one = await self.purge_expired(retention, os.path.basename(path)[:-len(".json")],
                                               dry_run, now)
                for key, value in one.items():
                    total[key] = total.get(key, 0) + value
            return total

        async with self._lock(tenant_id):
            # dry-run 需要看「若实跑会删掉多少」，因此不能在预演后再统计残留；
            # 实跑则由 super() 的 _purge_domain 负责落盘，这里重新读盘做重排与日志。
            before_counts = {k: len(v) for k, v in self._load(tenant_id).items() if isinstance(v, list)}
            result = await super().purge_expired(retention, tenant_id, dry_run, now)

            if not dry_run:
                store = self._load(tenant_id)
                if isinstance(store.get("order_status_log"), list):
                    # 状态流转日志已按时间过滤，再重排为「时间正序」，
                    # 保证 load_order_status() 取最后一条的语义不受清理影响。
                    store["order_status_log"] = self._sort_events_asc(store["order_status_log"])
                self._save(tenant_id, store)
            after_counts = {k: len(v) for k, v in self._load(tenant_id).items() if isinstance(v, list)}
            logger.info(f"[FileStore][记忆清理] 门店 {tenant_id} "
                        f"{'（dry-run 未落盘）' if dry_run else '已落盘'}："
                        f"{before_counts} → {after_counts}")
            return result

    @staticmethod
    def _sort_events_asc(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """状态流转事件按时间正序重排（无法解析时间的条目保留在最后，不丢数据）"""
        parsed: List[Tuple[datetime, int, Dict[str, Any]]] = []
        for index, event in enumerate(events):
            ts = _parse_ts(event.get("created_at"))
            parsed.append((ts or datetime.min, index, event))
        parsed.sort(key=lambda item: (item[0], item[1]))
        return [item[2] for item in parsed]

    async def _purge_domain(self, domain: str, days: int, cutoff: datetime,
                            tenant_id: str, dry_run: bool) -> int:
        """按天清理一个数据域（文件实现：过滤 JSON 数组）"""
        store = self._load(tenant_id)
        records = store.get(domain)
        if not isinstance(records, list) or not records:
            return 0

        keys = FILE_TIME_KEYS.get(domain, ("created_at",))
        kept: List[Dict[str, Any]] = []
        removed = 0
        for item in records:
            ts = None
            for key in keys:
                ts = _parse_ts((item or {}).get(key))
                if ts is not None:
                    break
            if ts is None:
                # 保守策略：时间戳缺失/无法解析 → 跳过（不删），并打 warning
                logger.warning(f"[FileStore][记忆清理] {domain} 记录缺少可解析的时间字段 "
                               f"{keys}，保守跳过：{str(item)[:120]}")
                kept.append(item)
                continue
            if ts < cutoff:
                removed += 1
            else:
                kept.append(item)

        store[domain] = kept
        if not dry_run:
            self._save(tenant_id, store)
        return removed

    async def _purge_compliance_log(self, cutoff: datetime, tenant_id: str,
                                    dry_run: bool) -> int:
        """
        清理超过保留期的合规留痕（文件实现）。

        保留期取记录自带的时间字段（created_at，缺失时退 noticed_at / confirmed_at）。
        「保留 3 年」语义下，created_at 早于「当前时间 - 保留期」即视为过期。
        记录自带的 retention_years（历史字段语义为「应保存的年数」）不参与判定——
        否则它永远小于「已过去的时间」会让全部留痕被误删。
        """
        store = self._load(tenant_id)
        logs = store.get("compliance_log")
        if not isinstance(logs, list) or not logs:
            return 0

        keys = FILE_TIME_KEYS["compliance_log"]
        kept: List[Dict[str, Any]] = []
        removed = 0
        for item in logs:
            ts = None
            for key in keys:
                ts = _parse_ts((item or {}).get(key))
                if ts is not None:
                    break
            if ts is None:
                logger.warning(f"[FileStore][记忆清理] compliance_log 记录缺少可解析的时间字段 "
                               f"{keys}，保守跳过：{str(item)[:120]}")
                kept.append(item)
                continue
            if ts < cutoff:
                removed += 1
            else:
                kept.append(item)

        store["compliance_log"] = kept
        if not dry_run:
            self._save(tenant_id, store)
        return removed


# ==================================================================
# 仓库工厂：按配置自动选择实现
# ==================================================================

_store_instance: Optional[BaseStore] = None
_store_ready = False


async def get_store() -> BaseStore:
    """
    获取全局长期记忆仓库（单例）。
    按 config.PG_ENABLED 决定使用 PostgresStore 还是 FileStore；
    PG 连接失败时自动降级为 FileStore，保证服务不中断。
    """
    global _store_instance, _store_ready

    if _store_instance is not None and _store_ready:
        return _store_instance

    if config.PG_ENABLED:
        store: Optional[PostgresStore] = None
        try:
            store = PostgresStore()
            await store.init()
            _store_instance = store
            _store_ready = True
            logger.info("[记忆系统] 使用 PostgreSQL 长期记忆仓库")
            return store
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[记忆系统] PostgreSQL 不可用（{exc}），自动降级为文件存储")
            if store is not None:
                # 连接池可能已半创建（例如 open 成功但建表失败），这里一并释放，避免泄漏连接
                store.close()

    store = FileStore()
    await store.init()
    _store_instance = store
    _store_ready = True
    logger.info("[记忆系统] 使用文件长期记忆仓库（降级模式）")
    return store


def close_store() -> None:
    """释放长期记忆仓库持有的连接池（服务 shutdown 调用；幂等）"""
    global _store_instance, _store_ready
    if isinstance(_store_instance, PostgresStore):
        _store_instance.close()
    _store_instance = None
    _store_ready = False


if __name__ == '__main__':
    import asyncio

    async def _selftest():
        print("=" * 60)
        print("长期记忆仓库自检（自动选择实现）")
        print("=" * 60)
        store = await get_store()
        print("实现类型：", type(store).__name__)

        # 写入并读取门店偏好
        await store.save_store_preferences("store_001", {
            "popular_dishes": ["招牌烤鸭", "宫保鸡丁"],
            "peak_hours": ["11:30-13:00"], "avg_order_value": 68.5,
        })
        prefs = await store.load_store_preferences("store_001")
        print("门店偏好：", prefs.get("popular_dishes"), "客单价", prefs.get("avg_order_value"))

        # 门店隔离验证：store_002 应读不到 store_001 的数据
        other = await store.load_store_preferences("store_002")
        print("门店隔离验证：store_002 热门菜品 =", other.get("popular_dishes"), "（应为空）")

        # 顾客画像
        await store.upsert_customer_profile("store_001", "cust_888", {
            "dietary_restrictions": ["低卡"], "allergens": ["花生"],
            "favorite_dishes": ["清蒸鲈鱼"], "visit_count": 3,
        })
        profile = await store.load_customer_profile("store_001", "cust_888")
        print("顾客画像：", profile.get("allergens"), profile.get("dietary_restrictions"))

        # 评价分析
        await store.save_review_analysis("store_001", [
            {"review_id": "r1", "platform": "美团", "rating": 5,
             "sentiment": "positive", "sentiment_score": 0.8, "topics": ["口味"]},
        ])
        reviews = await store.list_review_analysis("store_001")
        print("评价记录条数：", len(reviews))

        # 合规留痕
        await store.save_compliance_log("store_001", {
            "order_id": "ORD1001", "declared_allergens": ["花生"],
            "detected_allergens": ["花生"], "action": "trigger_food_safety_review",
        })
        print("合规留痕：已写入")
        print("\n自检通过。")

    async def _purge_selftest():
        """记忆清理自检：临时目录 + 伪造过期数据，验证 purge_expired 行为"""
        import shutil
        import tempfile

        print("=" * 60)
        print("记忆清理自检（purge_expired，临时目录，跑完不留痕）")
        print("=" * 60)

        # 优先用项目 data/ 下的临时目录（受限环境/只读系统临时目录下也能写），
        # 系统临时目录仅作兜底；两种情况都在 finally 里删除。
        tmp = ""
        try:
            tmp = os.path.join(config.DATA_DIR, "_purge_selftest_tmp")
            os.makedirs(tmp, exist_ok=True)
        except OSError:
            tmp = tempfile.mkdtemp(prefix="foodagent_purge_")
        try:
            store = FileStore(base_dir=tmp)
            tenant = "store_purge_demo"
            old = (datetime.now() - timedelta(days=200)).isoformat()
            fresh = datetime.now().isoformat()
            data = {
                "store_preferences": {"popular_dishes": ["招牌烤鸭"], "updated_at": old},
                "customer_profiles": {"cust_1": {"name": "张三", "updated_at": old}},
                "review_analysis": [
                    # 800 天 > 2 年保留期 → 应清理
                    {"review_id": "r_expired",
                     "created_at": (datetime.now() - timedelta(days=800)).isoformat()},
                    # 200 天 < 2 年保留期 → 必须保留（这正是与「对话 90 天」口径的差异）
                    {"review_id": "r_kept", "created_at": old},
                    {"review_id": "r_new", "created_at": fresh},
                ],
                "order_status_log": [
                    {"order_id": "ORD_OLD", "to_status": "reviewed", "created_at": old},
                    {"order_id": "ORD_NEW", "to_status": "created", "created_at": fresh},
                ],
                # 合规留痕：3 年（约 1096 天）之前的一条应被清理
                "compliance_log": [
                    {"order_id": "ORD_C_OLD", "created_at": (datetime.now() - timedelta(days=1200)).isoformat()},
                    {"order_id": "ORD_C_NEW", "created_at": fresh},
                ],
            }
            store._save(tenant, data)
            print("清理前：", {k: len(v) for k, v in data.items() if isinstance(v, list)})

            # dry-run：只统计不落盘
            preview = await store.purge_expired(tenant_id=tenant, dry_run=True)
            print("dry-run 统计：", preview)
            assert preview["review_analysis"] == 1, preview
            assert len(store._load(tenant)["review_analysis"]) == 3, "dry-run 不应落盘"

            # 正式清理
            purged = await store.purge_expired(tenant_id=tenant)
            print("实际清理：", purged)
            after = store._load(tenant)
            print("清理后：", {k: len(v) for k, v in after.items() if isinstance(v, list)})

            # 断言：按需求口径
            assert purged["review_analysis"] == 1, purged          # 2 年口径（200 天的记录保留）
            assert purged["order_status_log"] == 1, purged         # 90 天口径
            assert purged["compliance_log"] == 1, purged           # 3 年口径
            assert purged["store_preferences"] == 0, purged        # 永久
            assert purged["customer_profiles"] == 0, purged        # 永久
            assert len(after["review_analysis"]) == 2, after["review_analysis"]
            assert after["store_preferences"]["popular_dishes"] == ["招牌烤鸭"]
            assert "cust_1" in after["customer_profiles"]

            # 传非 0 也必须跳过永久域
            forced = await store.purge_expired(
                {"store_preferences": 1, "customer_profiles": 1}, tenant_id=tenant)
            assert forced["store_preferences"] == 0 and forced["customer_profiles"] == 0, forced
            assert store._load(tenant)["store_preferences"], "门店偏好被误删"
            print("\n记忆清理自检通过（永久域即使传非 0 也不清理）。")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    asyncio.run(_selftest())
    asyncio.run(_purge_selftest())
