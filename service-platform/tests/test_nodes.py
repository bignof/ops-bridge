"""节点聚合端点 /api/nodes 端到端测试(Task 9b)。

经 conftest 的 `client` fixture(临时文件库 + swap 单例 + 置空 service_hub_url)。
节点页**权威源 = 平台 Service 表**(每行 = (agent×service)),叠加 hub 实时在线态
(`list_agents`)与健康实例数(`list_instances`)。核心不变式:

- **单 agent 卡死/超时不拖垮整页**:`list_instances` 短超时 + `gather(return_exceptions=True)`
  + 该行 `degraded=True、healthyCount=None`,其它行照常、整体 **200**。
- **离线 agent**(不在 `list_agents` 返回里)→ `online=False`、不发 `list_instances`、
  `healthyCount=None`、`degraded=False`。
- **无 nacosServiceName 的行** → 不发 `list_instances`、`healthyCount=None`、`degraded=False`。
- **`list_agents` 整体失败** → 仍 200,所有行 `online=False`(不崩整页)。
- 无 Bearer → default-deny 中间件 / `require_session` 401。

打桩纪律:路由经**模块引用** `hub_client.list_agents(...)` / `hub_client.list_instances(...)`
调用,故 `monkeypatch.setattr(nodes.hub_client, "list_agents", ...)`(在路由模块所引的
`hub_client` 上替换)即生效——同 `test_namespace_rotate.py` 的 H7 打桩范式。
"""

from __future__ import annotations

import httpx
import app.hub_client as hc
import app.routers.nodes as nodes
from app import store
from app.db_models import Namespace, Service
from fastapi.testclient import TestClient


