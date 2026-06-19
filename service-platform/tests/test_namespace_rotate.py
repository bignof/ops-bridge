"""命名空间 rotate-key / rotate-pull-token 端到端测试(Task 7)。

经 conftest 的 `client` fixture(临时文件库 + swap 单例 + 置空 service_hub_url)。两条
轮换端点均 **show-once**——明文仅本次响应可得,平台不落地明文:

- `rotate-key`:调 hub `rotate_agent_key` 取新 agentKey,**不入库**。必须打桩
  `hub_client.rotate_agent_key`(否则 service_hub_url 未配 → HubError → 500)。
  断言响应含 `agentKey`、库内查不到该明文。
- `rotate-pull-token`:本地 `tokens.new_pull_token()` 生成,**只把哈希写
  `namespace.pull_token_hash`**。断言响应含 `pullToken` 明文,重查行
  `pull_token_hash != 明文` 且 `verify_token(明文, hash) is True`(哈希入库、明文不入库)。
- 不存在的 namespace → 404;无 Bearer → default-deny 中间件 401。
"""

from __future__ import annotations

import app.hub_client as hc
from app import store, tokens
from app.db_models import Namespace
from fastapi.testclient import TestClient


def _h(client: TestClient) -> dict[str, str]:
    """登录拿 JWT,组装 Authorization 头。"""
    token = client.post("/auth/login", json={"username": "admin", "password": "admin-pw"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _new_ns(client: TestClient, monkeypatch, code: str = "ns-rot") -> int:
    """建一个 namespace 并回 id;create 需打桩 provision_agent(见 Task 6b)。"""
    monkeypatch.setattr(hc, "provision_agent", lambda c: "prov-key")
    r = client.post("/api/namespaces", json={"code": code}, headers=_h(client))
    assert r.status_code == 201
    return r.json()["id"]


def test_rotate_pull_token_returns_once_and_stores_hash(client: TestClient, monkeypatch) -> None:
    nid = _new_ns(client, monkeypatch, code="ns-pt")
    h = _h(client)

    r = client.post(f"/api/namespaces/{nid}/rotate-pull-token", headers=h)
    assert r.status_code == 200
    plain = r.json()["pullToken"]  # show-once 明文
    assert plain and len(plain) >= 32

    # 重查该行:库内只存哈希,明文不落地(评审 Nit-1 同款 show-once 不变式)。
    row = store.get_row(Namespace, nid)
    assert row.pull_token_hash and row.pull_token_hash != plain
    assert tokens.verify_token(plain, row.pull_token_hash) is True


def test_rotate_pull_token_replaces_previous(client: TestClient, monkeypatch) -> None:
    # 再次轮换应换新明文与新哈希(旧的失效);响应不重复携带上一次的明文。
    nid = _new_ns(client, monkeypatch, code="ns-pt2")
    h = _h(client)

    first = client.post(f"/api/namespaces/{nid}/rotate-pull-token", headers=h).json()["pullToken"]
    first_hash = store.get_row(Namespace, nid).pull_token_hash

    second = client.post(f"/api/namespaces/{nid}/rotate-pull-token", headers=h).json()["pullToken"]
    second_hash = store.get_row(Namespace, nid).pull_token_hash

    assert second != first
    assert second_hash != first_hash
    assert tokens.verify_token(second, second_hash) is True
    assert tokens.verify_token(first, second_hash) is False  # 旧明文对新哈希不再通过


def test_rotate_key_returns_once_and_not_stored(client: TestClient, monkeypatch) -> None:
    nid = _new_ns(client, monkeypatch, code="ns-rk")
    # rotate-key 必须打桩(否则 service_hub_url 未配 → HubError → 500)。
    monkeypatch.setattr(hc, "rotate_agent_key", lambda code: "rotated-key")
    h = _h(client)

    r = client.post(f"/api/namespaces/{nid}/rotate-key", headers=h)
    assert r.status_code == 200
    assert r.json()["agentKey"] == "rotated-key"  # show-once 返回明文

    # 库内无该 agentKey 明文(不入库,守 show-once 不变式)。
    row = store.get_row(Namespace, nid)
    assert "rotated-key" not in str(
        {"code": row.code, "name": row.name, "pull_token_hash": row.pull_token_hash}
    )


def test_rotate_key_passes_code_to_hub(client: TestClient, monkeypatch) -> None:
    # 断言传给 hub 的是 namespace.code(=agentId),非别名 name。
    nid = _new_ns(client, monkeypatch, code="agent-rot")
    seen: dict = {}

    def fake_rotate(code: str) -> str:
        seen["code"] = code
        return "k2"

    monkeypatch.setattr(hc, "rotate_agent_key", fake_rotate)
    r = client.post(f"/api/namespaces/{nid}/rotate-key", headers=_h(client))
    assert r.status_code == 200
    assert seen["code"] == "agent-rot"


def test_rotate_key_missing_namespace_404(client: TestClient, monkeypatch) -> None:
    # 即便打桩,namespace 不存在也应 404(且不调 hub)。
    monkeypatch.setattr(hc, "rotate_agent_key", lambda code: "k")
    assert client.post("/api/namespaces/999999/rotate-key", headers=_h(client)).status_code == 404


def test_rotate_pull_token_missing_namespace_404(client: TestClient) -> None:
    assert client.post("/api/namespaces/999999/rotate-pull-token", headers=_h(client)).status_code == 404


def test_rotate_requires_auth(client: TestClient) -> None:
    # 无 Bearer → default-deny 中间件 401(两条端点都覆盖)。
    assert client.post("/api/namespaces/1/rotate-key").status_code == 401
    assert client.post("/api/namespaces/1/rotate-pull-token").status_code == 401
