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
from app.auth import issue_token
from app.db import Database
from app.db_models import Rollout, Service
from app.hub.routers import rolling as rolling_router

# 鉴权改平台 JWT(Rollout 端点从 X-Admin-Token → require_session,供 SPA Bearer 直调)。
# 直接用 issue_token 签一个 admin 会话 token(settings.jwt_secret 来自 conftest env,确定性、
# 不依赖 fixture/HTTP login),组成 Bearer 头给所有端点测试复用。
ADMIN = {"Authorization": f"Bearer {issue_token('admin')}"}


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
    # restart 路径只读 .agent_id;pull-redeploy 还读 .container_id/.dir(对齐真实 nullable 列,默认 None)。
    def __init__(self, agent_id, container_id=None, dir=None):
        self.agent_id = agent_id
        self.container_id = container_id
        self.dir = dir


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
    list_instances_timeout = 5  # P4-6 预热用的短超时(prewarm 旁路加速,不该久等)


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


def test_rollouts_router_mounted_requires_jwt(hub_client: TestClient) -> None:
    # 鉴权改 JWT:未带 Bearer → 401(/api/** default-deny 中间件先挡,已从白名单移除 /api/rollouts),
    # 而非旧的 admin-token 403。证明端点已并入平台 JWT 体系(SPA 可直调)。
    resp = hub_client.post("/api/rollouts", json={"serviceName": "s"})
    assert resp.status_code == 401


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


def _capture_coordinator(monkeypatch) -> dict:
    """把 rollouts 路由引用的 _run_service_rolling 换成不真跑的 async 桩,捕获透传的 kwargs。

    rollouts.py 经 `from ...rolling import _run_service_rolling` 绑进自身命名空间,故在
    `app.hub.routers.rollouts` 上 patch 才生效。桩什么都不做(后台任务空跑),只记录入参 —— 让端点
    层断言「body.instances → instance_filter」这一透传契约,不依赖真协调器/真 agent 连接。
    """
    import app.hub.routers.rollouts as rollouts_mod

    captured: dict = {}

    async def fake_coordinator(task_id, service_name, force, hub_state, settings, **kwargs):
        captured["task_id"] = task_id
        captured["service_name"] = service_name
        captured["force"] = force
        captured["rollout_id"] = kwargs.get("rollout_id")
        captured["instance_filter"] = kwargs.get("instance_filter")
        captured["mode"] = kwargs.get("mode")
        captured["image"] = kwargs.get("image")

    monkeypatch.setattr(rollouts_mod, "_run_service_rolling", fake_coordinator)
    return captured


def test_create_rollout_with_instances_passes_subset_filter(hub_client: TestClient, monkeypatch) -> None:
    # P5-2:body.instances 非空 → 透传成 instance_filter=set(instances)(灰度只滚子集)。
    captured = _capture_coordinator(monkeypatch)
    resp = hub_client.post(
        "/api/rollouts", json={"serviceName": "svc-canary", "instances": ["c1", "c2"]}, headers=ADMIN)
    assert resp.status_code == 200
    body = resp.json()
    assert "rolloutId" in body and "taskId" in body
    # 协调器拿到的 instance_filter 是这两个 containerId 的集合。
    assert captured["instance_filter"] == {"c1", "c2"}
    assert captured["service_name"] == "svc-canary"
    assert captured["rollout_id"] == body["rolloutId"]


def test_create_rollout_without_instances_filter_is_none(hub_client: TestClient, monkeypatch) -> None:
    # 不带 instances → instance_filter=None(全量,回归)。
    captured = _capture_coordinator(monkeypatch)
    resp = hub_client.post("/api/rollouts", json={"serviceName": "svc-full"}, headers=ADMIN)
    assert resp.status_code == 200
    assert captured["instance_filter"] is None


def test_create_rollout_empty_instances_filter_is_none(hub_client: TestClient, monkeypatch) -> None:
    # 空列表 instances → 视同全量(instance_filter=None),不是「子集为空」。
    captured = _capture_coordinator(monkeypatch)
    resp = hub_client.post(
        "/api/rollouts", json={"serviceName": "svc-empty", "instances": []}, headers=ADMIN)
    assert resp.status_code == 200
    assert captured["instance_filter"] is None


