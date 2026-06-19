"""namespace 资源端到端 CRUD 测试(Task 6b)。

经 conftest 的 `client` fixture(临时文件库 + swap 单例 + 置空 service_hub_url)。验证点:
- 增删改查全链路;响应**全 camelCase**(经 `*Out` 序列化)。
- 列表信封形状 `{count, rows, page, pageSize, totalPage}`;`name` 空回退 `code`(评审 H3)。
- **create show-once(评审 H7 / Nit-1)**:hub `provision_agent` 打桩(否则 service_hub_url
  未配 → HubError → 500),响应含一次性 `agentKey` 明文;**重查行不含 agentKey 明文**(不入库)。
- 唯一约束冲突(`namespace.code` 重复)→ 409。
- `/api/namespaces` 在 default-deny 中间件下,无 Bearer → 401。
"""

from __future__ import annotations

import app.hub_client as hc
from fastapi.testclient import TestClient


def _h(client: TestClient) -> dict[str, str]:
    """登录拿 JWT,组装 Authorization 头。"""
    token = client.post("/auth/login", json={"username": "admin", "password": "admin-pw"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_namespace_create_provisions_and_shows_key_once(client: TestClient, monkeypatch) -> None:
    # 评审 H7:不打桩则 service_hub_url 未配(conftest 置空)→ HubError → 500,断言 201 必红。
    monkeypatch.setattr(hc, "provision_agent", lambda code: "fake-key")
    h = _h(client)

    r = client.post("/api/namespaces", json={"code": "ns1", "name": "NS1"}, headers=h)
    assert r.status_code == 201
    assert r.json()["agentKey"] == "fake-key"  # show-once 返回明文
    assert r.json()["code"] == "ns1"
    assert r.json()["name"] == "NS1"
    nid = r.json()["id"]

    # 评审 Nit-1:重查该行,断言不含 agentKey 明文(库内无该列/不落地,守 show-once 不变式)。
    got = client.get(f"/api/namespaces/{nid}", headers=h).json()
    assert "agentKey" not in got and "fake-key" not in str(got)


def test_namespace_create_passes_code_to_provision(client: TestClient, monkeypatch) -> None:
    # 断言 provision_agent 收到的是 code(=agentId),非别名 name。
    seen: dict = {}

    def fake_provision(code: str) -> str:
        seen["code"] = code
        return "k-abc"

    monkeypatch.setattr(hc, "provision_agent", fake_provision)
    h = _h(client)
    r = client.post("/api/namespaces", json={"code": "agent-x", "name": "别名"}, headers=h)
    assert r.status_code == 201
    assert seen["code"] == "agent-x"


def test_namespace_crud(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(hc, "provision_agent", lambda code: "fake-key")
    h = _h(client)

    r = client.post("/api/namespaces", json={"code": "ns-crud", "name": "N"}, headers=h)
    assert r.status_code == 201
    nid = r.json()["id"]

    # list → 信封形状 + 分页字段
    body = client.get("/api/namespaces", headers=h).json()
    assert body["count"] >= 1
    assert "rows" in body and "totalPage" in body
    assert body["page"] == 1 and body["pageSize"] == 20
    assert any(row["id"] == nid for row in body["rows"])

    # get 单条
    got = client.get(f"/api/namespaces/{nid}", headers=h)
    assert got.status_code == 200
    assert got.json()["code"] == "ns-crud"

    # patch 局部更新
    patched = client.patch(f"/api/namespaces/{nid}", json={"name": "N2"}, headers=h)
    assert patched.status_code == 200
    assert patched.json()["name"] == "N2"
    assert patched.json()["code"] == "ns-crud"  # 未传字段保持原值

    # delete → 204
    assert client.delete(f"/api/namespaces/{nid}", headers=h).status_code == 204
    assert client.get(f"/api/namespaces/{nid}", headers=h).status_code == 404


def test_namespace_name_falls_back_to_code(client: TestClient, monkeypatch) -> None:
    # 评审 H3:name 空时列表/查询回退 code。
    monkeypatch.setattr(hc, "provision_agent", lambda code: "k")
    h = _h(client)
    r = client.post("/api/namespaces", json={"code": "only-code"}, headers=h)
    assert r.status_code == 201
    nid = r.json()["id"]
    assert r.json()["name"] == "only-code"  # create 响应即回退
    got = client.get(f"/api/namespaces/{nid}", headers=h).json()
    assert got["name"] == "only-code"


def test_namespace_unique_conflict(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(hc, "provision_agent", lambda code: "k")
    h = _h(client)
    first = client.post("/api/namespaces", json={"code": "dup-ns"}, headers=h)
    assert first.status_code == 201
    # 同 code 再 create → 唯一约束 → 409
    assert client.post("/api/namespaces", json={"code": "dup-ns"}, headers=h).status_code == 409


def test_namespace_get_missing_404(client: TestClient) -> None:
    h = _h(client)
    assert client.get("/api/namespaces/999999", headers=h).status_code == 404


def test_namespace_requires_auth(client: TestClient) -> None:
    # 无 Bearer → default-deny 中间件 401
    assert client.get("/api/namespaces").status_code == 401
    assert client.post("/api/namespaces", json={"code": "no-auth"}).status_code == 401
