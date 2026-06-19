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
import httpx
import pytest
from app import store
from app.db_models import Namespace
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


@pytest.mark.parametrize(
    "bad_code",
    [
        "x/../../dispatch",  # 路径穿越(评审 A3 实测打到 hub dispatch)
        "ns/with/slash",
        "ns#frag",
        "ns?query=1",
        "..",
        "ns with space",
        "",  # 空串
    ],
)
def test_namespace_create_rejects_illegal_code(client: TestClient, monkeypatch, bad_code: str) -> None:
    # 评审 A3:NamespaceIn.code 白名单 ^[A-Za-z0-9._-]{1,255}$;非法 code create 即被拒(422),
    # 且**不落库**(校验在入 store 之前,杜绝路径注入的 namespace.code 进 hub URL)。
    # 打桩 provision 以隔离 hub:若校验未生效而误落库,provision 桩会让它 201,从而暴露漏网。
    calls: list[str] = []
    monkeypatch.setattr(hc, "provision_agent", lambda code: calls.append(code) or "k")
    h = _h(client)

    r = client.post("/api/namespaces", json={"code": bad_code}, headers=h)
    assert r.status_code == 422, r.text  # Pydantic 校验失败 → 422
    assert calls == []  # 非法 code 在校验阶段即拒,不触达 provision
    # 不落库:库内无该 code 行
    rows, _ = store.list_rows(Namespace, page=1, page_size=200)
    assert all(row.code != bad_code for row in rows)


def test_namespace_create_accepts_legal_code(client: TestClient, monkeypatch) -> None:
    # 白名单放行合法 code(含点/下划线/连字符,契合 @namespace/plugin 之外的纯 code 形态)。
    monkeypatch.setattr(hc, "provision_agent", lambda code: "k")
    h = _h(client)
    r = client.post("/api/namespaces", json={"code": "ns.legal_code-1"}, headers=h)
    assert r.status_code == 201, r.text
    assert r.json()["code"] == "ns.legal_code-1"


def test_namespace_create_rolls_back_on_hub_failure(client: TestClient, monkeypatch) -> None:
    # 评审 A14:create 先落库再 provision;hub 失败必须**补偿删除**刚建的行(整体原子失败),
    # 否则遗留无 agentKey 的孤儿 namespace(show-once 永久丢)。
    # 断言:provision 抛 HubError → 端点 5xx **且** namespace 行数为 0(无孤儿)。
    before, before_count = store.list_rows(Namespace, page=1, page_size=200)
    assert before_count == 0

    def boom(code: str) -> str:
        raise hc.HubError("hub 模拟不可达")

    monkeypatch.setattr(hc, "provision_agent", boom)
    h = _h(client)
    r = client.post("/api/namespaces", json={"code": "orphan-ns"}, headers=h)
    assert r.status_code >= 500, r.text  # 整体失败(A13 映射为 502/503)

    after, after_count = store.list_rows(Namespace, page=1, page_size=200)
    assert after_count == 0  # 补偿删除生效:无孤儿 namespace 行残留


def test_namespace_create_maps_httpx_error_to_503_without_leaking_url(client: TestClient, monkeypatch) -> None:
    # 评审 A13:hub 传输层异常(httpx.HTTPError)→ 503,且 detail 用稳定中文文案,
    # **绝不回显 hub URL / 底层异常细节**(异常 str 里塞内部地址,断言它不出现在响应体里)。
    secret_url = "http://internal-hub.local:9999/api/agents"

    def boom(code: str) -> str:
        raise httpx.ConnectError(f"connection refused to {secret_url}")

    monkeypatch.setattr(hc, "provision_agent", boom)
    r = client.post("/api/namespaces", json={"code": "ns-503"}, headers=_h(client))
    assert r.status_code == 503, r.text  # 传输层失败映射 503
    assert secret_url not in r.text  # 不回显内部 hub URL
    assert "internal-hub.local" not in r.text  # 主机名亦不泄露
    # 补偿删除照样生效:传输层失败同样不留孤儿行
    rows, _ = store.list_rows(Namespace, page=1, page_size=200)
    assert all(row.code != "ns-503" for row in rows)