def test_create_rollout_subset_rolls_only_that_container_e2e(hub_client: TestClient, monkeypatch) -> None:
    # 端到端(真协调器):mock list-instances 返回 2 个 matched+healthy 实例,instances 只点 1 个 →
    # 只对该 containerId 发 graceful-restart(另一个不动)。验证 body→协调器→agent 整条链的灰度行为。
    import app.main as main_module
    import app.store as store_mod
    from app.hub.routers import rolling as rolling_mod

    state = main_module.hub_state

    # 聚合定位到单 agent(承载该服务);restart 路径只读 .agent_id,container_id/dir 默认 None。
    class _FakeDN:
        def __init__(self, agent_id):
            self.agent_id = agent_id
            self.container_id = None
            self.dir = None

    monkeypatch.setattr(
        store_mod, "aggregate_discovered_by_nacos",
        lambda status="active": {"svc-e2e": [_FakeDN("agent-e2e")]})

    calls: list = []

    async def fake_call_agent(agent_id, message, timeout):
        calls.append((agent_id, message))
        if message["type"] == "list-instances":
            return {"status": "success", "instances": [
                {"address": "ha:1", "containerId": "a1", "healthy": True, "matched": True},
                {"address": "ha:2", "containerId": "a2", "healthy": True, "matched": True},
            ]}
        return {"status": "success"}  # graceful-restart

    monkeypatch.setattr(state, "call_agent", fake_call_agent)

    resp = hub_client.post(
        "/api/rollouts", json={"serviceName": "svc-e2e", "instances": ["a2"]}, headers=ADMIN)
    assert resp.status_code == 200
    rollout_id = resp.json()["rolloutId"]

    # 后台任务异步跑;等它把 rollout 推进到终态(done)。
    async def _await_done():
        for _ in range(100):
            row = await asyncio.to_thread(store.get_rollout, rollout_id)
            if row is not None and row.status in ("done", "degraded", "failed"):
                return row
            await asyncio.sleep(0.02)
        return await asyncio.to_thread(store.get_rollout, rollout_id)

    row = asyncio.run(_await_done())
    assert row.status == "done"
    # 只对 a2(灰度子集)发了 graceful-restart,a1 未被滚。
    gr = [m for _, m in calls if m["type"] == "graceful-restart"]
    assert len(gr) == 1
    assert gr[0]["containerId"] == "a2"


def test_create_rollout_invalid_mode_returns_422(hub_client: TestClient) -> None:
    # mode 既非 restart 也非 pull-redeploy → 422;且不建任何 rollout 行。
    resp = hub_client.post(
        "/api/rollouts", json={"serviceName": "svc-422", "mode": "bogus"}, headers=ADMIN)
    assert resp.status_code == 422
    assert "不支持的投放模式" in resp.json()["detail"]
    assert store.list_rollouts(service_name="svc-422")["total"] == 0


def test_create_rollout_pull_redeploy_without_image_returns_422(hub_client: TestClient) -> None:
    # pull-redeploy 缺 image → 422,且不建 rollout 行。
    resp = hub_client.post(
        "/api/rollouts", json={"serviceName": "svc-pr-noimg", "mode": "pull-redeploy"}, headers=ADMIN)
    assert resp.status_code == 422
    assert "pull-redeploy 投放须指定 image" in resp.json()["detail"]
    assert store.list_rollouts(service_name="svc-pr-noimg")["total"] == 0


def test_create_rollout_pull_redeploy_with_image_passes_mode_and_image(
    hub_client: TestClient, monkeypatch
) -> None:
    # pull-redeploy + image → 200 返 ids,且 mode/image 透传给协调器。
    captured = _capture_coordinator(monkeypatch)
    resp = hub_client.post(
        "/api/rollouts",
        json={"serviceName": "svc-pr", "mode": "pull-redeploy", "image": "reg/app:v2"},
        headers=ADMIN)
    assert resp.status_code == 200
    body = resp.json()
    assert "rolloutId" in body and "taskId" in body
    assert captured["mode"] == "pull-redeploy"
    assert captured["image"] == "reg/app:v2"
    assert captured["service_name"] == "svc-pr"
    # rollout 行已建(mode 落库 pull-redeploy;image 本期不落库)。
    row = store.get_rollout(body["rolloutId"])
    assert row is not None and row.mode == "pull-redeploy"


def test_create_rollout_restart_mode_passes_none_image(hub_client: TestClient, monkeypatch) -> None:
    # restart(缺省/显式)→ 200,mode=restart 透传、image=None(协调器忽略)。
    captured = _capture_coordinator(monkeypatch)
    resp = hub_client.post("/api/rollouts", json={"serviceName": "svc-r"}, headers=ADMIN)
    assert resp.status_code == 200
    assert captured["mode"] == "restart"
    assert captured["image"] is None


