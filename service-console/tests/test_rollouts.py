"""投放(Rollout,P4-2/P4-3)store + 协调器回写 + 端点测试。

经 conftest 的 fixture:
- store 单测 + 协调器回写测试用 `client`(临时文件库 + swap 单例),直调 `store.*` 与
  `_run_service_rolling`(后者 rollout_id 路径要真写 rollouts 表,故必须真库)。
- 端点测试用 `hub_client`(每用例全新 HubState):投放端点建/查 rolling task 走
  `main_module.hub_state`,用全新实例避免跨用例残留滚动锁污染计数/冲突断言(照既有 rolling 端点测试)。

无 pytest-asyncio:异步一律 `asyncio.run(...)`(与 test_rolling.py 同款)。
"""

from __future__ import annotations

import asyncio

import sqlalchemy as sa
from fastapi.testclient import TestClient

from app import store
from app.db import Database
from app.db_models import Rollout
from app.hub.routers import rolling as rolling_router

ADMIN = {"X-Admin-Token": "test-admin-token"}


# =====================================================================================
# 建表自检
# =====================================================================================


def test_rollouts_table_created(tmp_path) -> None:
    d = Database(f"sqlite:///{tmp_path}/t.db")
    d.init_schema()
    assert "rollouts" in sa.inspect(d.engine).get_table_names()
    d.engine.dispose()


# =====================================================================================
# store.create_rollout / finish_rollout / get_rollout / list_rollouts
# =====================================================================================


def test_create_rollout_defaults_running(client: TestClient) -> None:
    row = store.create_rollout(
        rollout_id="ro-1", service_name="svc-a", namespace="ns-a",
        mode="restart", trigger="manual", target="v2", force=False, rolling_task_id="task-1")
    assert row.id == "ro-1"
    assert row.status == "running"
    assert row.frozen is False
    assert row.service_name == "svc-a"
    assert row.namespace == "ns-a"
    assert row.mode == "restart"
    assert row.trigger == "manual"
    assert row.target == "v2"
    assert row.rolling_task_id == "task-1"
    assert row.created_at is not None
    assert row.finished_at is None


def test_finish_rollout_done_not_frozen(client: TestClient) -> None:
    store.create_rollout(rollout_id="ro-done", service_name="svc", rolling_task_id="t")
    out = store.finish_rollout("ro-done", "done")
    assert out is not None
    assert out.status == "done"
    assert out.frozen is False
    assert out.finished_at is not None


def test_finish_rollout_failed_sets_frozen(client: TestClient) -> None:
    # 失败即停 → frozen=True(半迁移态等人工);error 落库。
    store.create_rollout(rollout_id="ro-fail", service_name="svc")
    out = store.finish_rollout("ro-fail", "failed", error="boom", frozen=True, rolling_task_id="t2")
    assert out is not None
    assert out.status == "failed"
    assert out.frozen is True
    assert out.error == "boom"
    assert out.rolling_task_id == "t2"  # None 不覆盖,但这里显式传了 t2 应写入
    assert out.finished_at is not None


def test_finish_rollout_unknown_id_returns_none(client: TestClient) -> None:
    assert store.finish_rollout("nope", "done") is None


def test_finish_rollout_keeps_rolling_task_id_when_none(client: TestClient) -> None:
    # finish 时 rolling_task_id 传 None 不覆盖 create 时已写的值。
    store.create_rollout(rollout_id="ro-keep", service_name="svc", rolling_task_id="task-keep")
    out = store.finish_rollout("ro-keep", "done", rolling_task_id=None)
    assert out is not None
    assert out.rolling_task_id == "task-keep"


def test_get_rollout_roundtrip_and_missing(client: TestClient) -> None:
    store.create_rollout(rollout_id="ro-get", service_name="svc")
    got = store.get_rollout("ro-get")
    assert got is not None and got.id == "ro-get"
    assert store.get_rollout("nope") is None


def test_list_rollouts_desc_by_created_at(client: TestClient) -> None:
    # created_at 倒序;同刻度按 id 倒序兜底(同事务相近时间)→ 后建的在前。
    store.create_rollout(rollout_id="ro-a", service_name="svc")
    store.create_rollout(rollout_id="ro-b", service_name="svc")
    store.create_rollout(rollout_id="ro-c", service_name="svc")
    res = store.list_rollouts()
    assert res["total"] == 3
    ids = [r.id for r in res["rows"]]
    # 最后建的 ro-c 在最前(倒序);ro-a 在最后。
    assert ids[0] == "ro-c"
    assert ids[-1] == "ro-a"


