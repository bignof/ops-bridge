"""default-deny 中间件测试(Task 3.5,评审 H6)。

核心命题:对 `/api/**` 的 default-deny **由中间件守住**,而非靠逐路由
`Depends(require_session)`。为证明这点,测试动态挂一个**不带任何 Depends**的
占位 `/api/__probe__` 路由——若中间件失效(只剩 Depends),它会裸奔返回 200;
有中间件时无 token 应被 401 拦下。

白名单前缀(`/auth/login`、`/api/distribution/`、`/health`)放行:
- `/health`、`/auth/login` 天然不在 `/api/` 前缀下,中间件根本不拦(顺带回归)。
- `/api/distribution/**` 在 `/api/` 下,须**显式**白名单放行(交给端点内 pull
  token 自校验);此时无路由可能 404,但**不是** 401。

一律经 conftest 的 `client` fixture(隔离临时库;env 凭据 admin/admin-pw)。
"""

from __future__ import annotations

from typing import Iterator

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client_with_probe(client: TestClient) -> Iterator[TestClient]:
    """在共享 app 上临时挂一个**无 Depends**的 `/api/__probe__`,用例后移除。

    占位路由证明:即便路由自己没有 `Depends(require_session)`,中间件仍守住
    `/api/**`。app 是模块级单例,故必须在用例结束后摘掉该路由,避免污染其它用例。
    """
    app = client.app

    async def _probe() -> dict:  # 故意不挂任何 Depends
        return {"ok": True}

    app.add_api_route("/api/__probe__", _probe, methods=["GET"])
    app.router.routes  # 触发路由表已构建(no-op,保险)
    try:
        yield client
    finally:
        app.router.routes[:] = [
            r for r in app.router.routes if getattr(r, "path", None) != "/api/__probe__"
        ]


def _token(client: TestClient) -> str:
    r = client.post("/auth/login", json={"username": "admin", "password": "admin-pw"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


# ① 占位 /api 路由无 Depends:无 token 仍 401(中间件守住,而非靠路由 Depends)
def test_probe_api_route_without_depends_still_401(client_with_probe: TestClient) -> None:
    r = client_with_probe.get("/api/__probe__")
    assert r.status_code == 401, r.text


# ① 补:占位 /api 路由带合法 JWT → 放行 200
def test_probe_api_route_with_valid_token_200(client_with_probe: TestClient) -> None:
    tok = _token(client_with_probe)
    r = client_with_probe.get("/api/__probe__", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200 and r.json() == {"ok": True}


# ① 补:坏 token → 401(中间件拦,非 500)
def test_probe_api_route_with_garbage_token_401(client_with_probe: TestClient) -> None:
    r = client_with_probe.get("/api/__probe__", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401, r.text


# ② 白名单:GET /health 无 token → 200(非 /api 前缀,中间件不拦)
def test_health_no_token_200(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


# ② 白名单:POST /auth/login 无 token → 可达(凭据错则 401,是端点拦截,非中间件)
def test_login_reachable_without_token(client: TestClient) -> None:
    # 正确凭据 → 200(证明中间件未拦 /auth/login)
    ok = client.post("/auth/login", json={"username": "admin", "password": "admin-pw"})
    assert ok.status_code == 200 and ok.json().get("token")


# ③ /api/distribution/** 无 JWT → 不被中间件 401 拦(放行给端点内 pull token)。
#    Task 11 已实现该端点:中间件放行后由端点内 pull token 自校验。关键命题仍是**不是
#    中间件 401**——带齐 query 参数但不带 token 时,落到端点 pull token 校验 → 403
#    (而非中间件 JWT 401),证明鉴权确实交给了端点而非中间件。
def test_distribution_prefix_not_blocked_by_middleware(client: TestClient) -> None:
    # 缺 query 参数时仅证明放行(非 401);带齐参数无 token 时落端点 pull token → 403。
    r_no_params = client.get("/api/distribution/plugins")
    assert r_no_params.status_code != 401, r_no_params.text  # 中间件放行(缺参 → 422)

    r = client.get("/api/distribution/plugins?namespace=x&service=y")
    assert r.status_code == 403, r.text  # 放行到端点,由 pull token 自校验拒绝(非中间件 401)
