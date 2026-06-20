"""plugin 资源端到端 CRUD 测试(Task 6a)。

经 conftest 的 `client` fixture(临时文件库 + swap 单例)。验证点:
- 增删改查全链路;响应**全 camelCase**(经 `PluginOut` / 列表信封序列化)。
- 列表信封形状 `{count, rows, page, pageSize, totalPage}`。
- 唯一约束冲突(`plugin.code` 重复)→ 409(store 捕 IntegrityError→Conflict→路由映射)。
- `/api/plugins` 在 default-deny 中间件下,无 Bearer → 401。
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def _h(client: TestClient) -> dict[str, str]:
    """登录拿 JWT,组装 Authorization 头。"""
    token = client.post("/auth/login", json={"username": "admin", "password": "admin-pw"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_plugin_crud(client: TestClient) -> None:
    h = _h(client)

    # create → 201,响应 camelCase
    r = client.post("/api/plugins", json={"code": "@business/plugin-x", "name": "X"}, headers=h)
    assert r.status_code == 201
    pid = r.json()["id"]
    assert r.json()["code"] == "@business/plugin-x"
    assert r.json()["name"] == "X"

    # list → 信封形状 + 分页字段
    body = client.get("/api/plugins", headers=h).json()
    assert body["count"] >= 1
    assert "rows" in body and "totalPage" in body
    assert body["page"] == 1 and body["pageSize"] == 20
    assert any(row["id"] == pid for row in body["rows"])

    # get 单条
    got = client.get(f"/api/plugins/{pid}", headers=h)
    assert got.status_code == 200
    assert got.json()["code"] == "@business/plugin-x"

    # patch 局部更新
    patched = client.patch(f"/api/plugins/{pid}", json={"name": "X2"}, headers=h)
    assert patched.status_code == 200
    assert patched.json()["name"] == "X2"
    assert patched.json()["code"] == "@business/plugin-x"  # 未传字段保持原值

    # delete → 204
    assert client.delete(f"/api/plugins/{pid}", headers=h).status_code == 204
    # 删后再查 404
    assert client.get(f"/api/plugins/{pid}", headers=h).status_code == 404


def test_plugin_unique_conflict(client: TestClient) -> None:
    h = _h(client)
    first = client.post("/api/plugins", json={"code": "dup"}, headers=h)
    assert first.status_code == 201
    # 同 code 再 create → 唯一约束 → 409
    assert client.post("/api/plugins", json={"code": "dup"}, headers=h).status_code == 409


def test_plugin_get_missing_404(client: TestClient) -> None:
    h = _h(client)
    assert client.get("/api/plugins/999999", headers=h).status_code == 404


def test_plugin_requires_auth(client: TestClient) -> None:
    # 无 Bearer → default-deny 中间件 401
    assert client.get("/api/plugins").status_code == 401
    assert client.post("/api/plugins", json={"code": "no-auth"}).status_code == 401