def test_list_rollouts_filters(client: TestClient) -> None:
    store.create_rollout(rollout_id="ro-1", service_name="svc-x", namespace="ns-1")
    store.create_rollout(rollout_id="ro-2", service_name="svc-y", namespace="ns-2")
    store.finish_rollout("ro-2", "failed", frozen=True)

    by_svc = store.list_rollouts(service_name="svc-x")
    assert by_svc["total"] == 1 and by_svc["rows"][0].id == "ro-1"

    by_ns = store.list_rollouts(namespace="ns-2")
    assert by_ns["total"] == 1 and by_ns["rows"][0].id == "ro-2"

    by_status = store.list_rollouts(status="failed")
    assert by_status["total"] == 1 and by_status["rows"][0].id == "ro-2"

    running = store.list_rollouts(status="running")
    assert running["total"] == 1 and running["rows"][0].id == "ro-1"


def test_list_rollouts_pagination(client: TestClient) -> None:
    for i in range(5):
        store.create_rollout(rollout_id=f"ro-{i}", service_name="svc")
    page1 = store.list_rollouts(page=1, page_size=2)
    assert page1["total"] == 5
    assert len(page1["rows"]) == 2
    page3 = store.list_rollouts(page=3, page_size=2)
    assert len(page3["rows"]) == 1  # 5 行,第 3 页只剩 1 行


# =====================================================================================
# 协调器 _run_service_rolling 带 rollout_id:终态回写 rollouts(真库)
# =====================================================================================
#
# 复用 test_rolling.py 的 FakeServiceHubState / _inst / _patch_agg 风格,但因 rollout_id 路径要
# 真写 rollouts 表(store.finish_rollout 经 asyncio.to_thread),故经 `client` fixture 用真库,
# 先 create_rollout 起一条 running,跑协调器后断言该行被回写到 done/failed(+frozen)。


class _FakeDN:
    def __init__(self, agent_id):
        self.agent_id = agent_id


class FakeServiceHubState:
    """跨 agent 协调器桩(照 test_rolling.py 同款):脚本化 list-instances / graceful-restart 回包。

    finish_rolling 在此被吸收(不真写 rolling_tasks);rollout 回写走真 store.finish_rollout。
    """

    def __init__(self, list_results=None, graceful_results=None, *, call_side_effect=None):
        self.list_results = list_results or {}
        self.graceful_results = list(graceful_results or [])
        self.call_side_effect = call_side_effect
        self.calls = []
        self.node_updates = []
        self.finished = None

    async def call_agent(self, agent_id, message, timeout):
        self.calls.append((agent_id, message))
        if self.call_side_effect is not None:
            raise self.call_side_effect
        if message["type"] == "list-instances":
            return self.list_results[agent_id]
        if message["type"] == "graceful-restart":
            return self.graceful_results.pop(0)
        raise AssertionError(f"未预期命令类型: {message['type']}")

    async def update_rolling_nodes(self, task_id, nodes):
        self.node_updates.append([dict(n) for n in nodes])

    async def finish_rolling(self, task_id, status, *, nodes=None, error=None, degraded=False):
        self.finished = {"status": status, "nodes": nodes, "error": error, "degraded": degraded}


class FakeSettings:
    rolling_settle_sec = 1
    rolling_shutdown_timeout = 60
    rolling_ready_timeout = 10
    rolling_cmd_timeout = 30


def _inst(addr, cid, matched=True, healthy=True):
    return {"address": addr, "containerId": cid, "healthy": healthy, "matched": matched}


def _patch_agg(monkeypatch, grouped):
    import app.store as store_mod

    fake = {svc: [_FakeDN(a) for a in agents] for svc, agents in grouped.items()}
    monkeypatch.setattr(store_mod, "aggregate_discovered_by_nacos", lambda status="active": fake)


