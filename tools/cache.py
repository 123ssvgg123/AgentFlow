"""
    Redis 缓存层：tools/cache.py

    职责：为门店偏好、顾客画像、MCP 检索结果提供统一缓存接口。

    三级降级设计（保证「有 Redis 更快，无 Redis 也能跑」）：
        L1  进程内字典（默认，零依赖，单机可用，重启失效）
        L2  Redis（启用后自动接入，多进程/多副本共享）
        L3  无缓存（缓存本身故障时直接透传，绝不阻断业务）

    启用步骤（见 docs/OBSERVABILITY.md）：
        1) pip install redis
        2) config.ini 的 [redis] enabled = true
        3) 重启服务，日志会打印「Redis 缓存已启用」
"""
import json
import time
from typing import Any, Dict, Optional, Tuple

from base.config import config
from base.logger import logger

#: 进程内缓存：{key: (value, expire_at)}
_LOCAL_CACHE: Dict[str, Tuple[Any, float]] = {}
#: 进程内缓存条数上限（防止无界增长）
_LOCAL_MAX_SIZE = 2000


class CacheBackend:
    """缓存后端（进程内 + 可选 Redis，自动降级）"""

    def __init__(self):
        self._redis = None
        self._redis_ready = False
        self._hits = 0
        self._misses = 0

    # ==================== 初始化 ====================

    def init(self) -> Dict[str, Any]:
        """初始化缓存后端（幂等）"""
        if not config.REDIS_ENABLED:
            logger.info("[缓存] Redis 未启用，使用进程内缓存（单机模式）")
            return {"backend": "local", "redis": False}

        try:
            import redis  # noqa: F401
        except ImportError as exc:
            logger.warning(f"[缓存] Redis 依赖未安装（{exc}），降级为进程内缓存；"
                           f"请执行：pip install redis")
            return {"backend": "local", "redis": False, "reason": "missing_dependency"}

        try:
            import redis
            self._redis = redis.Redis(
                host=config.REDIS_HOST, port=config.REDIS_PORT,
                db=config.REDIS_DB, password=config.REDIS_PASSWORD or None,
                decode_responses=True,
                socket_connect_timeout=2, socket_timeout=2,
            )
            self._redis.ping()
            self._redis_ready = True
            logger.info(f"[缓存] Redis 缓存已启用：{config.REDIS_HOST}:{config.REDIS_PORT}"
                        f"/db{config.REDIS_DB} prefix={config.REDIS_KEY_PREFIX}")
            return {"backend": "redis", "redis": True}
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[缓存] Redis 连接失败（{exc}），降级为进程内缓存")
            self._redis = None
            self._redis_ready = False
            return {"backend": "local", "redis": False, "reason": str(exc)}

    # ==================== 键处理 ====================

    @staticmethod
    def _full_key(key: str) -> str:
        return f"{config.REDIS_KEY_PREFIX}{key}"

    # ==================== 读写 ====================

    def get(self, key: str) -> Optional[Any]:
        """读缓存（未命中返回 None）"""
        # L2：Redis
        if self._redis_ready:
            try:
                raw = self._redis.get(self._full_key(key))
                if raw is not None:
                    self._hits += 1
                    return json.loads(raw)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[缓存] Redis 读取失败，回退进程内缓存: {exc}")

        # L1：进程内
        item = _LOCAL_CACHE.get(key)
        if item is None:
            self._misses += 1
            return None
        value, expire_at = item
        if expire_at and expire_at < time.time():
            _LOCAL_CACHE.pop(key, None)
            self._misses += 1
            return None
        self._hits += 1
        return value

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> bool:
        """写缓存（ttl 秒；None 用默认 TTL）"""
        ttl = ttl if ttl is not None else config.REDIS_DEFAULT_TTL

        # L2：Redis
        if self._redis_ready:
            try:
                self._redis.set(self._full_key(key),
                                json.dumps(value, ensure_ascii=False, default=str),
                                ex=ttl)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[缓存] Redis 写入失败，仅写进程内: {exc}")

        # L1：进程内（始终写，保证 Redis 抖动时本地仍有命中的可能）
        if len(_LOCAL_CACHE) >= _LOCAL_MAX_SIZE:
            # 简单淘汰：清掉已过期的，再移除最早插入的 10%
            now = time.time()
            for k in [k for k, (_, exp) in _LOCAL_CACHE.items() if exp and exp < now]:
                _LOCAL_CACHE.pop(k, None)
            if len(_LOCAL_CACHE) >= _LOCAL_MAX_SIZE:
                for k in list(_LOCAL_CACHE.keys())[: _LOCAL_MAX_SIZE // 10]:
                    _LOCAL_CACHE.pop(k, None)
        _LOCAL_CACHE[key] = (value, time.time() + ttl if ttl else 0)
        return True

    def delete(self, key: str) -> bool:
        """删除缓存"""
        if self._redis_ready:
            try:
                self._redis.delete(self._full_key(key))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[缓存] Redis 删除失败: {exc}")
        _LOCAL_CACHE.pop(key, None)
        return True

    def clear(self, prefix: str = "") -> int:
        """按前缀清理（返回清理条数）"""
        count = 0
        keys = [k for k in _LOCAL_CACHE if k.startswith(prefix)]
        for k in keys:
            _LOCAL_CACHE.pop(k, None)
            count += 1
        if self._redis_ready:
            try:
                pattern = self._full_key(f"{prefix}*")
                cursor = 0
                while True:
                    cursor, batch = self._redis.scan(cursor=cursor, match=pattern, count=200)
                    if batch:
                        self._redis.delete(*batch)
                        count += len(batch)
                    if cursor == 0:
                        break
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[缓存] Redis 按前缀清理失败: {exc}")
        return count

    # ==================== 统计 ====================

    def stats(self) -> Dict[str, Any]:
        """缓存统计（供 /health 与 Prometheus 指标使用）"""
        total = self._hits + self._misses
        return {
            "backend": "redis" if self._redis_ready else "local",
            "redis_connected": self._redis_ready,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total, 4) if total else 0.0,
            "local_entries": len(_LOCAL_CACHE),
            "default_ttl": config.REDIS_DEFAULT_TTL,
        }


