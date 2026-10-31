"""
    JWT 认证模块：api/auth.py

    职责（需求文档 4.1「注册与登录」）：
        1. 门店员工用「门店工号 + 密码」登录
        2. 后端签发 JWT Token（含 tenant_id、user_id、role）
        3. 后续请求在 Header 带 Authorization: Bearer <token>
        Token 有效期 2 小时（配置 config.JWT_EXPIRE_MINUTES）

    说明：示例账号体系为内置演示数据（真实项目应接甲方门店管理系统的账号服务）。
        密码使用 PBKDF2-HMAC-SHA256 加盐哈希存储，不落明文。
"""
import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from base.config import config
from base.logger import logger

# Bearer Token 提取器（auto_error=False 以便自定义未认证提示）
bearer_scheme = HTTPBearer(auto_error=False)


# ==================================================================
# 内置演示账号（真实项目替换为数据库查询）
# 格式：工号 -> {password_hash, salt, tenant_id, user_id, role, name}
# ==================================================================
def _hash_password(password: str, salt: str) -> str:
    """PBKDF2-HMAC-SHA256 加盐哈希"""
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()


def _make_account(username: str, password: str, tenant_id: str, role: str, name: str) -> Dict[str, Any]:
    """构造账号记录（含随机盐）"""
    salt = os.urandom(16).hex()
    return {
        "username": username,
        "password_hash": _hash_password(password, salt),
        "salt": salt,
        "tenant_id": tenant_id,
        "user_id": username,
        "role": role,
        "name": name,
    }


# 演示账号：3 个角色对应需求文档 4.1 的「不同角色的使用差异」
DEMO_ACCOUNTS: Dict[str, Dict[str, Any]] = {
    # 服务员：智能点餐、菜品推荐、过敏原查询
    "waiter_01": _make_account("waiter_01", "123456", "store_001", "waiter", "张服务"),
    "waiter_02": _make_account("waiter_02", "123456", "store_002", "waiter", "李服务"),
    # 后厨主管：后厨调度、排产管理、食材监控
    "kitchen_01": _make_account("kitchen_01", "123456", "store_001", "kitchen", "王主厨"),
    # 店长：评价分析、经营数据、决策支持
    "manager_01": _make_account("manager_01", "123456", "store_001", "manager", "赵店长"),
}


class AuthService:
    """JWT 认证服务"""

    def __init__(self):
        self.secret = config.JWT_SECRET
        self.algorithm = config.JWT_ALGORITHM
        self.expire_minutes = config.JWT_EXPIRE_MINUTES
        if self.secret == "foodagent-dev-secret-change-me":
            logger.warning("[Auth] 正在使用默认 JWT 密钥，生产环境请通过 .env 设置 JWT_SECRET")

    # ---------- 登录 ----------

    def authenticate(self, username: str, password: str) -> Optional[Dict[str, Any]]:
        """校验账号密码，成功返回账号信息，失败返回 None"""
        account = DEMO_ACCOUNTS.get(username)
        if not account:
            logger.warning(f"[Auth] 登录失败：账号不存在 {username}")
            return None
        expected = _hash_password(password, account["salt"])
        if not hmac.compare_digest(expected, account["password_hash"]):
            logger.warning(f"[Auth] 登录失败：密码错误 {username}")
            return None
        logger.info(f"[Auth] 登录成功：{username}（{account['role']} @ {account['tenant_id']}）")
        return account

    def create_token(self, account: Dict[str, Any]) -> str:
        """签发 JWT Token（含 tenant_id / user_id / role 三要素）"""
        now = datetime.now(timezone.utc)
        payload = {
            "sub": account["user_id"],
            "tenant_id": account["tenant_id"],
            "user_id": account["user_id"],
            "role": account["role"],
            "name": account.get("name", ""),
            "iat": now,
            "exp": now + timedelta(minutes=self.expire_minutes),
        }
        return jwt.encode(payload, self.secret, algorithm=self.algorithm)

    def decode_token(self, token: str) -> Dict[str, Any]:
        """校验并解析 Token，失败抛 HTTPException(401)"""
        try:
            return jwt.decode(token, self.secret, algorithms=[self.algorithm])
        except jwt.ExpiredSignatureError:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="登录已过期，请重新登录")
        except jwt.InvalidTokenError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail=f"无效的登录凭证：{exc}")

    # ---------- 依赖注入 ----------

    async def current_user(self, credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)) -> Dict[str, Any]:
        """
        FastAPI 依赖：从请求头解析当前用户。
        未认证时抛 401，由前端跳转登录页。
        """
        if credentials is None or not credentials.credentials:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="未提供登录凭证", headers={"WWW-Authenticate": "Bearer"})
        payload = self.decode_token(credentials.credentials)
        return {
            "tenant_id": payload.get("tenant_id", ""),
            "user_id": payload.get("user_id", ""),
            "role": payload.get("role", ""),
            "name": payload.get("name", ""),
        }

    def require_role(self, *roles: str):
        """依赖工厂：要求指定角色（用于后厨/店长专属接口）"""
        async def _checker(user: Dict[str, Any] = Depends(self.current_user)) -> Dict[str, Any]:
            if roles and user.get("role") not in roles:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"该操作需要 {'/'.join(roles)} 角色权限",
                )
            return user
        return _checker


auth_service = AuthService()


if __name__ == '__main__':
    print("=" * 60)
    print("JWT 认证自检")
    print("=" * 60)
    acc = auth_service.authenticate("waiter_01", "123456")
    print("登录结果：", "成功" if acc else "失败", "角色 =", acc.get("role") if acc else None)
    if acc:
        token = auth_service.create_token(acc)
        print("Token 前缀：", token[:40] + "...")
        print("解析结果：", auth_service.decode_token(token))
    # 错误密码
    print("错误密码：", auth_service.authenticate("waiter_01", "wrong"))