def test_run_service_rolling_rollout_success_writes_done(client: TestClient, monkeypatch) -> None:
    # 成功路径:协调器把 rollout 回写为 done 且不冻结。
    _patch_agg(monkeypatch, {"svc": ["agent-a", "agent-b"]})
    store.create_rollout(rollout_id="ro-ok", service_name="svc", rolling_task_id="task-ok")
    hub = FakeServiceHubState(
        list_results={
            "agent-a": {"status": "success", "instances": [_inst("ha:1", "a1")]},
            "agent-b": {"status": "success", "instances": [_inst("hb:1", "b1")]},
        },
        graceful_results=[{"status": "success"}, {"status": "success"}],
    )
    asyncio.run(rolling_router._run_service_rolling(
        "task-ok", "svc", False, hub, FakeSettings(), rollout_id="ro-ok"))

    assert hub.finished["status"] == "done"  # rolling_task 终态(被桩吸收)
    row = store.get_rollout("ro-ok")
    assert row.status == "done"
    assert row.frozen is False
    assert row.finished_at is not None
    assert row.rolling_task_id == "task-ok"


def test_run_service_rolling_rollout_failstop_writes_failed_frozen(client: TestClient, monkeypatch) -> None:
    # 某实例 graceful-restart 失败 → 失败即停:rolling nodes 该节点 failed/余下 skipped,
    # 且 rollout 被回写 failed + frozen=True。
    _patch_agg(monkeypatch, {"svc": ["agent-a", "agent-b"]})
    store.create_rollout(rollout_id="ro-fs", service_name="svc", rolling_task_id="task-fs")
    hub = FakeServiceHubState(
        list_results={
            "agent-a": {"status": "success", "instances": [_inst("ha:1", "a1")]},
            "agent-b": {"status": "success", "instances": [_inst("hb:1", "b1"), _inst("hb:2", "b2")]},
        },
        graceful_results=[{"status": "success"}, {"status": "failed", "error": "boom"}],
    )
    asyncio.run(rolling_router._run_service_rolling(
        "task-fs", "svc", False, hub, FakeSettings(), rollout_id="ro-fs"))

    # rolling_task 侧(桩)终态 failed,nodes 失败即停语义。
    assert hub.finished["status"] == "failed"
    nodes = hub.finished["nodes"]
    assert nodes[0]["status"] == "done"
    assert nodes[1]["status"] == "failed" and nodes[1]["error"] == "boom"
    assert nodes[2]["status"] == "skipped"
    # rollout 侧:failed + frozen=True + error 落库。
    row = store.get_rollout("ro-fs")
    assert row.status == "failed"
    assert row.frozen is True
    assert row.error and "失败,停止滚动" in row.error


def test_run_service_rolling_rollout_no_instances_writes_failed_frozen(client: TestClient, monkeypatch) -> None:
    # 聚合里无该服务(nacos 未发现活跃实例)→ rollout failed + frozen(也走 freeze)。
    _patch_agg(monkeypatch, {"other": ["agent-a"]})
    store.create_rollout(rollout_id="ro-none", service_name="svc", rolling_task_id="task-none")
    hub = FakeServiceHubState()
    asyncio.run(rolling_router._run_service_rolling(
        "task-none", "svc", True, hub, FakeSettings(), rollout_id="ro-none"))

    assert hub.finished["status"] == "failed"
    row = store.get_rollout("ro-none")
    assert row.status == "failed"
    assert row.frozen is True


def test_run_service_rolling_without_rollout_id_unchanged(client: TestClient, monkeypatch) -> None:
    # rollout_id=None(裸滚):行为与改造前一致 —— 只写 rolling_task(桩),不碰 rollouts 表。
    _patch_agg(monkeypatch, {"svc": ["agent-a"]})
    hub = FakeServiceHubState(
        list_results={"agent-a": {"status": "success", "instances": [_inst("ha:1", "a1"), _inst("ha:2", "a2")]}},
        graceful_results=[{"status": "success"}, {"status": "success"}],
    )
    asyncio.run(rolling_router._run_service_rolling("task-bare", "svc", False, hub, FakeSettings()))
    assert hub.finished["status"] == "done"
    # 没建过任何 rollout 行。
    assert store.list_rollouts()["total"] == 0


# =====================================================================================
# 端点:POST /api/rollouts
# =====================================================================================


