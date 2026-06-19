"""单 admin 鉴权:env 凭据校验 + JWT 签发 + FastAPI 会话依赖。

绑定约束:人类会话 = `Authorization: Bearer <JWT>`(不用 cookie),单 admin、
env 凭据、**常量时间比较**(`hmac.compare_digest`,防时序侧信道)。范式参照
`service-hub/app/api_support.py:_require_admin_token`(常量时间 / None 短路)。

PyJWT==2.10.1:空密钥时 `encode`/`decode` 抛 `InvalidKeyError`(fail-closed);
另在 `app/main.py` lifespan 兜底拒绝空 / 过短 `jwt_secret` 启动(纵深防御)。
"""

from __future__ import annotations

import hmac
import time

import jwt
from fastapi import Header, HTTPException, status

from app.config import settings


def verify_login(user: str, pw: str) -> bool:
    """常量时间校验单 admin 凭据;未配置 env 凭据时一律拒绝(fail-closed)。"""
    if not settings.admin_user or not settings.admin_password:
        return False
    # 两个比较都跑,避免短路泄漏「用户名是否正确」的时序信息。
    user_ok = hmac.compare_digest(user, settings.admin_user)
    pw_ok = hmac.compare_digest(pw, settings.admin_password)
    return user_ok and pw_ok


def issue_token(sub: str) -> str:
    """签发 HS256 JWT,exp = 现在 + jwt_ttl_seconds。"""
    now = int(time.time())
    payload = {"sub": sub, "iat": now, "exp": now + settings.jwt_ttl_seconds}
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def require_session(authorization: str | None = Header(default=None)) -> str:
    """FastAPI 依赖:解析 `Authorization: Bearer <JWT>`,校验失败一律 401,成功返回 sub。

    评审 Nit-2:`options={"require": ["sub", "exp"]}` 强制存在;缺字段触发
    `MissingRequiredClaimError`(PyJWTError 子类)→ 401 而非 KeyError→500;
    再加 `payload.get("sub")` 为空的兜底 401。
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    token = authorization[len("Bearer "):]
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=["HS256"],
            options={"require": ["sub", "exp"]},
        )
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")
    sub = payload.get("sub")
    if not sub:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")
    return sub
