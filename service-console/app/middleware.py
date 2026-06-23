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

import os

from fastapi import FastAPI, HTTPException
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.staticfiles import StaticFiles

from app.auth import require_session

# 受守前缀:仅 /api/** 走 default-deny。
API_PREFIX = "/api/"

# 白名单前缀(命中其一即放行;集中常量维护,便于审计)。
WHITELIST_PREFIXES: tuple[str, ...] = (
    "/auth/login",
    "/api/distribution/",
    "/health",
    # 并入的 hub 控制链路由(S2):这些端点走 hub 自带的 admin-token(X-Admin-Token)自校验,
    # 不走平台 JWT;放行后由各端点内的 _require_admin_token 把关(与 /api/distribution 同思路)。
    # 注:/api/agents 同时覆盖 /api/agents/{id}/logs/stream 与 /api/agents/{id}/commands。
    "/api/agents",
    "/api/commands",
    "/api/rolling-restart",
    "/api/service-rolling",
)


def _is_whitelisted(path: str) -> bool:
    return any(path.startswith(p) for p in WHITELIST_PREFIXES)


# ── CSP / 安全响应头(Task 7,Step 2b)─────────────────────────────────────────────
# 本控制台是对全机群有 dispatch=RCE 能力的运维 admin 会话,XSS 必须缓解(与 token 存哪
# 正交:注入脚本可复用已登录的 api client)。给**所有**响应注入下列头。
#
# CSP 取舍(既要严、又不能弄崩 antd + Vite 产物的 SPA):
# - `script-src 'self'`:**不放** unsafe-inline / unsafe-eval —— Vite 产物为外链 module JS;
#   并已在 vite.config.ts 关掉 modulepreload polyfill,使 index.html 零内联脚本,故严格策略可行。
# - `style-src 'self' 'unsafe-inline'`:antd 是 CSS-in-JS(运行时注入 <style>),**必须**放开
#   内联样式,否则组件样式全崩。
# - `img-src 'self' data:` / `font-src 'self' data:`:antd 图标 / 字体可能用 data: URI。
# - `connect-src 'self'`:XHR/fetch 同源(api client baseURL='/')。
# - `object-src 'none'` / `base-uri 'self'` / `frame-ancestors 'none'`:额外收紧(禁插件、禁
#   篡改 base、禁被嵌入,与 X-Frame-Options: DENY 双保险)。
# - `form-action 'self'`(评审 C4):登录表单纵深——限制表单只能 POST 回同源,防注入脚本把
#   凭据表单 action 改指向外站(CSP frame-ancestors/base-uri 之外再补一道表单出站约束)。
CSP_POLICY = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "font-src 'self' data:",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "frame-ancestors 'none'",
        "form-action 'self'",
    )
)

# 安全头集中常量(便于审计 / 测试断言)。用 setdefault 注入:不覆盖下游已显式设置的同名头。
SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": CSP_POLICY,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """给所有响应注入 CSP + 安全头(XSS / clickjacking / MIME-sniff 缓解)。

    注册在 SessionGuard **之后**(故在最外层),保证连 401 / 静态 / 异常响应也带头。
    用 setdefault 语义:已显式设置的同名头不被覆盖(预留下游特例)。
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        return response


class SessionGuardMiddleware(BaseHTTPMiddleware):
    """对 `/api/**`(白名单外)强制校验 Bearer JWT;失败 401。"""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if path.startswith(API_PREFIX) and not _is_whitelisted(path):
            # ⚠️ 仅鉴权判定(token 提取 + 校验)包进 try;**call_next 必须在 try 之外**,
            #    否则会把下游路由的正常异常/HTTPException 吞成 401,掩盖真实 500。
            try:
                # 复用 require_session 的解析(缺/坏/空-sub → HTTPException 401)。
                require_session(authorization=request.headers.get("authorization"))
            except HTTPException as exc:
                # BaseHTTPMiddleware 在 exception handler 链之外,须自行兜底成 JSON 401。
                return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
            except Exception:
                # 纵深防御(default-deny):鉴权判定自身抛任何意外异常一律 fail-closed 401,
                # 绝不意外落到 call_next 放行路径。
                return JSONResponse(
                    status_code=401, content={"detail": "unauthorized"}
                )
        return await call_next(request)