def test_rollouts_router_mounted_requires_admin_token(hub_client: TestClient) -> None:
    # 未带 token → 403(证明白名单放行后由端点首行 _require_admin_token 把关),而非 401 / 404。
    resp = hub_client.post("/api/rollouts", json={"serviceName": "s"})
    assert resp.status_code == 403
    assert resp.json() == {"detail": "Invalid admin token"}


def test_create_rollout_returns_ids(hub_client: TestClient) -> None:
    # happy path 端点契约:返 rolloutId + taskId。后台协调器无发现实例会很快 finish(failed),只验契约。
    resp = hub_client.post("/api/rollouts", json={"serviceName": "svc-ep"}, headers=ADMIN)
    assert resp.status_code == 200
    body = resp.json()
    assert "rolloutId" in body and "taskId" in body
    # rollout 行已建(running 或已被后台回写 failed,都算建成功)。
    import app.main as main_module

    row = asyncio.run(asyncio.to_thread(store.get_rollout, body["rolloutId"]))
    assert row is not None
    assert row.service_name == "svc-ep"
    assert main_module.hub_state is not None


def test_create_rollout_invalid_mode_returns_422(hub_client: TestClient) -> None:
    # mode 非 restart → 422 占位(不假装能跑 pull-redeploy);且不建任何 rollout 行。
    resp = hub_client.post(
        "/api/rollouts", json={"serviceName": "svc-422", "mode": "pull-redeploy"}, headers=ADMIN)
    assert resp.status_code == 422
    assert "pull-redeploy" in resp.json()["detail"]
    assert store.list_rollouts(service_name="svc-422")["total"] == 0


def test_create_rollout_conflict_returns_409_no_orphan(hub_client: TestClient) -> None:
    # 同 serviceName 已占锁(active_key=service_name)→ 再投放 → 409,且**不留半条 running 孤儿 rollout**。
    import app.main as main_module

    state = main_module.hub_state
    asyncio.run(state.create_rolling_task("task-occ", "*", "svc-conf", False, active_key="svc-conf"))

    resp = hub_client.post("/api/rollouts", json={"serviceName": "svc-conf"}, headers=ADMIN)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "同服务投放进行中"
    # 关键:撞锁时 rollout 行一条都没建(先建锁后建 rollout 的顺序保证无孤儿)。
    assert store.list_rollouts(service_name="svc-conf")["total"] == 0


# =====================================================================================
# 端点:GET /api/rollouts(列表 + 过滤)、GET /api/rollouts/{id}(含 rollingTask 嵌入)
# =====================================================================================


def test_list_rollouts_endpoint_filter_and_envelope(hub_client: TestClient) -> None:
    store.create_rollout(rollout_id="ro-l1", service_name="svc-l", namespace="ns-l")
    store.create_rollout(rollout_id="ro-l2", service_name="svc-other", namespace="ns-l")

    resp = hub_client.get("/api/rollouts", params={"serviceName": "svc-l"}, headers=ADMIN)
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["page"] == 1 and "totalPage" in body
    assert body["rows"][0]["serviceName"] == "svc-l"
    # camelCase 钉死:无 snake key。
    row = body["rows"][0]
    for snake in ("service_name", "previous_target", "rolling_task_id", "created_at"):
        assert snake not in row


def test_list_rollouts_endpoint_requires_admin_token(hub_client: TestClient) -> None:
    assert hub_client.get("/api/rollouts").status_code == 403


def test_get_rollout_endpoint_embeds_rolling_task(hub_client: TestClient) -> None:
    # GET 单条嵌入 rollingTask(取自 hub_state.get_rolling_task)。
    import app.main as main_module

    state = main_module.hub_state
    asyncio.run(state.create_rolling_task("task-embed", "*", "svc-embed", False, active_key="svc-embed"))
    asyncio.run(state.update_rolling_nodes("task-embed", [{"address": "h:1", "status": "done"}]))
    store.create_rollout(rollout_id="ro-embed", service_name="svc-embed", rolling_task_id="task-embed")

    resp = hub_client.get("/api/rollouts/ro-embed", headers=ADMIN)
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "ro-embed"
    assert body["rollingTask"] is not None
    assert body["rollingTask"]["taskId"] == "task-embed"
    assert body["rollingTask"]["nodes"][0]["address"] == "h:1"