def _h(client: TestClient) -> dict[str, str]:
    """登录拿 JWT,组装 Authorization 头。"""
    token = client.post("/auth/login", json={"username": "admin", "password": "admin-pw"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _mk_ns(code: str) -> int:
    """直接经 store 落一条 namespace(绕过 hub provision),返回 id。"""
    return store.create_row(Namespace, {"code": code, "name": code}).id


def _mk_svc(namespace_id: int, service_code: str, *, nacos: str | None = None) -> int:
    """直接经 store 落一条 service,返回 id。"""
    return store.create_row(
        Service,
        {
            "namespace_id": namespace_id,
            "service_code": service_code,
            "dir": f"/opt/{service_code}",
            "default_image": f"registry/{service_code}:1.0",
            "nacos_service_name": nacos,
        },
    ).id


def _agent(agent_id: str, *, online: bool = True, last_seen: str = "2026-06-21T00:00:00Z") -> dict:
    """造一条 hub AgentSnapshot 风格的 camelCase dict(只含路由读取的字段)。"""
    return {"agentId": agent_id, "online": online, "lastSeenAt": last_seen}


def _instances(*healthy_flags: bool) -> dict:
    """造一条 list_instances 成功响应:instances 数组按 healthy 标记生成。"""
    return {
        "status": "success",
        "instances": [{"address": f"10.0.0.{i}", "healthy": h} for i, h in enumerate(healthy_flags)],
    }


def test_nodes_basic_rows_and_fields(client: TestClient, monkeypatch) -> None:
    # 基本:2 个 service 行 → /api/nodes 返 2 行,字段齐(agentId/serviceCode/dir/defaultImage/nacosServiceName/online/healthyCount)。
    h = _h(client)
    ns = _mk_ns("agent-x")
    _mk_svc(ns, "svc-a", nacos="svc-a-nacos")
    _mk_svc(ns, "svc-b", nacos="svc-b-nacos")

    monkeypatch.setattr(nodes.hub_client, "list_agents", lambda: [_agent("agent-x")])
    # 两个 service 各回不同健康实例数。
    counts = {"svc-a-nacos": _instances(True, True, False), "svc-b-nacos": _instances(True)}
    monkeypatch.setattr(
        nodes.hub_client, "list_instances", lambda agent_id, service_name, **kw: counts[service_name]
    )

    body = client.get("/api/nodes", headers=h).json()
    assert body["count"] == 2
    rows = {r["serviceCode"]: r for r in body["rows"]}
    a = rows["svc-a"]
    assert a["agentId"] == "agent-x"
    assert a["namespaceCode"] == "agent-x"
    assert a["dir"] == "/opt/svc-a"
    assert a["defaultImage"] == "registry/svc-a:1.0"
    assert a["nacosServiceName"] == "svc-a-nacos"
    assert a["online"] is True
    assert a["healthyCount"] == 2  # 3 实例中 2 healthy
    assert a["degraded"] is False
    assert a["lastSeen"]  # 来自 agent 的 lastSeenAt

    b = rows["svc-b"]
    assert b["healthyCount"] == 1
    assert b["degraded"] is False
    # 列表信封字段齐
    assert body["page"] == 1 and body["pageSize"] == 20 and "totalPage" in body
    # 响应无 snake key(camel 契约钉死在 HTTP 层)
    for snake in ("service_code", "nacos_service_name", "default_image", "healthy_count"):
        assert snake not in a


def test_nodes_degraded_when_one_agent_times_out(client: TestClient, monkeypatch) -> None:
    # 降级(核心):行 A 的 list_instances 抛超时 → 行 A degraded==true && healthyCount==null,
    # 行 B 正常 degraded==false && healthyCount==N,整体 200(不 500、不阻塞)。
    h = _h(client)
    ns = _mk_ns("agent-deg")
    _mk_svc(ns, "svc-slow", nacos="slow-nacos")
    _mk_svc(ns, "svc-ok", nacos="ok-nacos")

    monkeypatch.setattr(nodes.hub_client, "list_agents", lambda: [_agent("agent-deg")])

    def fake_list_instances(agent_id, service_name, **kw):
        if service_name == "slow-nacos":
            raise httpx.TimeoutException("simulated timeout")  # 单 agent/服务卡死
        return _instances(True, True)

    monkeypatch.setattr(nodes.hub_client, "list_instances", fake_list_instances)

    resp = client.get("/api/nodes", headers=h)
    assert resp.status_code == 200  # 超时不阻塞整页
    rows = {r["serviceCode"]: r for r in resp.json()["rows"]}
    slow = rows["svc-slow"]
    assert slow["degraded"] is True
    assert slow["healthyCount"] is None
    ok = rows["svc-ok"]
    assert ok["degraded"] is False
    assert ok["healthyCount"] == 2


def test_nodes_degraded_on_generic_exception(client: TestClient, monkeypatch) -> None:
    # 任意 Exception(非超时,如 HubError / HTTP 错)同样 → 该行 degraded、healthyCount=null、整体 200。
    h = _h(client)
    ns = _mk_ns("agent-err")
    _mk_svc(ns, "svc-boom", nacos="boom-nacos")

    monkeypatch.setattr(nodes.hub_client, "list_agents", lambda: [_agent("agent-err")])

    def boom(agent_id, service_name, **kw):
        raise hc.HubError("hub 不可用")

    monkeypatch.setattr(nodes.hub_client, "list_instances", boom)

    resp = client.get("/api/nodes", headers=h)
    assert resp.status_code == 200
    row = resp.json()["rows"][0]
    assert row["degraded"] is True
    assert row["healthyCount"] is None


def test_nodes_offline_agent_not_fanned_out(client: TestClient, monkeypatch) -> None:
    # 离线 agent(不在 list_agents map)→ online=false、healthyCount=null、degraded=false、不调 list_instances。
    h = _h(client)
    ns = _mk_ns("agent-off")
    _mk_svc(ns, "svc-off", nacos="off-nacos")

    # list_agents 返回该 agent 但 online=False(也覆盖「在 map 里但离线」)。
    monkeypatch.setattr(nodes.hub_client, "list_agents", lambda: [_agent("agent-off", online=False)])

    called = {"hit": False}

    def must_not_call(*a, **k):
        called["hit"] = True
        return _instances(True)

    monkeypatch.setattr(nodes.hub_client, "list_instances", must_not_call)

    row = client.get("/api/nodes", headers=h).json()["rows"][0]
    assert row["online"] is False
    assert row["healthyCount"] is None
    assert row["degraded"] is False
    assert called["hit"] is False  # 离线行不发 fan-out


def test_nodes_agent_absent_from_map_is_offline(client: TestClient, monkeypatch) -> None:
    # agent 完全不在 list_agents 返回里 → online=false、不 fan-out。
    h = _h(client)
    ns = _mk_ns("agent-ghost")
    _mk_svc(ns, "svc-ghost", nacos="ghost-nacos")

    monkeypatch.setattr(nodes.hub_client, "list_agents", lambda: [])  # 空在线表
    called = {"hit": False}
    monkeypatch.setattr(
        nodes.hub_client, "list_instances", lambda *a, **k: called.__setitem__("hit", True)
    )

    row = client.get("/api/nodes", headers=h).json()["rows"][0]
    assert row["online"] is False
    assert row["healthyCount"] is None
    assert row["degraded"] is False
    assert called["hit"] is False


def test_nodes_no_nacos_name_skips_fanout(client: TestClient, monkeypatch) -> None:
    # 无 nacosServiceName 的行 → 不调 list_instances、healthyCount=null、degraded=false(即便 agent 在线)。
    h = _h(client)
    ns = _mk_ns("agent-nonacos")
    _mk_svc(ns, "svc-nonacos", nacos=None)

    monkeypatch.setattr(nodes.hub_client, "list_agents", lambda: [_agent("agent-nonacos")])
    called = {"hit": False}
    monkeypatch.setattr(
        nodes.hub_client, "list_instances", lambda *a, **k: called.__setitem__("hit", True)
    )

    row = client.get("/api/nodes", headers=h).json()["rows"][0]
    assert row["online"] is True
    assert row["nacosServiceName"] is None
    assert row["healthyCount"] is None
    assert row["degraded"] is False
    assert called["hit"] is False  # 无 nacos 名不发 fan-out


def test_nodes_list_agents_failure_all_offline(client: TestClient, monkeypatch) -> None:
    # list_agents 整体失败 → 仍 200,所有行 online=false、不崩(map 退化为空)。
    h = _h(client)
    ns = _mk_ns("agent-hubdown")
    _mk_svc(ns, "svc-1", nacos="n1")
    _mk_svc(ns, "svc-2", nacos="n2")

    def boom():
        raise hc.HubError("hub 整体不可用")

    monkeypatch.setattr(nodes.hub_client, "list_agents", boom)
    called = {"hit": False}
    monkeypatch.setattr(
        nodes.hub_client, "list_instances", lambda *a, **k: called.__setitem__("hit", True)
    )

    resp = client.get("/api/nodes", headers=h)
    assert resp.status_code == 200  # list_agents 失败不崩整页
    rows = resp.json()["rows"]
    assert len(rows) == 2
    assert all(r["online"] is False for r in rows)
    assert all(r["healthyCount"] is None and r["degraded"] is False for r in rows)
    assert called["hit"] is False  # 全离线,无 fan-out


def test_nodes_status_non_success_is_degraded(client: TestClient, monkeypatch) -> None:
    # list_instances 返回 dict 但 status != success → 视为该行降级(healthyCount=null、degraded=true)。
    h = _h(client)
    ns = _mk_ns("agent-bad")
    _mk_svc(ns, "svc-bad", nacos="bad-nacos")

    monkeypatch.setattr(nodes.hub_client, "list_agents", lambda: [_agent("agent-bad")])
    monkeypatch.setattr(
        nodes.hub_client,
        "list_instances",
        lambda *a, **k: {"status": "error", "instances": []},
    )

    row = client.get("/api/nodes", headers=h).json()["rows"][0]
    assert row["degraded"] is True
    assert row["healthyCount"] is None


def test_nodes_pagination(client: TestClient, monkeypatch) -> None:
    # 分页:pageSize=1 → 每页 1 行,count=总数,totalPage 正确。
    h = _h(client)
    ns = _mk_ns("agent-pg")
    _mk_svc(ns, "svc-p1", nacos="p1")
    _mk_svc(ns, "svc-p2", nacos="p2")

    monkeypatch.setattr(nodes.hub_client, "list_agents", lambda: [_agent("agent-pg")])
    monkeypatch.setattr(nodes.hub_client, "list_instances", lambda *a, **k: _instances(True))

    body = client.get("/api/nodes?page=1&pageSize=1", headers=h).json()
    assert body["count"] == 2
    assert len(body["rows"]) == 1
    assert body["pageSize"] == 1
    assert body["totalPage"] == 2


def test_nodes_requires_auth(client: TestClient) -> None:
    # 无 Bearer → default-deny 中间件 / require_session 401。
    assert client.get("/api/nodes").status_code == 401