# ── SPA 兜底托管(Task 7,Step 2)──────────────────────────────────────────────────
# 不把 StaticFiles 挂在 "/"(那是一个 catch-all Mount,会抢在**运行期新增**的 /api 路由
# 之前——例如测试用 `app.add_api_route("/api/__probe__", ...)`,以及任何未匹配的 /api/*
# 都会被静态 404 接管,违背「StaticFiles 不得吞 API」)。改为**fallback 中间件**:先让
# 路由正常处理,仅当响应 404 且为安全 GET/HEAD 时,才尝试回前端产物。
#
# 关键收紧:**只回真实存在的静态文件**(如 /assets/index-xxx.js、/favicon.ico)+ 根路径 `/`
# 回 index.html;其余未命中路径(/docs、/openapi.json、/任意)一律保持原始 404,**不**做
# history fallback。本 SPA 用 hash 路由(createHashRouter),深链/刷新形如 `/#/namespaces`,
# 服务端只需托管 `/` 与资源文件,无需为任意 path 回 index.html —— 这也避免了把后端的 /docs、
# /openapi.json 等 404 误吞成 SPA 200。
#
# call_next 必须在 try 之外:下游异常须原样冒泡(不被吞);仅对「正常返回的 404」做兜底替换。

# SPA **绝不**接管的后端前缀:这些前缀下的 404 必须保持后端语义(JSON 404),不进静态查找。
SPA_EXCLUDED_PREFIXES: tuple[str, ...] = ("/api", "/auth", "/health")


class SPAFallbackMiddleware(BaseHTTPMiddleware):
    """路由未命中(404)时,为前端 SPA 回**已存在的**静态产物(资源文件 / 根 index.html)。

    仅兜底安全 GET/HEAD;后端前缀(/api、/auth、/health)的 404 原样透传;只服务真实存在的
    文件 + 根 `/`,其余 404 原样透传;下游异常原样冒泡。API 路由(含运行期新增)始终优先
    (本类是中间件,不占路由槽)。
    """

    def __init__(self, app, static_dir: str) -> None:  # noqa: ANN001 (Starlette app 类型宽松)
        super().__init__(app)
        self._static_dir = static_dir
        self._index = os.path.join(static_dir, "index.html")
        # 复用 StaticFiles 做带 content-type / 防穿越的资源查找(不挂为路由,仅借其 get_response)。
        self._static = StaticFiles(directory=static_dir)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)  # ⚠️ 在 try 之外:下游异常原样冒泡
        if response.status_code != 404 or request.method not in ("GET", "HEAD"):
            return response

        path = request.url.path
        # 后端前缀下的 404 保持原样(JSON 404),不回 SPA、不进静态查找。
        if any(path == p or path.startswith(p + "/") for p in SPA_EXCLUDED_PREFIXES):
            return response

        # 根路径 → index.html(SPA 壳)。
        if path == "/" and os.path.isfile(self._index):
            return FileResponse(self._index)

        # 其余:仅当**真实存在**对应静态文件时才服务(如 /assets/*、/favicon.ico);
        # 否则保持原始 404(不把 /docs、/openapi.json、未知 path 误吞成 SPA)。
        # ⚠️ StaticFiles.get_response 对缺失/穿越抛 **starlette** 的 HTTPException(非 fastapi 子类),
        #    须按 StarletteHTTPException 捕获,否则会逃逸成 500。
        rel = path.lstrip("/")
        if rel:
            try:
                return await self._static.get_response(rel, request.scope)
            except StarletteHTTPException:
                pass  # 文件不存在/穿越 → 落回原始 404
        return response


def mount_spa(app: FastAPI, static_dir: str) -> bool:
    """若 `static_dir` 存在(有前端构建产物),启用 SPA fallback 托管并返回 True;否则 False。

    设计要点见 `SPAFallbackMiddleware`:**绝不**把 StaticFiles 挂为 "/" catch-all,避免吞 API;
    且只回真实存在的文件 + 根 index.html,不吞 /docs、/openapi.json 等后端 404。
    本中间件应**最先** add(位于中间件链最内层、贴着 router),使 API 路由始终优先;
    SecurityHeaders 最后 add(最外层),故 SPA 静态响应也会被注入安全头(见 main.py 注册顺序)。
    """
    if not os.path.isdir(static_dir):
        return False
    app.add_middleware(SPAFallbackMiddleware, static_dir=static_dir)
    return True