def test_get_rollout_endpoint_no_task_id_null_embed(hub_client: TestClient) -> None:
    # 无 rolling_task_id → rollingTask 为 None(不报错)。
    store.create_rollout(rollout_id="ro-noembed", service_name="svc", rolling_task_id=None)
    resp = hub_client.get("/api/rollouts/ro-noembed", headers=ADMIN)
    assert resp.status_code == 200
    assert resp.json()["rollingTask"] is None


def test_get_rollout_endpoint_404(hub_client: TestClient) -> None:
    resp = hub_client.get("/api/rollouts/nope", headers=ADMIN)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "投放记录不存在"


# =====================================================================================
# 端点:retry / rollback 状态机
# =====================================================================================


def test_retry_only_failed_allowed(hub_client: TestClient) -> None:
    # failed 才放行 retry;新建 trigger=retry 的 rollout,返回新 ids。
    store.create_rollout(rollout_id="ro-retry", service_name="svc-retry", namespace="ns-r", force=True)
    store.finish_rollout("ro-retry", "failed", error="x", frozen=True)

    resp = hub_client.post("/api/rollouts/ro-retry/retry", headers=ADMIN)
    assert resp.status_code == 200
    body = resp.json()
    assert body["rolloutId"] != "ro-retry"  # 新 rollout
    new = store.get_rollout(body["rolloutId"])
    assert new.trigger == "retry"
    assert new.service_name == "svc-retry"
    assert new.namespace == "ns-r"
    assert new.force is True  # 复用原 force


def test_retry_non_failed_returns_409(hub_client: TestClient) -> None:
    # running 状态 retry → 409。
    store.create_rollout(rollout_id="ro-run", service_name="svc")
    resp = hub_client.post("/api/rollouts/ro-run/retry", headers=ADMIN)
    assert resp.status_code == 409
    assert "仅 failed" in resp.json()["detail"]


def test_retry_missing_returns_404(hub_client: TestClient) -> None:
    resp = hub_client.post("/api/rollouts/nope/retry", headers=ADMIN)
    assert resp.status_code == 404


def test_retry_requires_admin_token(hub_client: TestClient) -> None:
    assert hub_client.post("/api/rollouts/x/retry").status_code == 403


def test_rollback_requires_previous_target(hub_client: TestClient) -> None:
    # failed 但无 previous_target → 409(无上一版可回滚)。
    store.create_rollout(rollout_id="ro-rb1", service_name="svc")
    store.finish_rollout("ro-rb1", "failed", frozen=True)
    resp = hub_client.post("/api/rollouts/ro-rb1/rollback", headers=ADMIN)
    assert resp.status_code == 409
    assert "无上一版可回滚" in resp.json()["detail"]


def test_rollback_failed_with_previous_target_allowed(hub_client: TestClient) -> None:
    # failed + previous_target 非空 → 放行;新建 trigger=rollback 的 rollout,target=上一版。
    store.create_rollout(
        rollout_id="ro-rb2", service_name="svc-rb", target="v3", previous_target="v2")
    store.finish_rollout("ro-rb2", "failed", frozen=True)

    resp = hub_client.post("/api/rollouts/ro-rb2/rollback", headers=ADMIN)
    assert resp.status_code == 200
    body = resp.json()
    new = store.get_rollout(body["rolloutId"])
    assert new.trigger == "rollback"
    assert new.service_name == "svc-rb"
    assert new.target == "v2"            # 回滚目标 = 原上一版
    assert new.previous_target == "v3"   # 回滚后的上一版 = 回滚前的当前 target


def test_rollback_non_failed_returns_409(hub_client: TestClient) -> None:
    store.create_rollout(rollout_id="ro-rb3", service_name="svc", previous_target="v1")
    resp = hub_client.post("/api/rollouts/ro-rb3/rollback", headers=ADMIN)
    assert resp.status_code == 409
    assert "仅 failed" in resp.json()["detail"]


def test_rollback_missing_returns_404(hub_client: TestClient) -> None:
    resp = hub_client.post("/api/rollouts/nope/rollback", headers=ADMIN)
    assert resp.status_code == 404


def test_rollback_requires_admin_token(hub_client: TestClient) -> None:
    assert hub_client.post("/api/rollouts/x/rollback").status_code == 403