# ==================== 全局单例与便捷函数 ====================

cache = CacheBackend()

#: 常用缓存键前缀（统一管理，避免拼写不一致）
KEY_STORE_PREFS = "store_prefs:"
KEY_CUSTOMER_PROFILE = "customer_profile:"
KEY_MCP_SEARCH = "mcp_search:"
KEY_FOOD_SAFETY_LAW = "fss_law:"
KEY_NUTRITION = "nutrition:"
KEY_WEB_SEARCH = "web_search:"


def cached(key: str, ttl: Optional[int] = None):
    """
    装饰器：缓存异步函数的返回值。

    用法：
        @cached("nutrition", ttl=600)
        async def query_nutrition(dish_name): ...
    注意：key 会自动拼接函数参数（简单 repr），仅适合参数可哈希的简单场景。
    """
    def _decorator(func):
        async def _wrapper(*args, **kwargs):
            cache_key = f"{key}:{args[1:] if len(args) > 1 else ()}:{sorted(kwargs.items())}"
            hit = cache.get(cache_key)
            if hit is not None:
                return hit
            result = await func(*args, **kwargs)
            if result is not None:
                cache.set(cache_key, result, ttl)
            return result
        _wrapper.__name__ = getattr(func, "__name__", "cached_wrapper")
        return _wrapper
    return _decorator


if __name__ == '__main__':
    print("=" * 60)
    print("缓存层自检")
    print("=" * 60)
    print("Redis 开关：", config.REDIS_ENABLED)
    print("初始化：", cache.init())

    # 基本读写
    cache.set("test:key1", {"a": 1, "中文": "值"}, ttl=60)
    v = cache.get("test:key1")
    assert v == {"a": 1, "中文": "值"}, f"读写不一致：{v}"
    print("读写校验通过：", v)

    # 未命中
    assert cache.get("test:not_exist") is None
    print("未命中断言通过")

    # 过期
    cache.set("test:key2", "expire_soon", ttl=1)
    assert cache.get("test:key2") == "expire_soon"
    print("TTL 写入校验通过（TTL=1s）")

    # 删除
    cache.delete("test:key1")
    assert cache.get("test:key1") is None
    print("删除校验通过")

    # 统计
    print("\n统计：", cache.stats())

    cache.clear("test:")
    print("\n自检通过。")