def test_retry_pull_redeploy_returns_422_no_image_column(hub_client: TestClient) -> None:
    # pull-redeploy 原投放重试:表无 image 列取不回 → 422,引导走发布弹窗;不建新 rollout。
    store.create_rollout(
        rollout_id="ro-pr-retry", service_name="svc-prr", mode="pull-redeploy", force=False)
    store.finish_rollout("ro-pr-retry", "failed", error="x", frozen=True)

    before = store.list_rollouts(service_name="svc-prr")["total"]
    resp = hub_client.post("/api/rollouts/ro-pr-retry/retry", headers=ADMIN)
    assert resp.status_code == 422
    assert "pull-redeploy 重试需重新指定 image" in resp.json()["detail"]
    # 没建新 rollout(仍只有原来那条)。
    assert store.list_rollouts(service_name="svc-prr")["total"] == before


def test_rollback_pull_redeploy_returns_422_no_image_column(hub_client: TestClient) -> None:
    # pull-redeploy 原投放回滚(即便有 previous_target)→ 422(表无 image 列),不建新 rollout。
    store.create_rollout(
        rollout_id="ro-pr-rb", service_name="svc-prb", mode="pull-redeploy",
        target="reg/app:v3", previous_target="reg/app:v2")
    store.finish_rollout("ro-pr-rb", "failed", frozen=True)

    before = store.list_rollouts(service_name="svc-prb")["total"]
    resp = hub_client.post("/api/rollouts/ro-pr-rb/rollback", headers=ADMIN)
    assert resp.status_code == 422
    assert "pull-redeploy 回滚需重新指定 image" in resp.json()["detail"]
    assert store.list_rollouts(service_name="svc-prb")["total"] == before


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


def test_list_rollouts_endpoint_requires_jwt(hub_client: TestClient) -> None:
    # 鉴权改 JWT:无 Bearer → 401(default-deny)。
    assert hub_client.get("/api/rollouts").status_code == 401


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


def test_retry_requires_jwt(hub_client: TestClient) -> None:
    # 鉴权改 JWT:无 Bearer → 401(default-deny)。
    assert hub_client.post("/api/rollouts/x/retry").status_code == 401


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


def test_rollback_requires_jwt(hub_client: TestClient) -> None:
    # 鉴权改 JWT:无 Bearer → 401(default-deny)。
    assert hub_client.post("/api/rollouts/x/rollback").status_code == 401


# =====================================================================================
# P4-6 best-effort 预热(prewarm):restart 投放前通知 agent 预拉插件缓存
# =====================================================================================
#
# 契约(agent 侧):console → agent `{type:"prewarm", requestId, services:[serviceCode...]}`。
# 铁律:预热失败/超时**绝不**影响投放(返回/时序/抢锁顺序不变);只 restart 预热,pull-redeploy 不预热。
#
# 单测分两层:① 直调 `_prewarm_service`(配 FakePrewarmHub + mock 聚合/Service 查询)精确断言
# 发了什么/吞了异常;② 端点层(hub_client + _capture_coordinator 桩掉真协调器)断言 restart 触发预热、
# pull-redeploy 不触发,且预热不改变 {rolloutId, taskId} 契约。


class FakePrewarmHub:
    """prewarm 专用 hub 桩:记录每次 call_agent 的 (agent_id, message);可选注入异常验证 best-effort 吞错。

    `raise_for`:agent_id → 要抛的异常(模拟该 agent 超时/连接不可用);其余 agent 正常记录回 success。
    """

    def __init__(self, *, raise_for: dict | None = None):
        self.calls: list = []
        self.raise_for = raise_for or {}

    async def call_agent(self, agent_id, message, timeout):
        self.calls.append((agent_id, message, timeout))
        if agent_id in self.raise_for:
            raise self.raise_for[agent_id]
        return {"type": "prewarm-result", "requestId": message["requestId"], "status": "success", "warmed": 1}


def _mk_service(namespace_id: int, service_code: str, nacos_service_name: str | None):
    # 建一条 Service 行(created_at/updated_at 由 create_row 自动补);供 serviceCode 解析用例使用。
    return store.create_row(
        Service,
        {"namespace_id": namespace_id, "service_code": service_code, "nacos_service_name": nacos_service_name},
    )


