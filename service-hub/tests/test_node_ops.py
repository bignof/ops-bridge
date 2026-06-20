from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.db import Database
from app.main import app
from app.store import HubState


# === 测试基座(与 test_api.py 同构,本文件自带以便独立运行) ===


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    database = Database("sqlite:///" + str(tmp_path / "test.db"))
    test_state = HubState(heartbeat_timeout=90, command_history_limit=200, database=database)
    database.init_schema()

    import app.main as main_module

    old_database = main_module.database
    old_hub_state = main_module.hub_state
    old_admin_token = main_module.settings.admin_token
    main_module.database = database
    main_module.hub_state = test_state
    object.__setattr__(main_module.settings, "admin_token", "test-admin-token")
    app.dependency_overrides = {}

    with TestClient(app) as test_client:
        yield test_client

    database.engine.dispose()
    main_module.database = old_database
    main_module.hub_state = old_hub_state
    object.__setattr__(main_module.settings, "admin_token", old_admin_token)


@pytest.fixture(autouse=True)
def _reset_force_guard():
    # 每个用例前后都清空滑窗,避免速率计数跨用例串味(进程级模块状态)。
    from app import force_guard

    force_guard.reset_force_rate_limit()
    yield
    force_guard.reset_force_rate_limit()


class FakeSocket:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.messages.append(payload)


def attach_agent(state: HubState, agent_id: str, remote: str = "127.0.0.1:12345") -> FakeSocket:
    state._register_agent_sync(agent_id, remote)
    socket = FakeSocket()
    state._connections[agent_id] = socket  # type: ignore[assignment]
    return socket


def _mock_call_agent(state: HubState, monkeypatch: pytest.MonkeyPatch, *, healthy: int = 2, status: str = "success") -> dict[str, Any]:
    """把 hub_state.call_agent 替换成返回指定健康实例数的 stub;返回记录调用次数的计数器 dict。

    counter["calls"] 记录被调用次数,用于断言"非 force 未触发 list-instances"。
    """
    counter: dict[str, Any] = {"calls": 0, "messages": []}
    instances = [
        {"address": f"h:{1800 + i}", "containerId": f"c{i}", "healthy": True, "matched": True}
        for i in range(healthy)
    ]

    async def fake_call_agent(agent_id: str, message: dict, timeout: float) -> dict:
        counter["calls"] += 1
        counter["messages"].append(message)
        return {"status": status, "instances": instances}

    monkeypatch.setattr(state, "call_agent", fake_call_agent)
    return counter


def _force_stop_body(request_id: str, *, service_name: str | None = "memory-share", allow_last: bool | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "requestId": request_id,
        "action": "stop",
        "mode": "force",
        "dir": "/srv/a",
    }
    if service_name is not None:
        body["serviceName"] = service_name
    if allow_last is not None:
        body["allowLastInstance"] = allow_last
    return body


def _post(client: TestClient, body: dict[str, Any]):
    return client.post(
        "/api/agents/agent-a/commands",
        headers={"X-Admin-Token": "test-admin-token"},
        json=body,
    )


# === ① 速率护栏 ===


