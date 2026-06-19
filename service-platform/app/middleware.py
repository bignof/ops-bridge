"""default-deny 会话守卫中间件(评审 H6,spec L100)。

绑定约束:HTTP 中间件对 `/api/**` 统一 **default-deny**(强制校验 `Authorization:
Bearer <JWT>`),而非仅靠逐路由 `Depends(require_session)`。service-hub 的读端点
(`GET /api/agents` 等)正因只在写端点挂 token、读端点裸奔而 fail-open——本平台
**不复制该 bug**。逐路由 `Depends(require_session)` **保留作纵深防御**(双层):
中间件先把无/坏 JWT 的 `/api/**` 请求挡在外面,放行后端点的 Depends 再校验一次。

白名单(**前缀**匹配,审计可见,新增公开 /api 端点须显式加入):
- `/auth/login`、`/health` —— 本就不在 `/api/` 前缀下,中间件天然不拦(列入仅
  为意图清晰 + 防将来误把它们挪到 /api 下时静默失守)。
- `/api/distribution/` —— 在 `/api/` 下,显式放行;鉴权改由各 distribution 端点
  内的 pull token 自校验(Task 11),中间件不越俎代庖。

复用 `app.auth.require_session` 的底层解析:它对缺/坏/空-sub 一律抛
`HTTPException(401)`,本中间件捕获后转成 `JSONResponse(401)`(BaseHTTPMiddleware
不在 FastAPI 的 exception handler 链上,须自行兜底,否则会冒泡成 500)。
"""

from __future__ import annotations

from fastapi import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.auth import require_session

# 受守前缀:仅 /api/** 走 default-deny。
API_PREFIX = "/api/"

# 白名单前缀(命中其一即放行;集中常量维护,便于审计)。
WHITELIST_PREFIXES: tuple[str, ...] = (
    "/auth/login",
    "/api/distribution/",
    "/health",
)


def _is_whitelisted(path: str) -> bool:
    return any(path.startswith(p) for p in WHITELIST_PREFIXES)


class SessionGuardMiddleware(BaseHTTPMiddleware):
    """对 `/api/**`(白名单外)强制校验 Bearer JWT;失败 401。"""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if path.startswith(API_PREFIX) and not _is_whitelisted(path):
            try:
                # 复用 require_session 的解析(缺/坏/空-sub → HTTPException 401)。
                require_session(authorization=request.headers.get("authorization"))
            except HTTPException as exc:
                # BaseHTTPMiddleware 在 exception handler 链之外,须自行兜底成 JSON 401。
                return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        return await call_next(request)