def test_prewarm_sends_prewarm_with_service_codes_to_each_agent(client: TestClient, monkeypatch) -> None:
    # 承载该 nacos 服务的两个 agent;nacos 名映射到一个 serviceCode → 给每个 agent 发 type=prewarm + 该 code。
    from app.hub.routers import rollouts as rollouts_mod

    _patch_agg(monkeypatch, {"svc-nacos": ["agent-a", "agent-b"]})
    _mk_service(1, "svc-code", "svc-nacos")
    hub = FakePrewarmHub()

    asyncio.run(rollouts_mod._prewarm_service("svc-nacos", hub, FakeSettings()))

    # 两个 agent 各被发了一次 prewarm,services 含正确 serviceCode,且用短超时(list_instances_timeout)。
    assert len(hub.calls) == 2
    assert {c[0] for c in hub.calls} == {"agent-a", "agent-b"}
    for _agent, msg, timeout in hub.calls:
        assert msg["type"] == "prewarm"
        assert msg["services"] == ["svc-code"]
        assert "requestId" in msg
        assert timeout == FakeSettings.list_instances_timeout


def test_prewarm_dedupes_multiple_service_codes(client: TestClient, monkeypatch) -> None:
    # 同 nacos 名跨多 namespace 有多 Service 行 → 收集去重全部 serviceCode 一并发(顺序按 id)。
    from app.hub.routers import rollouts as rollouts_mod

    _patch_agg(monkeypatch, {"svc-nacos": ["agent-a"]})
    _mk_service(1, "code-1", "svc-nacos")
    _mk_service(2, "code-2", "svc-nacos")
    _mk_service(3, "code-dup", "other-nacos")  # 不同 nacos 名,不应入选
    hub = FakePrewarmHub()

    asyncio.run(rollouts_mod._prewarm_service("svc-nacos", hub, FakeSettings()))

    assert len(hub.calls) == 1
    assert hub.calls[0][1]["services"] == ["code-1", "code-2"]


def test_prewarm_swallows_call_agent_exception(client: TestClient, monkeypatch) -> None:
    # best-effort:某 agent call_agent 抛异常(TimeoutError/任意 Exception)→ 不冒泡,继续发下一个 agent。
    from app.hub.routers import rollouts as rollouts_mod

    _patch_agg(monkeypatch, {"svc-nacos": ["agent-bad", "agent-good"]})
    _mk_service(1, "svc-code", "svc-nacos")
    hub = FakePrewarmHub(raise_for={"agent-bad": asyncio.TimeoutError()})

    # 不抛(异常被吞);两个 agent 都被尝试过(失败的也算尝试)。
    asyncio.run(rollouts_mod._prewarm_service("svc-nacos", hub, FakeSettings()))
    assert {c[0] for c in hub.calls} == {"agent-bad", "agent-good"}


def test_prewarm_no_agent_silently_skips(client: TestClient, monkeypatch) -> None:
    # 聚合里无该服务(无承载 agent)→ 静默跳过,一次 call_agent 都不发。
    from app.hub.routers import rollouts as rollouts_mod

    _patch_agg(monkeypatch, {"other": ["agent-a"]})
    _mk_service(1, "svc-code", "svc-nacos")
    hub = FakePrewarmHub()

    asyncio.run(rollouts_mod._prewarm_service("svc-nacos", hub, FakeSettings()))
    assert hub.calls == []


def test_prewarm_no_service_code_mapping_silently_skips(client: TestClient, monkeypatch) -> None:
    # 有承载 agent 但 nacos 名映射不到任何 Service.service_code → 无从预热,静默跳过(不发 prewarm)。
    from app.hub.routers import rollouts as rollouts_mod

    _patch_agg(monkeypatch, {"svc-nacos": ["agent-a"]})
    # 不建任何 nacos_service_name == "svc-nacos" 的 Service 行。
    hub = FakePrewarmHub()

    asyncio.run(rollouts_mod._prewarm_service("svc-nacos", hub, FakeSettings()))
    assert hub.calls == []


def test_prewarm_aggregate_error_is_swallowed(client: TestClient, monkeypatch) -> None:
    # 兜底层:聚合查询本身抛错 → 整个 _prewarm_service 吞掉(不抛),投放调用方永不受影响。
    from app.hub.routers import rollouts as rollouts_mod
    import app.store as store_mod

    def _boom(status="active"):
        raise RuntimeError("db down")

    monkeypatch.setattr(store_mod, "aggregate_discovered_by_nacos", _boom)
    hub = FakePrewarmHub()

    # 不抛异常即通过(兜底 try/except 生效)。
    asyncio.run(rollouts_mod._prewarm_service("svc-nacos", hub, FakeSettings()))
    assert hub.calls == []


