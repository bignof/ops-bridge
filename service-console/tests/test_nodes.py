"""节点聚合端点 /api/nodes 端到端测试(Task 9b)。

经 conftest 的 `client` fixture(临时文件库 + swap 单例 + admin_token 测试值)。节点页**权威源
= 平台 Service 表**(每行 = (agent×service)),叠加 hub 实时在线态(`list_agents`)与健康实例数
(`list_instances`)。核心不变式:

- **单 agent 卡死/超时不拖垮整页**:`list_instances` 用 `asyncio.wait_for` 硬短超时 +
  `gather(return_exceptions=True)` + 该行 `degraded=True、healthyCount=None`,其它行照常、整体 **200**。
- **离线 agent**(不在 `list_agents` 返回里)→ `online=False`、不发 `list_instances`、
  `healthyCount=None`、`degraded=False`。
- **无 nacosServiceName 的行** → 不发 `list_instances`、`healthyCount=None`、`degraded=False`。
- **`list_agents` 整体失败** → 仍 200,所有行 `online=False`(不崩整页)。
- 无 Bearer → default-deny 中间件 / `require_session` 401。

打桩纪律:路由经**模块引用** `hub_client.list_agents(...)` / `hub_client.list_instances(...)`
调用,故 `monkeypatch.setattr(nodes.hub_client, "list_agents", ...)`(在路由模块所引的
`hub_client` 上替换)即生效。**S5:`hub_client.*` 已改进程内 async,替身一律须为 async**
(返回 coroutine);本文件统一用 `_aval(...)` / `_araise(...)` / `_acapture(...)` 等异步替身工厂,
不再用同步 lambda(否则 `await` 同步替身会 TypeError)。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import app.hub_client as hc
import app.routers.nodes as nodes
from app import store
from app.db_models import Namespace, Service
from app.hub.db_models import DiscoveredNodeModel
from fastapi.testclient import TestClient


# ── 异步替身工厂(S5):hub_client.* 已是 async,所有打桩须返回 coroutine ──


def _aval(value):
    """async 替身:被 await 时返回固定 value(忽略入参)。"""

    async def _stub(*a, **k):
        return value

    return _stub


def _araise(exc: Exception):
    """async 替身:被 await 时抛 exc。"""

    async def _stub(*a, **k):
        raise exc

    return _stub


def _afn(sync_fn):
    """把一个同步函数包成 async 替身:被 await 时以原入参调 sync_fn 并返回其结果。

    供需要按入参分支返回(如按 serviceName 选不同实例集)或记录入参的桩复用,既保留原同步逻辑
    的可读性,又满足 `await` 契约。
    """

    async def _stub(*a, **k):
        return sync_fn(*a, **k)

    return _stub


def _amust_not_call(message: str):
    """async 替身:一旦被调用即抛 AssertionError(断言某 hub 调用不应发生)。"""

    async def _stub(*a, **k):
        raise AssertionError(message)

    return _stub


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


def _mk_discovered(
    agent_id: str,
    nacos_service: str,
    *,
    container_name: str,
    dir_: str | None = None,
    image: str | None = None,
    container_id: str | None = None,
    compose_project: str | None = None,
    status: str = "active",
) -> int:
    """直接经 store 落一条 DiscoveredNode(模拟 agent 发现上报已落库),返回 id。

    P3-6「发现权威」:节点操作寻址优先取这里的 dir/image/containerId(权威),仅无发现行时回退
    Service 台账值。heartbeat_at / first_seen_at 是 NOT NULL 无默认列,须显式给(create_row 仅自动
    补 created_at/updated_at)。
    """
    now = datetime.now(timezone.utc)
    return store.create_row(
        DiscoveredNodeModel,
        {
            "agent_id": agent_id,
            "container_name": container_name,
            "container_id": container_id,
            "compose_project": compose_project,
            "compose_service": "app",
            "dir": dir_,
            "image": image,
            "running": True,
            "nacos_service": nacos_service,
            "healthy": True,
            "status": status,
            "heartbeat_at": now,
            "first_seen_at": now,
        },
    ).id


def _agent(agent_id: str, *, online: bool = True, last_seen: str = "2026-06-21T00:00:00Z") -> dict:
    """造一条 hub AgentSnapshot 风格的 camelCase dict(只含路由读取的字段)。"""
    return {"agentId": agent_id, "online": online, "lastSeenAt": last_seen}


def _instances(*healthy_flags: bool) -> dict:
    """造一条 list_instances 成功响应:instances 数组按 healthy 标记生成。

    instances 各项默认 `matched=True`(= 本机实例),供节点列表 healthyCount 用例使用
    (列表只看 healthy,matched 对其无影响)。优雅 drain 用例需要区分本机/别节点时,
    直接内联构造 instances 显式给 matched(见下方 stop/redeploy graceful 用例)。
    """
    return {
        "status": "success",
        "instances": [{"address": f"10.0.0.{i}", "healthy": h, "matched": True} for i, h in enumerate(healthy_flags)],
    }


def test_compose_default_project_normalization() -> None:
    # 评审 #11:Docker 默认 compose 工程名启发式 = basename → 小写 → 仅留 [a-z0-9_-] → 去前导非字母数字。
    f = nodes._compose_default_project
    assert f("/opt/My_Svc-01") == "my_svc-01"  # 大写转小写
    assert f("/opt/svc/") == "svc"  # 去尾部斜杠后取 basename
    assert f("C:\\deploy\\Worker_A") == "worker_a"  # Windows 分隔符
    assert f("/opt/a.b c!d") == "abcd"  # 剔除非 [a-z0-9_-](点/空格/感叹号)
    assert f("/opt/__lead") == "lead"  # 去前导非字母数字(下划线开头被剥到首个字母)
    assert f("/opt/-9x") == "9x"  # 前导连字符被剥,数字开头保留
    assert f(None) is None  # 空 dir → None(不带 expectedComposeProject)
    assert f("") is None
    assert f("/opt/___") is None  # 规范化后为空 → None


def test_nodes_basic_rows_and_fields(client: TestClient, monkeypatch) -> None:
    # 基本:2 个 service 行 → /api/nodes 返 2 行,字段齐(agentId/serviceCode/dir/defaultImage/nacosServiceName/online/healthyCount)。
    h = _h(client)
    ns = _mk_ns("agent-x")
    _mk_svc(ns, "svc-a", nacos="svc-a-nacos")
    _mk_svc(ns, "svc-b", nacos="svc-b-nacos")

    monkeypatch.setattr(nodes.hub_client, "list_agents", _aval([_agent("agent-x")]))
    # 两个 service 各回不同健康实例数。
    counts = {"svc-a-nacos": _instances(True, True, False), "svc-b-nacos": _instances(True)}
    monkeypatch.setattr(
        nodes.hub_client, "list_instances", _afn(lambda agent_id, service_name, **kw: counts[service_name])
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

    monkeypatch.setattr(nodes.hub_client, "list_agents", _aval([_agent("agent-deg")]))

    async def fake_list_instances(agent_id, service_name, **kw):
        if service_name == "slow-nacos":
            raise asyncio.TimeoutError("simulated timeout")  # 单 agent/服务卡死(进程内为 asyncio 超时)
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


def test_nodes_degraded_when_one_agent_hangs_past_wait_for(client: TestClient, monkeypatch) -> None:
    # S5 核心回归:进程内 await 无 socket 超时,单 agent 的 list_instances **永久挂起** 时,
    # 必须由 BFF fan-out 的 `asyncio.wait_for(coro, timeout)` 截断 → 该行 degraded,其余行 + 整页 200。
    # 用一个会睡远超 wait_for 阈值的桩模拟「WS call_agent 卡死」;把 _FANOUT_TIMEOUT_SEC 调到极小以免拖慢测试。
    monkeypatch.setattr(nodes, "_FANOUT_TIMEOUT_SEC", 0.05)
    h = _h(client)
    ns = _mk_ns("agent-hang")
    _mk_svc(ns, "svc-hang", nacos="hang-nacos")
    _mk_svc(ns, "svc-fast", nacos="fast-nacos")

    monkeypatch.setattr(nodes.hub_client, "list_agents", _aval([_agent("agent-hang")]))

    async def fake_list_instances(agent_id, service_name, **kw):
        if service_name == "hang-nacos":
            await asyncio.sleep(5)  # 远超 wait_for 的 0.05s:模拟单 agent WS 永久卡死
            return _instances(True)  # 不会走到(wait_for 先超时)
        return _instances(True, True)

    monkeypatch.setattr(nodes.hub_client, "list_instances", fake_list_instances)

    resp = client.get("/api/nodes", headers=h)
    assert resp.status_code == 200  # wait_for 截断卡死行,不吊死整页
    rows = {r["serviceCode"]: r for r in resp.json()["rows"]}
    assert rows["svc-hang"]["degraded"] is True  # 卡死行被 wait_for 超时标 degraded
    assert rows["svc-hang"]["healthyCount"] is None
    assert rows["svc-fast"]["degraded"] is False  # 其余行不受影响
    assert rows["svc-fast"]["healthyCount"] == 2


def test_nodes_degraded_on_generic_exception(client: TestClient, monkeypatch) -> None:
    # 任意 Exception(非超时,如 HubError / HTTP 错)同样 → 该行 degraded、healthyCount=null、整体 200。
    h = _h(client)
    ns = _mk_ns("agent-err")
    _mk_svc(ns, "svc-boom", nacos="boom-nacos")

    monkeypatch.setattr(nodes.hub_client, "list_agents", _aval([_agent("agent-err")]))
    monkeypatch.setattr(nodes.hub_client, "list_instances", _araise(hc.HubError("hub 不可用")))

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
    monkeypatch.setattr(nodes.hub_client, "list_agents", _aval([_agent("agent-off", online=False)]))
    monkeypatch.setattr(nodes.hub_client, "list_instances", _amust_not_call("离线行不应发 fan-out"))

    row = client.get("/api/nodes", headers=h).json()["rows"][0]
    assert row["online"] is False
    assert row["healthyCount"] is None
    assert row["degraded"] is False


def test_nodes_agent_absent_from_map_is_offline(client: TestClient, monkeypatch) -> None:
    # agent 完全不在 list_agents 返回里 → online=false、不 fan-out。
    h = _h(client)
    ns = _mk_ns("agent-ghost")
    _mk_svc(ns, "svc-ghost", nacos="ghost-nacos")

    monkeypatch.setattr(nodes.hub_client, "list_agents", _aval([]))  # 空在线表
    monkeypatch.setattr(nodes.hub_client, "list_instances", _amust_not_call("不在 map 的行不应 fan-out"))

    row = client.get("/api/nodes", headers=h).json()["rows"][0]
    assert row["online"] is False
    assert row["healthyCount"] is None
    assert row["degraded"] is False


def test_nodes_no_nacos_name_skips_fanout(client: TestClient, monkeypatch) -> None:
    # 无 nacosServiceName 的行 → 不调 list_instances、healthyCount=null、degraded=false(即便 agent 在线)。
    h = _h(client)
    ns = _mk_ns("agent-nonacos")
    _mk_svc(ns, "svc-nonacos", nacos=None)

    monkeypatch.setattr(nodes.hub_client, "list_agents", _aval([_agent("agent-nonacos")]))
    monkeypatch.setattr(nodes.hub_client, "list_instances", _amust_not_call("无 nacos 名不应 fan-out"))

    row = client.get("/api/nodes", headers=h).json()["rows"][0]
    assert row["online"] is True
    assert row["nacosServiceName"] is None
    assert row["healthyCount"] is None
    assert row["degraded"] is False


def test_nodes_list_agents_failure_all_offline(client: TestClient, monkeypatch) -> None:
    # list_agents 整体失败 → 仍 200,所有行 online=false、不崩(map 退化为空)。
    h = _h(client)
    ns = _mk_ns("agent-hubdown")
    _mk_svc(ns, "svc-1", nacos="n1")
    _mk_svc(ns, "svc-2", nacos="n2")

    monkeypatch.setattr(nodes.hub_client, "list_agents", _araise(hc.HubError("hub 整体不可用")))
    monkeypatch.setattr(nodes.hub_client, "list_instances", _amust_not_call("全离线时不应 fan-out"))

    resp = client.get("/api/nodes", headers=h)
    assert resp.status_code == 200  # list_agents 失败不崩整页
    rows = resp.json()["rows"]
    assert len(rows) == 2
    assert all(r["online"] is False for r in rows)
    assert all(r["healthyCount"] is None and r["degraded"] is False for r in rows)


def test_nodes_status_non_success_is_degraded(client: TestClient, monkeypatch) -> None:
    # list_instances 返回 dict 但 status != success → 视为该行降级(healthyCount=null、degraded=true)。
    h = _h(client)
    ns = _mk_ns("agent-bad")
    _mk_svc(ns, "svc-bad", nacos="bad-nacos")

    monkeypatch.setattr(nodes.hub_client, "list_agents", _aval([_agent("agent-bad")]))
    monkeypatch.setattr(nodes.hub_client, "list_instances", _aval({"status": "error", "instances": []}))

    row = client.get("/api/nodes", headers=h).json()["rows"][0]
    assert row["degraded"] is True
    assert row["healthyCount"] is None


def test_nodes_pagination(client: TestClient, monkeypatch) -> None:
    # 分页:pageSize=1 → 每页 1 行,count=总数,totalPage 正确。
    h = _h(client)
    ns = _mk_ns("agent-pg")
    _mk_svc(ns, "svc-p1", nacos="p1")
    _mk_svc(ns, "svc-p2", nacos="p2")

    monkeypatch.setattr(nodes.hub_client, "list_agents", _aval([_agent("agent-pg")]))
    monkeypatch.setattr(nodes.hub_client, "list_instances", _aval(_instances(True)))

    body = client.get("/api/nodes?page=1&pageSize=1", headers=h).json()
    assert body["count"] == 2
    assert len(body["rows"]) == 1
    assert body["pageSize"] == 1
    assert body["totalPage"] == 2


def test_nodes_requires_auth(client: TestClient) -> None:
    # 无 Bearer → default-deny 中间件 / require_session 401。
    assert client.get("/api/nodes").status_code == 401


def test_nodes_list_shows_discovered_dir_image_when_single(client: TestClient, monkeypatch) -> None:
    # 列表展示「发现权威」(P3-6):该 (agent, nacosService) 有**唯一** active DiscoveredNode →
    # dir/defaultImage 显示发现值(覆盖 Service 台账值)。
    h = _h(client)
    ns = _mk_ns("agent-ld")
    _mk_svc(ns, "svc-ld", nacos="ld-nacos")  # Service.dir=/opt/svc-ld image=registry/svc-ld:1.0
    _mk_discovered("agent-ld", "ld-nacos", container_name="c1", dir_="/srv/disc-ld", image="reg/disc:5")

    monkeypatch.setattr(nodes.hub_client, "list_agents", _aval([_agent("agent-ld")]))
    monkeypatch.setattr(nodes.hub_client, "list_instances", _aval(_instances(True)))

    row = client.get("/api/nodes", headers=h).json()["rows"][0]
    assert row["dir"] == "/srv/disc-ld"  # 发现权威覆盖展示
    assert row["defaultImage"] == "reg/disc:5"


def test_nodes_list_multi_instance_keeps_service_value(client: TestClient, monkeypatch) -> None:
    # 列表展示:多实例(同 nacosService 多 compose 工程)无法择一 → 展示仍回退 Service 值
    # (per-instance 列表归后续实例页 SPA 批次)。
    h = _h(client)
    ns = _mk_ns("agent-lm")
    _mk_svc(ns, "svc-lm", nacos="lm-nacos")  # Service.dir=/opt/svc-lm
    _mk_discovered("agent-lm", "lm-nacos", container_name="admin", compose_project="admin", dir_="/srv/admin")
    _mk_discovered("agent-lm", "lm-nacos", container_name="2admin", compose_project="2admin", dir_="/srv/2admin")

    monkeypatch.setattr(nodes.hub_client, "list_agents", _aval([_agent("agent-lm")]))
    monkeypatch.setattr(nodes.hub_client, "list_instances", _aval(_instances(True)))

    row = client.get("/api/nodes", headers=h).json()["rows"][0]
    assert row["dir"] == "/opt/svc-lm"  # 多实例不择一,展示 Service 值


def test_nodes_list_no_discovered_keeps_service_value(client: TestClient, monkeypatch) -> None:
    # 列表展示:无 DiscoveredNode → 回退展示 Service 台账值(与迁移前一致)。
    h = _h(client)
    ns = _mk_ns("agent-ln")
    _mk_svc(ns, "svc-ln", nacos="ln-nacos")  # dir=/opt/svc-ln image=registry/svc-ln:1.0

    monkeypatch.setattr(nodes.hub_client, "list_agents", _aval([_agent("agent-ln")]))
    monkeypatch.setattr(nodes.hub_client, "list_instances", _aval(_instances(True)))

    row = client.get("/api/nodes", headers=h).json()["rows"][0]
    assert row["dir"] == "/opt/svc-ln"
    assert row["defaultImage"] == "registry/svc-ln:1.0"


# =====================================================================================
# Task 10b:节点操作下发 POST /api/nodes/{agentId}/{serviceCode}/{action} + 操作审计
# =====================================================================================
#
# 寻址权威源 = 平台 Service 表(dir / nacosServiceName / defaultImage),BFF 不接受客户端
# 传路径/任意 image。优雅 restart 走 hub /api/rolling-restart;其余 dispatch。
# requested_by 由 hub 服务端派生 —— BFF **绝不自报 requested_by**(S5 后进程内 dispatch
# 固定传 requested_by_hint=None)。打桩纪律同上:patch nodes.hub_client.* 模块引用,替身须 async。


def _dispatch_ok(request_id: str = "req-123", accepted: bool = True) -> dict:
    """造一条 hub dispatch 成功响应(202 CommandDispatchResponse,camelCase)。"""
    return {"accepted": accepted, "command": {"requestId": request_id, "status": "queued"}}


def _capture_dispatch(store_box: dict):
    """返回一个 async dispatch_command 替身,把 (agent_id, payload) 记到 store_box 并回成功。"""

    async def _fake(agent_id, payload, **kw):
        store_box["agent_id"] = agent_id
        store_box["payload"] = payload
        return _dispatch_ok()

    return _fake


def test_op_restart_graceful_uses_rolling(client: TestClient, monkeypatch) -> None:
    # restart + graceful(或 mode 缺省)→ 走 hub rolling_restart(agentId, nacosServiceName, force=False),
    # 返回 kind=rolling + taskId(不走 dispatch)。
    h = _h(client)
    ns = _mk_ns("agent-r")
    _mk_svc(ns, "svc-roll", nacos="roll-nacos")

    box = {}

    async def fake_rolling(agent_id, service_name, force=False, **kw):
        box["agent_id"] = agent_id
        box["service_name"] = service_name
        box["force"] = force
        return {"taskId": "task-xyz"}

    monkeypatch.setattr(nodes.hub_client, "rolling_restart", fake_rolling)
    # dispatch 不应被调用
    monkeypatch.setattr(nodes.hub_client, "dispatch_command", _amust_not_call("不应调 dispatch"))

    resp = client.post("/api/nodes/agent-r/svc-roll/restart", json={"mode": "graceful"}, headers=h)
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "rolling"
    assert body["taskId"] == "task-xyz"
    assert body["requestId"] is None
    assert box["agent_id"] == "agent-r"
    assert box["service_name"] == "roll-nacos"
    assert box["force"] is False
    # 响应 camel:无 snake key
    assert "request_id" not in body and "task_id" not in body


def test_op_restart_default_mode_is_graceful(client: TestClient, monkeypatch) -> None:
    # restart 不传 mode → 按 graceful 走 rolling。
    h = _h(client)
    ns = _mk_ns("agent-rd")
    _mk_svc(ns, "svc-rd", nacos="rd-nacos")

    box = {}

    async def fake_rolling(a, s, force=False, **k):
        box["force"] = force
        return {"taskId": "t-rd"}

    monkeypatch.setattr(nodes.hub_client, "rolling_restart", fake_rolling)
    resp = client.post("/api/nodes/agent-rd/svc-rd/restart", json={}, headers=h)
    assert resp.status_code == 200
    assert resp.json()["kind"] == "rolling"
    assert resp.json()["taskId"] == "t-rd"
    assert box["force"] is False


def test_op_restart_graceful_no_nacos_400(client: TestClient, monkeypatch) -> None:
    # restart + graceful 但 service 无 nacosServiceName → 400(滚动必须有 serviceName)。
    h = _h(client)
    ns = _mk_ns("agent-rn")
    _mk_svc(ns, "svc-rn", nacos=None)

    monkeypatch.setattr(nodes.hub_client, "rolling_restart", _aval({"taskId": "x"}))
    resp = client.post("/api/nodes/agent-rn/svc-rn/restart", json={"mode": "graceful"}, headers=h)
    assert resp.status_code == 400


def test_op_restart_force_dispatches_force_restart(client: TestClient, monkeypatch) -> None:
    # restart + force → dispatch action=force-restart + dir(不走 rolling)。
    h = _h(client)
    ns = _mk_ns("agent-rf")
    _mk_svc(ns, "svc-rf", nacos="rf-nacos")

    box = {}
    monkeypatch.setattr(nodes.hub_client, "dispatch_command", _capture_dispatch(box))
    monkeypatch.setattr(nodes.hub_client, "rolling_restart", _amust_not_call("force 不应走 rolling"))

    resp = client.post("/api/nodes/agent-rf/svc-rf/restart", json={"mode": "force"}, headers=h)
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "command"
    assert body["requestId"] == "req-123"
    assert body["accepted"] is True
    assert box["agent_id"] == "agent-rf"
    assert box["payload"]["action"] == "force-restart"
    assert box["payload"]["dir"] == "/opt/svc-rf"


def test_op_start_dispatches_start(client: TestClient, monkeypatch) -> None:
    # start → dispatch action=start + dir(mode 忽略)。
    h = _h(client)
    ns = _mk_ns("agent-s")
    _mk_svc(ns, "svc-start", nacos="start-nacos")

    box = {}
    monkeypatch.setattr(nodes.hub_client, "dispatch_command", _capture_dispatch(box))
    resp = client.post("/api/nodes/agent-s/svc-start/start", json={}, headers=h)
    assert resp.status_code == 200
    assert box["payload"]["action"] == "start"
    assert box["payload"]["dir"] == "/opt/svc-start"
    # start 不带 mode/image/healthBaseUrl
    assert "mode" not in box["payload"]


def test_op_stop_force_dispatches_with_servicename_and_allowlast(client: TestClient, monkeypatch) -> None:
    # stop + force → dispatch action=stop mode=force + dir + serviceName + allowLastInstance 透传。
    h = _h(client)
    ns = _mk_ns("agent-sf")
    _mk_svc(ns, "svc-sf", nacos="sf-nacos")

    box = {}
    monkeypatch.setattr(nodes.hub_client, "dispatch_command", _capture_dispatch(box))
    resp = client.post(
        "/api/nodes/agent-sf/svc-sf/stop", json={"mode": "force", "allowLastInstance": True}, headers=h
    )
    assert resp.status_code == 200
    p = box["payload"]
    assert p["action"] == "stop"
    assert p["mode"] == "force"
    assert p["dir"] == "/opt/svc-sf"
    assert p["serviceName"] == "sf-nacos"
    assert p["allowLastInstance"] is True


def test_op_stop_graceful_derives_health_base_url(client: TestClient, monkeypatch) -> None:
    # stop + graceful → list_instances 返「本机 matched 健康实例」→ dispatch 含 healthBaseUrl=http://<addr>
    # + mode=graceful + shutdownTimeoutSec=60 + serviceName。
    # 评审 #3/#11(同步更新):新过滤为「healthy 且 matched」,既有桩须补 matched=True 才能过新过滤。
    h = _h(client)
    ns = _mk_ns("agent-sg")
    _mk_svc(ns, "svc-sg", nacos="sg-nacos")

    box = {}
    monkeypatch.setattr(nodes.hub_client, "dispatch_command", _capture_dispatch(box))
    # 第一个 unhealthy、第二个 healthy(均本机 matched)→ 应取第一个 healthy 的 address(10.0.0.1)
    monkeypatch.setattr(
        nodes.hub_client,
        "list_instances",
        _aval(
            {
                "status": "success",
                "instances": [
                    {"address": "10.0.0.0", "healthy": False, "matched": True},
                    {"address": "10.0.0.1", "healthy": True, "matched": True},
                ],
            }
        ),
    )
    resp = client.post("/api/nodes/agent-sg/svc-sg/stop", json={"mode": "graceful"}, headers=h)
    assert resp.status_code == 200
    p = box["payload"]
    assert p["action"] == "stop"
    assert p["mode"] == "graceful"
    assert p["dir"] == "/opt/svc-sg"
    assert p["serviceName"] == "sg-nacos"
    assert p["healthBaseUrl"] == "http://10.0.0.1"
    assert p["shutdownTimeoutSec"] == 60


def test_op_stop_graceful_picks_matched_not_index_zero(client: TestClient, monkeypatch) -> None:
    # 评审 #3(multi-node):healthy 实例可能在别节点(nacos 集群级,agent 恒置 healthy=True,
    # 只有 matched 反映「本机有该容器」)。即便别节点实例排在前面,也必须 drain **本机 matched** 那个,
    # 否则会 drain 别节点 worker、本节点没排空就停。断言 healthBaseUrl 指向 matched 实例(非第 0 个)。
    h = _h(client)
    ns = _mk_ns("agent-mx")
    _mk_svc(ns, "svc-mx", nacos="mx-nacos")

    box = {}
    monkeypatch.setattr(nodes.hub_client, "dispatch_command", _capture_dispatch(box))
    # 第 0 个:别节点(healthy 但 matched=False);第 1 个:本机(healthy 且 matched=True)。
    monkeypatch.setattr(
        nodes.hub_client,
        "list_instances",
        _aval(
            {
                "status": "success",
                "instances": [
                    {"address": "10.9.9.9", "healthy": True, "matched": False},  # 别节点,不可 drain
                    {"address": "10.0.0.5", "healthy": True, "matched": True},  # 本机
                ],
            }
        ),
    )
    resp = client.post("/api/nodes/agent-mx/svc-mx/stop", json={"mode": "graceful"}, headers=h)
    assert resp.status_code == 200
    # 选中的是本机 matched 实例,而非数组第 0 个(别节点)
    assert box["payload"]["healthBaseUrl"] == "http://10.0.0.5"


def test_op_stop_graceful_only_unmatched_409(client: TestClient, monkeypatch) -> None:
    # 评审 #3:仅有 matched=False(别节点)健康实例 → 本节点无可 drain 的本机实例 → 409(改用 force)。
    h = _h(client)
    ns = _mk_ns("agent-um")
    _mk_svc(ns, "svc-um", nacos="um-nacos")

    monkeypatch.setattr(
        nodes.hub_client,
        "list_instances",
        _aval({"status": "success", "instances": [{"address": "10.9.9.9", "healthy": True, "matched": False}]}),
    )
    monkeypatch.setattr(nodes.hub_client, "dispatch_command", _amust_not_call("无本机实例不应 dispatch"))
    resp = client.post("/api/nodes/agent-um/svc-um/stop", json={"mode": "graceful"}, headers=h)
    assert resp.status_code == 409


def test_op_stop_graceful_passes_expected_compose_project(client: TestClient, monkeypatch) -> None:
    # 评审 #11(contract):BFF 须把由 Service.dir 推得的 compose 默认工程名作为 expectedComposeProject
    # 传给 list_instances(否则 agent 的工程漂移守卫永不触发)。Docker 默认工程名 = 目录 basename
    # 小写、仅留 [a-z0-9_-]、去前导非字母数字。dir=/opt/My_Svc-01 → 工程名 my_svc-01。
    h = _h(client)
    ns = _mk_ns("agent-ecp")
    # 覆盖 _mk_svc 默认 dir,用带大写/混合字符的目录验证 basename 规范化
    store.create_row(
        Service,
        {"namespace_id": ns, "service_code": "svc-ecp", "dir": "/opt/My_Svc-01", "default_image": "registry/x:1.0", "nacos_service_name": "ecp-nacos"},
    )

    captured = {}

    def sync_list_instances(agent_id, service_name, expected_compose_project=None, **kw):
        captured["expected"] = expected_compose_project
        return {"status": "success", "instances": [{"address": "10.0.0.3", "healthy": True, "matched": True}]}

    monkeypatch.setattr(nodes.hub_client, "list_instances", _afn(sync_list_instances))
    monkeypatch.setattr(nodes.hub_client, "dispatch_command", _capture_dispatch({}))

    resp = client.post("/api/nodes/agent-ecp/svc-ecp/stop", json={"mode": "graceful"}, headers=h)
    assert resp.status_code == 200
    assert captured["expected"] == "my_svc-01"  # 由 /opt/My_Svc-01 推得


def test_op_stop_graceful_no_healthy_409(client: TestClient, monkeypatch) -> None:
    # stop + graceful 但 0 healthy → 409(无健康实例可优雅 drain;请用 force)。
    h = _h(client)
    ns = _mk_ns("agent-sg0")
    _mk_svc(ns, "svc-sg0", nacos="sg0-nacos")

    monkeypatch.setattr(
        nodes.hub_client,
        "list_instances",
        _aval({"status": "success", "instances": [{"address": "10.0.0.0", "healthy": False}]}),
    )
    monkeypatch.setattr(nodes.hub_client, "dispatch_command", _amust_not_call("无健康实例不应 dispatch"))
    resp = client.post("/api/nodes/agent-sg0/svc-sg0/stop", json={"mode": "graceful"}, headers=h)
    assert resp.status_code == 409


def test_op_stop_graceful_no_nacos_400(client: TestClient, monkeypatch) -> None:
    # stop + graceful 但无 nacosServiceName → 400(优雅操作需配置 nacosServiceName,不发 list_instances)。
    h = _h(client)
    ns = _mk_ns("agent-sgn")
    _mk_svc(ns, "svc-sgn", nacos=None)

    monkeypatch.setattr(nodes.hub_client, "list_instances", _amust_not_call("无 nacos 不应查实例"))
    resp = client.post("/api/nodes/agent-sgn/svc-sgn/stop", json={"mode": "graceful"}, headers=h)
    assert resp.status_code == 400


def test_op_stop_requires_mode(client: TestClient, monkeypatch) -> None:
    # stop 缺省 mode → 400(stop/redeploy 须指定 mode)。
    h = _h(client)
    ns = _mk_ns("agent-snm")
    _mk_svc(ns, "svc-snm", nacos="snm-nacos")
    resp = client.post("/api/nodes/agent-snm/svc-snm/stop", json={}, headers=h)
    assert resp.status_code == 400


def test_op_redeploy_force_uses_default_image(client: TestClient, monkeypatch) -> None:
    # redeploy + force → dispatch action=pull-redeploy mode=force + dir + image=defaultImage。
    h = _h(client)
    ns = _mk_ns("agent-rdf")
    _mk_svc(ns, "svc-rdf", nacos="rdf-nacos")

    box = {}
    monkeypatch.setattr(nodes.hub_client, "dispatch_command", _capture_dispatch(box))
    resp = client.post("/api/nodes/agent-rdf/svc-rdf/redeploy", json={"mode": "force"}, headers=h)
    assert resp.status_code == 200
    p = box["payload"]
    assert p["action"] == "pull-redeploy"
    assert p["mode"] == "force"
    assert p["dir"] == "/opt/svc-rdf"
    assert p["image"] == "registry/svc-rdf:1.0"


def test_op_redeploy_graceful_derives_health_and_image(client: TestClient, monkeypatch) -> None:
    # redeploy + graceful → 派生 healthBaseUrl + dispatch pull-redeploy + image=defaultImage。
    h = _h(client)
    ns = _mk_ns("agent-rdg")
    _mk_svc(ns, "svc-rdg", nacos="rdg-nacos")

    box = {}
    monkeypatch.setattr(nodes.hub_client, "dispatch_command", _capture_dispatch(box))
    # 评审 #3/#11(同步更新):本机 matched 健康实例,才能过「healthy 且 matched」新过滤。
    monkeypatch.setattr(
        nodes.hub_client,
        "list_instances",
        _aval({"status": "success", "instances": [{"address": "10.0.0.7", "healthy": True, "matched": True}]}),
    )
    resp = client.post("/api/nodes/agent-rdg/svc-rdg/redeploy", json={"mode": "graceful"}, headers=h)
    assert resp.status_code == 200
    p = box["payload"]
    assert p["action"] == "pull-redeploy"
    assert p["mode"] == "graceful"
    assert p["image"] == "registry/svc-rdg:1.0"
    assert p["healthBaseUrl"] == "http://10.0.0.7"
    # 评审 T10 Important:优雅 redeploy 须与优雅 stop 对称透传 shutdownTimeoutSec(平台统一 drain 超时)。
    assert p["shutdownTimeoutSec"] == 60


def test_op_redeploy_force_no_default_image_400(client: TestClient, monkeypatch) -> None:
    # redeploy + force 但 service 无 defaultImage → 400。
    h = _h(client)
    ns = _mk_ns("agent-rdni")
    # 直接造一条无 default_image 的 service(_mk_svc 默认填了 image,这里覆盖为 None)
    store.create_row(
        Service,
        {"namespace_id": ns, "service_code": "svc-rdni", "dir": "/opt/svc-rdni", "default_image": None, "nacos_service_name": "rdni-nacos"},
    )
    monkeypatch.setattr(nodes.hub_client, "dispatch_command", _amust_not_call("无 image 不应 dispatch"))
    resp = client.post("/api/nodes/agent-rdni/svc-rdni/redeploy", json={"mode": "force"}, headers=h)
    assert resp.status_code == 400


def test_op_redeploy_graceful_no_default_image_400(client: TestClient, monkeypatch) -> None:
    # redeploy + graceful 但无 defaultImage → 400(早于派生 healthBaseUrl;list_instances 不应被调)。
    h = _h(client)
    ns = _mk_ns("agent-rdgn")
    store.create_row(
        Service,
        {"namespace_id": ns, "service_code": "svc-rdgn", "dir": "/opt/svc-rdgn", "default_image": None, "nacos_service_name": "rdgn-nacos"},
    )
    monkeypatch.setattr(nodes.hub_client, "list_instances", _amust_not_call("无 image 应先 400,不查实例"))
    resp = client.post("/api/nodes/agent-rdgn/svc-rdgn/redeploy", json={"mode": "graceful"}, headers=h)
    assert resp.status_code == 400


def test_op_stop_graceful_list_instances_failure_502(client: TestClient, monkeypatch) -> None:
    # 评审 #8(test-integrity):优雅 stop 派生 healthBaseUrl 时 list_instances 抛错(hub 不可达/超时)
    # → 被 dispatch 的 broad except 压平成脱敏 502;且派生失败**不得**继续下发(dispatch 不被调)。
    h = _h(client)
    ns = _mk_ns("agent-sgf")
    _mk_svc(ns, "svc-sgf", nacos="sgf-nacos")

    monkeypatch.setattr(nodes.hub_client, "list_instances", _araise(hc.HubError("hub list-instances 内部细节-勿泄漏")))
    monkeypatch.setattr(nodes.hub_client, "dispatch_command", _amust_not_call("派生失败不应 dispatch"))
    resp = client.post("/api/nodes/agent-sgf/svc-sgf/stop", json={"mode": "graceful"}, headers=h)
    assert resp.status_code == 502
    assert "勿泄漏" not in resp.text  # 脱敏:不回显内部异常文案


def test_op_redeploy_graceful_list_instances_timeout_502(client: TestClient, monkeypatch) -> None:
    # 评审 #8:优雅 redeploy 派生 healthBaseUrl 时 list_instances 超时 → 脱敏 502 + dispatch 不被调。
    h = _h(client)
    ns = _mk_ns("agent-rgf")
    _mk_svc(ns, "svc-rgf", nacos="rgf-nacos")

    monkeypatch.setattr(nodes.hub_client, "list_instances", _araise(asyncio.TimeoutError("connect timeout 内部细节-勿泄漏")))
    monkeypatch.setattr(nodes.hub_client, "dispatch_command", _amust_not_call("派生失败不应 dispatch"))
    resp = client.post("/api/nodes/agent-rgf/svc-rgf/redeploy", json={"mode": "graceful"}, headers=h)
    assert resp.status_code == 502
    assert "勿泄漏" not in resp.text


def test_op_redeploy_requires_mode(client: TestClient) -> None:
    # redeploy 缺省 mode → 400。
    h = _h(client)
    ns = _mk_ns("agent-rdm")
    _mk_svc(ns, "svc-rdm", nacos="rdm-nacos")
    resp = client.post("/api/nodes/agent-rdm/svc-rdm/redeploy", json={}, headers=h)
    assert resp.status_code == 400


def test_op_unknown_action_422(client: TestClient) -> None:
    # 未知 action(不在 {start,stop,restart,redeploy})→ 422(路径枚举校验)。
    h = _h(client)
    ns = _mk_ns("agent-ua")
    _mk_svc(ns, "svc-ua", nacos="ua-nacos")
    resp = client.post("/api/nodes/agent-ua/svc-ua/blowup", json={"mode": "force"}, headers=h)
    assert resp.status_code == 422


def test_op_unknown_service_404(client: TestClient) -> None:
    # (agent_id, service_code) 不在台账 → 404(节点服务不在台账)。
    h = _h(client)
    _mk_ns("agent-known")  # namespace 存在,但无该 service
    resp = client.post("/api/nodes/agent-known/no-such-svc/start", json={}, headers=h)
    assert resp.status_code == 404
    # agent(namespace)本身不存在 → 也 404
    resp2 = client.post("/api/nodes/ghost-agent/whatever/start", json={}, headers=h)
    assert resp2.status_code == 404


def test_op_dispatch_failure_maps_502(client: TestClient, monkeypatch) -> None:
    # dispatch 抛 HubError → 502 脱敏(不泄漏内部细节)。
    h = _h(client)
    ns = _mk_ns("agent-502")
    _mk_svc(ns, "svc-502", nacos="n502")

    monkeypatch.setattr(nodes.hub_client, "dispatch_command", _araise(hc.HubError("内部 hub 细节-不该泄漏")))
    resp = client.post("/api/nodes/agent-502/svc-502/start", json={}, headers=h)
    assert resp.status_code == 502
    assert "内部 hub 细节" not in resp.text  # 脱敏:不回显内部消息


def test_op_rolling_failure_maps_502(client: TestClient, monkeypatch) -> None:
    # rolling_restart 抛 → 502 脱敏。
    h = _h(client)
    ns = _mk_ns("agent-r502")
    _mk_svc(ns, "svc-r502", nacos="nr502")

    monkeypatch.setattr(nodes.hub_client, "rolling_restart", _araise(hc.HubError("rolling 内部细节-勿泄漏")))
    resp = client.post("/api/nodes/agent-r502/svc-r502/restart", json={"mode": "graceful"}, headers=h)
    assert resp.status_code == 502
    assert "勿泄漏" not in resp.text


def test_op_requires_auth(client: TestClient) -> None:
    # 无 Bearer → 401(纵深防御 + require_session)。
    assert client.post("/api/nodes/a/b/start", json={}).status_code == 401


# =====================================================================================
# P3-6:寻址权威迁到 DiscoveredNode(发现权威)
# =====================================================================================
#
# dir / image / containerId 的权威源 = agent 自动发现上报的 DiscoveredNode,Service.dir/default_image
# 退化为「该 (agent, nacosService) 暂无 DiscoveredNode 时」的迁移期回退。下面用例 seed DiscoveredNode
# 行(经 _mk_discovered)验证:有发现行 → 取发现值(即便 Service 值不同/为空);多实例(同 nacosService
# 多 compose 工程)须 composeProject 消歧;无发现行 → 回退 Service;Service 也空 → 现有「需配置」仍成立。


def test_op_dispatch_uses_discovered_dir_over_service(client: TestClient, monkeypatch) -> None:
    # 发现权威(核心):Service.dir=/opt/svc-dn,但 DiscoveredNode.dir=/srv/real/path →
    # dispatch payload 的 dir 必须取**发现值** /srv/real/path(不是 Service 值)。
    h = _h(client)
    ns = _mk_ns("agent-dn")
    _mk_svc(ns, "svc-dn", nacos="dn-nacos")  # _mk_svc 默认 dir=/opt/svc-dn
    _mk_discovered("agent-dn", "dn-nacos", container_name="c1", dir_="/srv/real/path", image="reg/real:9")

    box = {}
    monkeypatch.setattr(nodes.hub_client, "dispatch_command", _capture_dispatch(box))
    resp = client.post("/api/nodes/agent-dn/svc-dn/restart", json={"mode": "force"}, headers=h)
    assert resp.status_code == 200
    # dir 取自 DiscoveredNode(权威),而非 Service.dir
    assert box["payload"]["dir"] == "/srv/real/path"


def test_op_dispatch_uses_discovered_dir_when_service_dir_empty(client: TestClient, monkeypatch) -> None:
    # 发现权威边界:Service.dir 为空(None)但 DiscoveredNode.dir 有值 → 仍用发现值,不因 Service 空而失败。
    h = _h(client)
    ns = _mk_ns("agent-dne")
    # Service.dir=None(覆盖 _mk_svc 默认),仅配 nacos
    store.create_row(
        Service,
        {"namespace_id": ns, "service_code": "svc-dne", "dir": None, "default_image": None, "nacos_service_name": "dne-nacos"},
    )
    _mk_discovered("agent-dne", "dne-nacos", container_name="c1", dir_="/srv/discovered")

    box = {}
    monkeypatch.setattr(nodes.hub_client, "dispatch_command", _capture_dispatch(box))
    resp = client.post("/api/nodes/agent-dne/svc-dne/start", json={}, headers=h)
    assert resp.status_code == 200
    assert box["payload"]["dir"] == "/srv/discovered"


def test_op_redeploy_force_uses_discovered_image_over_service(client: TestClient, monkeypatch) -> None:
    # 发现权威(image):redeploy force 的 image 取 DiscoveredNode.image,而非 Service.default_image。
    h = _h(client)
    ns = _mk_ns("agent-dni")
    _mk_svc(ns, "svc-dni", nacos="dni-nacos")  # Service.default_image=registry/svc-dni:1.0
    _mk_discovered("agent-dni", "dni-nacos", container_name="c1", dir_="/srv/x", image="registry/discovered:7")

    box = {}
    monkeypatch.setattr(nodes.hub_client, "dispatch_command", _capture_dispatch(box))
    resp = client.post("/api/nodes/agent-dni/svc-dni/redeploy", json={"mode": "force"}, headers=h)
    assert resp.status_code == 200
    p = box["payload"]
    assert p["image"] == "registry/discovered:7"  # 发现权威
    assert p["dir"] == "/srv/x"


def test_op_dispatch_falls_back_to_service_when_no_discovered(client: TestClient, monkeypatch) -> None:
    # 回退(迁移期):该 (agent, nacosService) 无任何 DiscoveredNode → dir/image 取 Service 台账值。
    h = _h(client)
    ns = _mk_ns("agent-fb")
    _mk_svc(ns, "svc-fb", nacos="fb-nacos")  # dir=/opt/svc-fb image=registry/svc-fb:1.0
    # 不 seed 任何 DiscoveredNode

    box = {}
    monkeypatch.setattr(nodes.hub_client, "dispatch_command", _capture_dispatch(box))
    resp = client.post("/api/nodes/agent-fb/svc-fb/redeploy", json={"mode": "force"}, headers=h)
    assert resp.status_code == 200
    p = box["payload"]
    assert p["dir"] == "/opt/svc-fb"  # 回退 Service.dir
    assert p["image"] == "registry/svc-fb:1.0"  # 回退 Service.default_image


def test_op_dispatch_no_discovered_and_service_empty_still_400(client: TestClient, monkeypatch) -> None:
    # 回退且 Service 也空:redeploy force 无 DiscoveredNode 且 Service.default_image=None → 现有「需配置 defaultImage」400 仍成立。
    h = _h(client)
    ns = _mk_ns("agent-fb0")
    store.create_row(
        Service,
        {"namespace_id": ns, "service_code": "svc-fb0", "dir": "/opt/svc-fb0", "default_image": None, "nacos_service_name": "fb0-nacos"},
    )
    monkeypatch.setattr(nodes.hub_client, "dispatch_command", _amust_not_call("无 image 不应 dispatch"))
    resp = client.post("/api/nodes/agent-fb0/svc-fb0/redeploy", json={"mode": "force"}, headers=h)
    assert resp.status_code == 400


def test_op_multi_instance_without_compose_project_409(client: TestClient, monkeypatch) -> None:
    # 多实例(同一 nacosService 两个 compose 工程)+ 不带 composeProject → 409(请指定 composeProject)。
    h = _h(client)
    ns = _mk_ns("agent-mi")
    _mk_svc(ns, "svc-mi", nacos="mi-nacos")
    _mk_discovered("agent-mi", "mi-nacos", container_name="admin", compose_project="admin", dir_="/srv/admin")
    _mk_discovered("agent-mi", "mi-nacos", container_name="2admin", compose_project="2admin", dir_="/srv/2admin")

    monkeypatch.setattr(nodes.hub_client, "dispatch_command", _amust_not_call("多实例未消歧不应 dispatch"))
    resp = client.post("/api/nodes/agent-mi/svc-mi/start", json={}, headers=h)
    assert resp.status_code == 409


def test_op_multi_instance_with_compose_project_hits_that_instance(client: TestClient, monkeypatch) -> None:
    # 多实例 + 带 composeProject → 命中该实例的 dir(按 composeProject 定位到指定 compose 工程那一行)。
    h = _h(client)
    ns = _mk_ns("agent-mi2")
    _mk_svc(ns, "svc-mi2", nacos="mi2-nacos")
    _mk_discovered("agent-mi2", "mi2-nacos", container_name="admin", compose_project="admin", dir_="/srv/admin")
    _mk_discovered("agent-mi2", "mi2-nacos", container_name="2admin", compose_project="2admin", dir_="/srv/2admin")

    box = {}
    monkeypatch.setattr(nodes.hub_client, "dispatch_command", _capture_dispatch(box))
    resp = client.post(
        "/api/nodes/agent-mi2/svc-mi2/start", json={"composeProject": "2admin"}, headers=h
    )
    assert resp.status_code == 200
    assert box["payload"]["dir"] == "/srv/2admin"  # 命中指定 compose 工程那一实例


def test_op_multi_instance_unknown_compose_project_404(client: TestClient, monkeypatch) -> None:
    # 多实例 + 指定了不存在的 composeProject → 404(目标实例不存在,语义同台账缺失)。
    h = _h(client)
    ns = _mk_ns("agent-mi3")
    _mk_svc(ns, "svc-mi3", nacos="mi3-nacos")
    _mk_discovered("agent-mi3", "mi3-nacos", container_name="admin", compose_project="admin", dir_="/srv/admin")
    _mk_discovered("agent-mi3", "mi3-nacos", container_name="2admin", compose_project="2admin", dir_="/srv/2admin")

    monkeypatch.setattr(nodes.hub_client, "dispatch_command", _amust_not_call("无匹配实例不应 dispatch"))
    resp = client.post(
        "/api/nodes/agent-mi3/svc-mi3/start", json={"composeProject": "nope"}, headers=h
    )
    assert resp.status_code == 404


def test_op_stale_discovered_ignored_falls_back(client: TestClient, monkeypatch) -> None:
    # 仅有 stale 的 DiscoveredNode(已缺席)→ 不计入寻址(只取 active)→ 回退 Service.dir。
    h = _h(client)
    ns = _mk_ns("agent-st")
    _mk_svc(ns, "svc-st", nacos="st-nacos")  # dir=/opt/svc-st
    _mk_discovered("agent-st", "st-nacos", container_name="c1", dir_="/srv/stale", status="stale")

    box = {}
    monkeypatch.setattr(nodes.hub_client, "dispatch_command", _capture_dispatch(box))
    resp = client.post("/api/nodes/agent-st/svc-st/start", json={}, headers=h)
    assert resp.status_code == 200
    assert box["payload"]["dir"] == "/opt/svc-st"  # stale 不算,回退 Service


def test_op_stop_graceful_expected_project_from_discovered(client: TestClient, monkeypatch) -> None:
    # 发现权威(expectedComposeProject):有唯一 DiscoveredNode 时,优雅 drain 传给 list_instances 的
    # expectedComposeProject 取 DiscoveredNode 的真实 compose_project(而非从 Service.dir basename 猜)。
    # Service.dir=/opt/svc-ep(dir 启发式会得 svc-ep),但 DiscoveredNode.compose_project=real-proj →
    # 应传 real-proj,证明权威来自发现值而非 dir 启发式。
    h = _h(client)
    ns = _mk_ns("agent-ep")
    _mk_svc(ns, "svc-ep", nacos="ep-nacos")  # dir=/opt/svc-ep
    _mk_discovered(
        "agent-ep", "ep-nacos", container_name="c1", dir_="/whatever", compose_project="real-proj"
    )

    captured = {}

    def sync_list_instances(agent_id, service_name, expected_compose_project=None, **kw):
        captured["expected"] = expected_compose_project
        return {"status": "success", "instances": [{"address": "10.0.0.3", "healthy": True, "matched": True}]}

    monkeypatch.setattr(nodes.hub_client, "list_instances", _afn(sync_list_instances))
    monkeypatch.setattr(nodes.hub_client, "dispatch_command", _capture_dispatch({}))

    resp = client.post("/api/nodes/agent-ep/svc-ep/stop", json={"mode": "graceful"}, headers=h)
    assert resp.status_code == 200
    assert captured["expected"] == "real-proj"  # 取自 DiscoveredNode.compose_project(发现权威)


def test_op_stop_graceful_expected_project_falls_back_to_dir_heuristic(client: TestClient, monkeypatch) -> None:
    # 回退:DiscoveredNode.compose_project 为空时,expectedComposeProject 回退 dir basename 启发式
    # (此处用发现 dir;无发现行的回退已由 test_op_stop_graceful_passes_expected_compose_project 覆盖)。
    h = _h(client)
    ns = _mk_ns("agent-epf")
    _mk_svc(ns, "svc-epf", nacos="epf-nacos")
    # 发现行存在但 compose_project=None,dir=/opt/My_Svc-01 → 回退启发式得 my_svc-01
    _mk_discovered("agent-epf", "epf-nacos", container_name="c1", dir_="/opt/My_Svc-01", compose_project=None)

    captured = {}

    def sync_list_instances(agent_id, service_name, expected_compose_project=None, **kw):
        captured["expected"] = expected_compose_project
        return {"status": "success", "instances": [{"address": "10.0.0.4", "healthy": True, "matched": True}]}

    monkeypatch.setattr(nodes.hub_client, "list_instances", _afn(sync_list_instances))
    monkeypatch.setattr(nodes.hub_client, "dispatch_command", _capture_dispatch({}))

    resp = client.post("/api/nodes/agent-epf/svc-epf/stop", json={"mode": "graceful"}, headers=h)
    assert resp.status_code == 200
    assert captured["expected"] == "my_svc-01"  # compose_project 为空 → 回退发现 dir 的 basename 启发式


def test_resolve_addressing_single_discovered_authority(client: TestClient) -> None:
    # 直接单测寻址解析:唯一 active DiscoveredNode → dir/image/containerId/expectedComposeProject 全取发现值。
    ns = _mk_ns("agent-ra")
    _mk_svc(ns, "svc-ra", nacos="ra-nacos")  # Service.dir=/opt/svc-ra image=registry/svc-ra:1.0
    _mk_discovered(
        "agent-ra", "ra-nacos", container_name="c1", dir_="/srv/auth", image="reg/auth:1",
        container_id="cid-abc", compose_project="proj-x",
    )
    addr = nodes._resolve_addressing("agent-ra", "svc-ra")
    assert addr.dir == "/srv/auth"
    assert addr.image == "reg/auth:1"
    assert addr.container_id == "cid-abc"
    assert addr.nacos_service_name == "ra-nacos"
    assert addr.expected_compose_project == "proj-x"


def test_resolve_addressing_no_nacos_falls_back_to_service(client: TestClient) -> None:
    # Service 无 nacosServiceName → 无法按 nacos 收敛实例 → 直接回退 Service 台账值(dir/image),containerId 无。
    ns = _mk_ns("agent-rann")
    store.create_row(
        Service,
        {"namespace_id": ns, "service_code": "svc-rann", "dir": "/opt/no-nacos", "default_image": "reg/nn:1", "nacos_service_name": None},
    )
    # 即便库里有同 agent 的发现行,Service 无 nacos 也走回退(无法对回)
    _mk_discovered("agent-rann", "some-nacos", container_name="c1", dir_="/srv/ignored")
    addr = nodes._resolve_addressing("agent-rann", "svc-rann")
    assert addr.dir == "/opt/no-nacos"  # 回退 Service
    assert addr.image == "reg/nn:1"
    assert addr.container_id is None
    assert addr.nacos_service_name is None


def test_store_list_discovered_nodes_for_agents_queries(client: TestClient) -> None:
    # 直接覆盖批量查询封装:空入参短路回 [](不发空 IN);有入参按 agent_id IN + status 过滤。
    assert store.list_discovered_nodes_for_agents([]) == []  # 空 agent_ids 短路
    _mk_ns("agent-bq")
    _mk_discovered("agent-bq", "bq-nacos", container_name="c1", dir_="/srv/a")
    _mk_discovered("agent-bq", "bq-nacos", container_name="c2", dir_="/srv/b", status="stale")
    _mk_discovered("agent-other", "x-nacos", container_name="c3", dir_="/srv/c")

    # 默认只取 active,且按 agent_id IN 过滤掉 agent-other。
    active = store.list_discovered_nodes_for_agents(["agent-bq"])
    assert {r.container_name for r in active} == {"c1"}
    # status=None → 取全集(含 stale)。
    all_rows = store.list_discovered_nodes_for_agents(["agent-bq"], status=None)
    assert {r.container_name for r in all_rows} == {"c1", "c2"}


def test_store_list_discovered_nodes_status_none_includes_stale(client: TestClient) -> None:
    # list_discovered_nodes 的 status=None 分支:取全集(含 stale);默认 active 只取活跃行。
    _mk_ns("agent-sn")
    _mk_discovered("agent-sn", "sn-nacos", container_name="a", dir_="/srv/a")
    _mk_discovered("agent-sn", "sn-nacos", container_name="b", dir_="/srv/b", status="stale")

    active = store.list_discovered_nodes("agent-sn", "sn-nacos")
    assert {r.container_name for r in active} == {"a"}
    all_rows = store.list_discovered_nodes("agent-sn", "sn-nacos", status=None)
    assert {r.container_name for r in all_rows} == {"a", "b"}


# --- 操作审计 GET /api/node-operations(代理 hub /api/commands,limit/offset↔page/pageSize) --------


def _hub_commands(*, total: int, items: list[dict], limit: int, offset: int) -> dict:
    """造一条 hub CommandListResponse 风格响应(camelCase)。"""
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "hasMore": offset + len(items) < total,
        "sortBy": "createdAt",
        "order": "desc",
    }


def _cmd_row(request_id: str = "c1", **over) -> dict:
    """造一条 hub CommandSnapshot 风格行(camelCase),含审计页关心字段。"""
    base = {
        "requestId": request_id,
        "agentId": "agent-x",
        "status": "succeeded",
        "action": "stop",
        "mode": "force",
        "dir": "/opt/x",
        "image": None,
        "requestedBy": "platform-admin",
        "requestSource": "console",
        "output": "ok",
        "error": None,
        "createdAt": "2026-06-21T00:00:00Z",
        "updatedAt": "2026-06-21T00:00:01Z",
        "payload": {"action": "stop"},
    }
    base.update(over)
    return base


def test_node_operations_lists_audit_envelope(client: TestClient, monkeypatch) -> None:
    # GET /api/node-operations → 调 list_commands、返平台标准信封 {count, rows, page, pageSize, totalPage}。
    h = _h(client)

    box = {}

    def sync_list_commands(page, page_size, **kw):
        box["page"] = page
        box["page_size"] = page_size
        return _hub_commands(total=3, items=[_cmd_row("c1"), _cmd_row("c2")], limit=page_size, offset=0)

    monkeypatch.setattr(nodes.hub_client, "list_commands", _afn(sync_list_commands))

    body = client.get("/api/node-operations?page=1&pageSize=2", headers=h).json()
    assert box["page"] == 1 and box["page_size"] == 2
    assert body["count"] == 3
    assert body["page"] == 1 and body["pageSize"] == 2
    assert body["totalPage"] == 2  # ceil(3/2)
    assert len(body["rows"]) == 2
    r0 = body["rows"][0]
    assert r0["requestId"] == "c1"
    assert r0["agentId"] == "agent-x"
    assert r0["action"] == "stop"
    assert r0["mode"] == "force"
    assert r0["status"] == "succeeded"
    assert r0["requestedBy"] == "platform-admin"
    assert r0["requestSource"] == "console"
    assert r0["dir"] == "/opt/x"
    assert r0["output"] == "ok"
    # camel 契约:无 snake key
    for snake in ("request_id", "agent_id", "requested_by", "request_source"):
        assert snake not in r0


def test_node_operations_truncates_output(client: TestClient, monkeypatch) -> None:
    # output / error 超 1000 字符 → 截尾保留后 1000 字符 + 前缀标记(结果通常在尾部)。
    h = _h(client)
    long_out = "A" * 500 + "B" * 1000  # 1500 字符,尾部是 1000 个 B
    long_err = "E" * 2000
    monkeypatch.setattr(
        nodes.hub_client,
        "list_commands",
        _afn(
            lambda page, page_size, **kw: _hub_commands(
                total=1, items=[_cmd_row("ct", output=long_out, error=long_err)], limit=page_size, offset=0
            )
        ),
    )
    row = client.get("/api/node-operations", headers=h).json()["rows"][0]
    # 截断后:长度 = 1000 + 前缀标记长度;尾部保留(末尾是 B,不是 A)
    assert row["output"].endswith("B" * 1000)
    assert "已截断" in row["output"]
    assert len(row["output"]) < len(long_out)  # 确实被截短
    assert row["error"].endswith("E" * 1000)
    assert "已截断" in row["error"]


def test_node_operations_truncate_boundary(client: TestClient, monkeypatch) -> None:
    # 评审 #9(test-integrity):截尾边界。恰 1000 字符(== 上限)→ 原样返回、不加「已截断」;
    # 1001 字符(> 上限)→ 被截、含前缀标记。
    h = _h(client)
    exact = "X" * 1000  # 恰好等于 _AUDIT_FIELD_MAX,不应截
    over = "Y" * 1001  # 超 1 字符,应截

    monkeypatch.setattr(
        nodes.hub_client,
        "list_commands",
        _afn(
            lambda page, page_size, **kw: _hub_commands(
                total=1, items=[_cmd_row("cb", output=exact, error=over)], limit=page_size, offset=0
            )
        ),
    )
    row = client.get("/api/node-operations", headers=h).json()["rows"][0]
    # == 上限:原样、长度不变、无前缀
    assert row["output"] == exact
    assert len(row["output"]) == 1000
    assert "已截断" not in row["output"]
    # > 上限:被截、保留末尾 1000、含前缀
    assert "已截断" in row["error"]
    assert row["error"].endswith("Y" * 1000)


def test_node_operations_hub_failure_502(client: TestClient, monkeypatch) -> None:
    # hub 调用失败 → 502 脱敏。
    h = _h(client)

    monkeypatch.setattr(nodes.hub_client, "list_commands", _araise(hc.HubError("hub 列表内部错误-勿泄漏")))
    resp = client.get("/api/node-operations", headers=h)
    assert resp.status_code == 502
    assert "勿泄漏" not in resp.text


def test_node_operations_requires_auth(client: TestClient) -> None:
    # 无 Bearer → 401。
    assert client.get("/api/node-operations").status_code == 401