def test_force_stop_rate_limit_blocks_after_window_max(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # 把窗口上限压成 2:连发 3 次 force stop,前 2 次过(202),第 3 次被速率拒(429)。
    import app.main as main_module

    state = main_module.hub_state
    attach_agent(state, "agent-a")
    # 返回 2 healthy,确保不被"最后健康实例"闸拦,隔离出速率闸。
    _mock_call_agent(state, monkeypatch, healthy=2)
    object.__setattr__(main_module.settings, "force_op_max_per_window", 2)

    r1 = _post(client, _force_stop_body("req-rl-1"))
    r2 = _post(client, _force_stop_body("req-rl-2"))
    r3 = _post(client, _force_stop_body("req-rl-3"))

    assert r1.status_code == 202, r1.text
    assert r2.status_code == 202, r2.text
    assert r3.status_code == 429, r3.text
    assert "速率" in r3.json()["detail"]


def test_force_stop_rate_limited_command_not_persisted(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # 被速率拒的 force 操作不入库:GET 查不到该 request_id。
    import app.main as main_module

    state = main_module.hub_state
    attach_agent(state, "agent-a")
    _mock_call_agent(state, monkeypatch, healthy=2)
    object.__setattr__(main_module.settings, "force_op_max_per_window", 1)

    assert _post(client, _force_stop_body("req-rl-ok")).status_code == 202
    blocked = _post(client, _force_stop_body("req-rl-blocked"))
    assert blocked.status_code == 429

    got = client.get("/api/commands/req-rl-blocked", headers={"X-Admin-Token": "test-admin-token"})
    assert got.status_code == 404


# === ② 不可停最后一个健康实例 ===


def test_force_stop_last_healthy_instance_rejected(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # 仅 1 个健康实例 → force stop 被拒(409)。
    import app.main as main_module

    state = main_module.hub_state
    attach_agent(state, "agent-a")
    _mock_call_agent(state, monkeypatch, healthy=1)

    r = _post(client, _force_stop_body("req-last-1"))
    assert r.status_code == 409, r.text
    assert "最后一个健康实例" in r.json()["detail"]
    # 被拒不入库
    got = client.get("/api/commands/req-last-1", headers={"X-Admin-Token": "test-admin-token"})
    assert got.status_code == 404


def test_force_stop_allow_last_instance_bypasses_check(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # allowLastInstance=true → 跳过最后健康实例校验,即便只有 1 个也放行(202),且不调 list-instances。
    import app.main as main_module

    state = main_module.hub_state
    attach_agent(state, "agent-a")
    counter = _mock_call_agent(state, monkeypatch, healthy=1)

    r = _post(client, _force_stop_body("req-last-allow", allow_last=True))
    assert r.status_code == 202, r.text
    assert counter["calls"] == 0  # 显式解锁后不应再去查 list-instances


def test_force_stop_two_healthy_allowed(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # 2 个健康实例 → 放行(202)。
    import app.main as main_module

    state = main_module.hub_state
    attach_agent(state, "agent-a")
    counter = _mock_call_agent(state, monkeypatch, healthy=2)

    r = _post(client, _force_stop_body("req-two-ok"))
    assert r.status_code == 202, r.text
    assert counter["calls"] == 1  # 查过一次 list-instances


# === list-instances 失败 → fail-closed ===


def test_force_stop_list_instances_failed_fail_closed(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # call_agent 返回 status=failed → 无法核实健康数,fail-closed 拒(409)。
    import app.main as main_module

    state = main_module.hub_state
    attach_agent(state, "agent-a")
    _mock_call_agent(state, monkeypatch, status="failed")

    r = _post(client, _force_stop_body("req-failclosed-1"))
    assert r.status_code == 409, r.text
    assert "无法核实" in r.json()["detail"]


def test_force_stop_list_instances_failed_but_allow_last_passes(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # list-instances 失败但 allowLastInstance=true → 跳过校验,放行(202)。
    import app.main as main_module

    state = main_module.hub_state
    attach_agent(state, "agent-a")
    counter = _mock_call_agent(state, monkeypatch, status="failed")

    r = _post(client, _force_stop_body("req-failclosed-allow", allow_last=True))
    assert r.status_code == 202, r.text
    assert counter["calls"] == 0


# === 非 force 不受闸 ===


def test_graceful_stop_not_rate_limited_nor_instance_checked(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # mode=graceful 的 stop:不查 list-instances、不计速率(把窗口压到 1 仍能连发多次)。
    import app.main as main_module

    state = main_module.hub_state
    attach_agent(state, "agent-a")
    counter = _mock_call_agent(state, monkeypatch, healthy=1)  # 即便只有 1 healthy 也不该被拦
    object.__setattr__(main_module.settings, "force_op_max_per_window", 1)

    body = {"requestId": "req-graceful-1", "action": "stop", "mode": "graceful", "dir": "/srv/a", "serviceName": "memory-share"}
    r1 = client.post("/api/agents/agent-a/commands", headers={"X-Admin-Token": "test-admin-token"}, json=body)
    body["requestId"] = "req-graceful-2"
    r2 = client.post("/api/agents/agent-a/commands", headers={"X-Admin-Token": "test-admin-token"}, json=body)

    assert r1.status_code == 202, r1.text
    assert r2.status_code == 202, r2.text  # 未计速率,第 2 次仍过
    assert counter["calls"] == 0  # 未查 list-instances


def test_plain_stop_without_mode_not_guarded(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # 无 mode 的 stop:同样不受闸。
    import app.main as main_module

    state = main_module.hub_state
    attach_agent(state, "agent-a")
    counter = _mock_call_agent(state, monkeypatch, healthy=1)
    object.__setattr__(main_module.settings, "force_op_max_per_window", 1)

    body = {"requestId": "req-plainstop-1", "action": "stop", "dir": "/srv/a", "serviceName": "memory-share"}
    r1 = client.post("/api/agents/agent-a/commands", headers={"X-Admin-Token": "test-admin-token"}, json=body)
    body["requestId"] = "req-plainstop-2"
    r2 = client.post("/api/agents/agent-a/commands", headers={"X-Admin-Token": "test-admin-token"}, json=body)

    assert r1.status_code == 202, r1.text
    assert r2.status_code == 202, r2.text
    assert counter["calls"] == 0


def test_force_restart_not_guarded_by_this_task(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # 本任务护栏仅作用于 force stop;force-restart(action=force-restart)不在闸内,不查 list-instances、不计速率。
    import app.main as main_module

    state = main_module.hub_state
    attach_agent(state, "agent-a")
    counter = _mock_call_agent(state, monkeypatch, healthy=1)
    object.__setattr__(main_module.settings, "force_op_max_per_window", 1)

    body = {"requestId": "req-frestart-1", "action": "force-restart", "mode": "force", "dir": "/srv/a", "serviceName": "memory-share"}
    r1 = client.post("/api/agents/agent-a/commands", headers={"X-Admin-Token": "test-admin-token"}, json=body)
    body["requestId"] = "req-frestart-2"
    r2 = client.post("/api/agents/agent-a/commands", headers={"X-Admin-Token": "test-admin-token"}, json=body)

    assert r1.status_code == 202, r1.text
    assert r2.status_code == 202, r2.text
    assert counter["calls"] == 0


# === force stop 无 service_name:不阻断,跳过 ②,但告警 ===


def test_force_stop_without_service_name_passes_but_warns(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import app.main as main_module

    state = main_module.hub_state
    attach_agent(state, "agent-a")
    counter = _mock_call_agent(state, monkeypatch, healthy=1)

    with caplog.at_level("WARNING", logger="app.main"):
        r = _post(client, _force_stop_body("req-nosvc-1", service_name=None))

    assert r.status_code == 202, r.text
    assert counter["calls"] == 0  # 无 service_name 跳过最后健康实例校验
    assert any("service_name" in record.getMessage() for record in caplog.records)


def test_force_stop_without_service_name_still_rate_limited(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # 无 service_name 只跳过 ②,① 速率仍生效。
    import app.main as main_module

    state = main_module.hub_state
    attach_agent(state, "agent-a")
    _mock_call_agent(state, monkeypatch, healthy=1)
    object.__setattr__(main_module.settings, "force_op_max_per_window", 1)

    r1 = _post(client, _force_stop_body("req-nosvc-rl-1", service_name=None))
    r2 = _post(client, _force_stop_body("req-nosvc-rl-2", service_name=None))
    assert r1.status_code == 202, r1.text
    assert r2.status_code == 429, r2.text


# === force_guard 单元:滑窗剪枝与 reset ===


def test_force_guard_unit_rate_limit_and_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import force_guard
    import app.main as main_module
    from fastapi import HTTPException

    force_guard.reset_force_rate_limit()
    object.__setattr__(main_module.settings, "force_op_max_per_window", 2)

    force_guard.check_force_rate_limit(main_module.settings)
    force_guard.check_force_rate_limit(main_module.settings)
    with pytest.raises(HTTPException) as exc:
        force_guard.check_force_rate_limit(main_module.settings)
    assert exc.value.status_code == 429

    # reset 后重新可用
    force_guard.reset_force_rate_limit()
    force_guard.check_force_rate_limit(main_module.settings)


def test_force_guard_unit_window_slides(monkeypatch: pytest.MonkeyPatch) -> None:
    # 时间前进越过窗口后,旧时间戳被剪掉,配额恢复。
    from app import force_guard
    import app.main as main_module
    from fastapi import HTTPException

    force_guard.reset_force_rate_limit()
    object.__setattr__(main_module.settings, "force_op_max_per_window", 2)
    object.__setattr__(main_module.settings, "force_op_window_sec", 60)

    fake_now = {"t": 1000.0}
    monkeypatch.setattr(force_guard.time, "monotonic", lambda: fake_now["t"])

    force_guard.check_force_rate_limit(main_module.settings)
    force_guard.check_force_rate_limit(main_module.settings)
    with pytest.raises(HTTPException):
        force_guard.check_force_rate_limit(main_module.settings)

    # 时间前进 61s,越过窗口,旧两条被剪掉
    fake_now["t"] = 1061.0
    force_guard.check_force_rate_limit(main_module.settings)  # 不应再抛