def _capture_prewarm(monkeypatch) -> dict:
    """把 rollouts 路由引用的 _prewarm_service 换成 async 桩,捕获是否被调 + 入参(service_name)。

    与 _capture_coordinator 同理:rollouts.py 直接定义/调用 _prewarm_service,在
    `app.hub.routers.rollouts` 模块上 patch 才生效。端点层只验「restart 调、pull-redeploy 不调」这一
    分流契约,不真发 agent 调用。
    """
    import app.hub.routers.rollouts as rollouts_mod

    captured: dict = {"called": False, "service_name": None, "count": 0}

    async def fake_prewarm(service_name, hub_state, settings):
        captured["called"] = True
        captured["service_name"] = service_name
        captured["count"] += 1

    monkeypatch.setattr(rollouts_mod, "_prewarm_service", fake_prewarm)
    return captured


def test_restart_rollout_triggers_prewarm(hub_client: TestClient, monkeypatch) -> None:
    # restart 投放:触发预热(对该服务),且 {rolloutId, taskId} 契约不变(预热不改返回/时序)。
    _capture_coordinator(monkeypatch)  # 桩掉真协调器(后台不真跑)
    captured = _capture_prewarm(monkeypatch)
    resp = hub_client.post("/api/rollouts", json={"serviceName": "svc-warm"}, headers=ADMIN)
    assert resp.status_code == 200
    body = resp.json()
    assert "rolloutId" in body and "taskId" in body
    # 后台预热任务异步起;给事件循环一拍让其执行到桩。
    asyncio.run(asyncio.sleep(0.05))
    assert captured["called"] is True
    assert captured["service_name"] == "svc-warm"


def test_pull_redeploy_rollout_does_not_prewarm(hub_client: TestClient, monkeypatch) -> None:
    # pull-redeploy 投放:拉的是镜像非插件,**不**预热;投放本身照常(返 ids)。
    _capture_coordinator(monkeypatch)
    captured = _capture_prewarm(monkeypatch)
    resp = hub_client.post(
        "/api/rollouts",
        json={"serviceName": "svc-pr-warm", "mode": "pull-redeploy", "image": "reg/app:v2"},
        headers=ADMIN)
    assert resp.status_code == 200
    asyncio.run(asyncio.sleep(0.05))
    assert captured["called"] is False


def test_prewarm_failure_does_not_break_rollout_e2e(hub_client: TestClient, monkeypatch) -> None:
    # 端到端 best-effort:预热里 call_agent 全抛异常,投放仍正常推进到终态(done),异常不冒泡。
    import app.main as main_module
    import app.store as store_mod
    from app.hub.routers import rolling as rolling_mod  # noqa: F401  (确保协调器真跑)

    state = main_module.hub_state

    # 聚合定位到单 agent(承载该服务);_prewarm_service 与协调器都读 .agent_id。
    class _FakeDN:
        def __init__(self, agent_id):
            self.agent_id = agent_id
            self.container_id = None
            self.dir = None

    monkeypatch.setattr(
        store_mod, "aggregate_discovered_by_nacos",
        lambda status="active": {"svc-warm-e2e": [_FakeDN("agent-warm")]})
    # 让 prewarm 的 serviceCode 解析有结果(否则静默跳过,测不到「失败仍不影响」)。
    _mk_service(1, "svc-warm-code", "svc-warm-e2e")

    async def fake_call_agent(agent_id, message, timeout):
        if message["type"] == "prewarm":
            # 预热一律失败 —— 验证 best-effort:绝不影响投放。
            raise asyncio.TimeoutError()
        if message["type"] == "list-instances":
            return {"status": "success", "instances": [
                {"address": "ha:1", "containerId": "a1", "healthy": True, "matched": True},
                {"address": "ha:2", "containerId": "a2", "healthy": True, "matched": True},
            ]}
        return {"status": "success"}  # graceful-restart

    monkeypatch.setattr(state, "call_agent", fake_call_agent)

    resp = hub_client.post("/api/rollouts", json={"serviceName": "svc-warm-e2e"}, headers=ADMIN)
    assert resp.status_code == 200  # 预热失败不影响投放发起
    rollout_id = resp.json()["rolloutId"]

    async def _await_done():
        for _ in range(100):
            row = await asyncio.to_thread(store.get_rollout, rollout_id)
            if row is not None and row.status in ("done", "degraded", "failed"):
                return row
            await asyncio.sleep(0.02)
        return await asyncio.to_thread(store.get_rollout, rollout_id)

    row = asyncio.run(_await_done())
    # 投放照常完成(预热失败被吞,滚动正常滚完两个实例)。
    assert row.status == "done"
