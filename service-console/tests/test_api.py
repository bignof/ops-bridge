from __future__ import annotations

import asyncio
from datetime import timedelta
import inspect
import json
import os
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.db import Database
from app.hub.db_models import AgentModel
from app.main import app
from app.hub.api_support import _handle_agent_message
from app.hub.store import HubState, utc_now


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


class FakeSocket:
    def __init__(self, on_send=None) -> None:
        self.messages: list[dict[str, Any]] = []
        self.on_send = on_send

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.messages.append(payload)
        if self.on_send is None:
            return

        result = self.on_send(payload)
        if inspect.isawaitable(result):
            await result


def attach_agent(state: HubState, agent_id: str, remote: str = "127.0.0.1:12345", on_send=None) -> FakeSocket:
    state._register_agent_sync(agent_id, remote)
    socket = FakeSocket(on_send=on_send)
    state._connections[agent_id] = socket  # type: ignore[assignment]
    return socket


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_list_agents_and_get_agent_return_expected_shape(client: TestClient) -> None:
    import app.main as main_module

    state = main_module.hub_state
    attach_agent(state, "agent-a")

    response = client.get("/api/agents", headers={"X-Admin-Token": "test-admin-token"})

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["agentId"] == "agent-a"
    assert body[0]["connected"] is True
    assert body[0]["online"] is True
    assert body[0]["credentialConfigured"] is False
    assert body[0]["queuedCommands"] == 0
    assert body[0]["processingCommands"] == 0
    assert body[0]["lastCommandCreatedAt"] is None

    agent_response = client.get("/api/agents/agent-a", headers={"X-Admin-Token": "test-admin-token"})

    assert agent_response.status_code == 200
    assert agent_response.json()["agentId"] == "agent-a"


def test_register_frame_exposes_capabilities_in_agent_snapshot(client: TestClient) -> None:
    # #4/#10:agent 发 register(capabilities/agentVersion)后,这些纯内存态须经 get_agent 暴露。
    import app.main as main_module

    state = main_module.hub_state
    attach_agent(state, "agent-a")
    asyncio.run(
        _handle_agent_message(
            "agent-a",
            {
                "type": "register",
                "agentId": "agent-a",
                "capabilities": ["restart", "start", "stop"],
                "agentVersion": "9.9.9",
            },
        )
    )

    response = client.get("/api/agents/agent-a", headers={"X-Admin-Token": "test-admin-token"})

    assert response.status_code == 200
    body = response.json()
    assert body["capabilities"] == ["restart", "start", "stop"]
    assert body["agentVersion"] == "9.9.9"


def test_disconnect_clears_agent_runtime_capabilities(client: TestClient) -> None:
    # #4/#10:断开后在线态(含 capabilities)应被清掉(纯内存态,不残留)。
    import app.main as main_module

    state = main_module.hub_state
    attach_agent(state, "agent-a")
    asyncio.run(state.set_agent_runtime("agent-a", ["restart"], "1.0.0"))
    assert asyncio.run(state.get_agent_runtime("agent-a")) == {"capabilities": ["restart"], "agent_version": "1.0.0"}

    asyncio.run(state.disconnect_agent("agent-a"))

    assert asyncio.run(state.get_agent_runtime("agent-a")) is None


def test_agent_status_includes_queued_and_processing_command_summary(client: TestClient) -> None:
    import app.main as main_module

    state = main_module.hub_state
    attach_agent(state, "agent-a")
    asyncio.run(
        state.store_command(
            "agent-a",
            {"type": "command", "requestId": "req-queued", "action": "restart", "dir": "/srv/a"},
        )
    )
    asyncio.run(
        state.store_command(
            "agent-a",
            {"type": "command", "requestId": "req-processing", "action": "restart", "dir": "/srv/a"},
        )
    )
    asyncio.run(state.mark_ack("req-processing"))

    response = client.get("/api/agents/agent-a", headers={"X-Admin-Token": "test-admin-token"})

    assert response.status_code == 200
    body = response.json()
    assert body["queuedCommands"] == 1
    assert body["processingCommands"] == 1
    assert body["lastCommandCreatedAt"] is not None


def test_rotate_agent_credentials_requires_admin_token_and_persists_state(client: TestClient) -> None:
    forbidden = client.post("/api/agents/agent-a/credentials/rotate")

    assert forbidden.status_code == 403
    assert forbidden.json() == {"detail": "Invalid admin token"}

    response = client.post(
        "/api/agents/agent-a/credentials/rotate",
        headers={"X-Admin-Token": "test-admin-token"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["agentId"] == "agent-a"
    assert body["agentKey"]
    assert body["issuedAt"]
    assert body["created"] is True

    agent_response = client.get("/api/agents/agent-a", headers={"X-Admin-Token": "test-admin-token"})

    assert agent_response.status_code == 200
    assert agent_response.json()["credentialConfigured"] is True
    assert agent_response.json()["keyIssuedAt"] is not None


def test_provision_agent_creates_offline_agent_and_returns_initial_key(client: TestClient) -> None:
    forbidden = client.post("/api/agents", json={"agentId": "agent-new"})

    assert forbidden.status_code == 403
    assert forbidden.json() == {"detail": "Invalid admin token"}

    response = client.post(
        "/api/agents",
        headers={"X-Admin-Token": "test-admin-token"},
        json={"agentId": "agent-new"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["agentKey"]
    assert body["issuedAt"]
    assert body["agent"]["agentId"] == "agent-new"
    assert body["agent"]["connected"] is False
    assert body["agent"]["online"] is False
    assert body["agent"]["credentialConfigured"] is True

    conflict = client.post(
        "/api/agents",
        headers={"X-Admin-Token": "test-admin-token"},
        json={"agentId": "agent-new"},
    )

    assert conflict.status_code == 409
    assert conflict.json() == {"detail": "Agent already exists"}


def test_provisioned_key_remains_valid_even_if_issued_at_is_old(client: TestClient) -> None:
    import app.main as main_module

    response = client.post(
        "/api/agents",
        headers={"X-Admin-Token": "test-admin-token"},
        json={"agentId": "agent-e2e"},
    )

    assert response.status_code == 201
    agent_key = response.json()["agentKey"]

    with main_module.database.session_factory() as session:
        record = session.scalar(select(AgentModel).where(AgentModel.agent_id == "agent-e2e"))
        assert record is not None
        record.key_issued_at = utc_now() - timedelta(days=3650)
        session.commit()

    with client.websocket_connect(f"/ws/agent/agent-e2e?key={agent_key}") as websocket:
        websocket.send_json({"type": "heartbeat"})

    agent_response = client.get("/api/agents/agent-e2e", headers={"X-Admin-Token": "test-admin-token"})
    assert agent_response.status_code == 200
    assert agent_response.json()["credentialConfigured"] is True


def test_get_unknown_agent_returns_404(client: TestClient) -> None:
    response = client.get("/api/agents/missing-agent", headers={"X-Admin-Token": "test-admin-token"})

    assert response.status_code == 404
    assert response.json() == {"detail": "Agent not found"}


def test_dispatch_command_creates_record_and_returns_expected_payload(client: TestClient) -> None:
    import app.main as main_module

    state = main_module.hub_state
    socket = attach_agent(state, "agent-a")

    response = client.post(
        "/api/agents/agent-a/commands",
        headers={
            "X-Admin-Token": "test-admin-token",
            "X-Requested-By": "platform-api",
            "X-Requested-Source": "ops-console",
        },
        json={
            "requestId": "req-dispatch-1",
            "action": "restart",
            "dir": "/srv/a",
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["accepted"] is True
    assert body["command"]["requestId"] == "req-dispatch-1"
    # requested_by 由 hub 据 admin token 服务端派生(X-Requested-By 仅作 hint,不再原样落库)
    assert body["command"]["requestedBy"] == "platform-admin"
    # request_source 维持客户端 X-Requested-Source 原样(本次范围不动)
    assert body["command"]["requestSource"] == "ops-console"
    assert socket.messages[0]["requestId"] == "req-dispatch-1"

    get_response = client.get("/api/commands/req-dispatch-1", headers={"X-Admin-Token": "test-admin-token"})
    assert get_response.status_code == 200
    assert get_response.json()["requestId"] == "req-dispatch-1"


def test_dispatch_graceful_stop_forwards_drain_params_to_agent(client: TestClient) -> None:
    # 优雅 stop:hub 须把 healthBaseUrl + shutdownTimeoutSec 透传进下发帧,供 agent drain。
    import app.main as main_module

    state = main_module.hub_state
    socket = attach_agent(state, "agent-a")

    response = client.post(
        "/api/agents/agent-a/commands",
        headers={"X-Admin-Token": "test-admin-token"},
        json={
            "requestId": "req-graceful-stop-1",
            "action": "stop",
            "mode": "graceful",
            "dir": "/srv/a",
            "healthBaseUrl": "http://10.0.0.5:13000",
            "shutdownTimeoutSec": 45,
        },
    )

    assert response.status_code == 202
    sent = socket.messages[0]
    assert sent["action"] == "stop"
    assert sent["mode"] == "graceful"
    assert sent["healthBaseUrl"] == "http://10.0.0.5:13000"
    assert sent["shutdownTimeoutSec"] == 45


def test_dispatch_derives_requested_by_from_admin_token_ignoring_client_header(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # 安全:客户端自报的 X-Requested-By 不可信,requested_by 必须由 hub 据 admin token 服务端派生。
    # 持有合法 admin token 的调用方即便伪造 X-Requested-By: attacker,落库的 requested_by 也应是派生身份。
    import app.main as main_module

    state = main_module.hub_state
    attach_agent(state, "agent-a")

    with caplog.at_level("INFO", logger="app.main"):
        response = client.post(
            "/api/agents/agent-a/commands",
            headers={
                "X-Admin-Token": "test-admin-token",
                "X-Requested-By": "attacker",
            },
            json={
                "requestId": "req-derive-1",
                "action": "restart",
                "dir": "/srv/a",
            },
        )

    assert response.status_code == 202
    # 响应体即为落库结果:requested_by 是派生身份而非客户端伪造值
    assert response.json()["command"]["requestedBy"] == "platform-admin"

    # 读回 GET /api/commands/{id} 二次确认持久化的是派生值,不是 "attacker"
    get_response = client.get("/api/commands/req-derive-1", headers={"X-Admin-Token": "test-admin-token"})
    assert get_response.status_code == 200
    assert get_response.json()["requestedBy"] == "platform-admin"

    # 客户端自报值仅作 hint 记入日志(便于审计追溯),但不作权威、不入 requested_by
    assert any("attacker" in record.getMessage() for record in caplog.records)


def test_retry_derives_requested_by_from_admin_token_ignoring_client_header(client: TestClient) -> None:
    # retry 同理:重试生成的新命令 requested_by 也必须是派生身份,忽略客户端 X-Requested-By
    import app.main as main_module

    state = main_module.hub_state
    fake_socket = attach_agent(state, "agent-a")
    asyncio.run(state.store_command("agent-a", {"type": "command", "requestId": "req-retry-src", "action": "restart", "dir": "/srv/a"}))
    asyncio.run(state.mark_result("req-retry-src", "failed", error="boom"))

    response = client.post(
        "/api/commands/req-retry-src/retry",
        headers={
            "X-Admin-Token": "test-admin-token",
            "X-Requested-By": "attacker",
        },
    )

    assert response.status_code == 202
    new_request_id = response.json()["command"]["requestId"]
    assert response.json()["command"]["requestedBy"] == "platform-admin"

    get_response = client.get(f"/api/commands/{new_request_id}", headers={"X-Admin-Token": "test-admin-token"})
    assert get_response.status_code == 200
    assert get_response.json()["requestedBy"] == "platform-admin"


def test_dispatch_requires_admin_token(client: TestClient) -> None:
    response = client.post(
        "/api/agents/agent-a/commands",
        json={
            "requestId": "req-no-token",
            "action": "restart",
            "dir": "/srv/a",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid admin token"


def test_dispatch_update_without_image_returns_422(client: TestClient) -> None:
    import app.main as main_module

    state = main_module.hub_state
    attach_agent(state, "agent-a")

    response = client.post(
        "/api/agents/agent-a/commands",
        json={
            "requestId": "req-invalid-update",
            "action": "update",
            "dir": "/srv/a",
        },
    )

    assert response.status_code == 422


def test_dispatch_stop_with_mode_persists_and_returns_202(client: TestClient) -> None:
    # 放宽 action + mode 字段:stop + graceful 受理、下发帧含 mode、持久化可读回
    import app.main as main_module

    state = main_module.hub_state
    socket = attach_agent(state, "agent-a")

    response = client.post(
        "/api/agents/agent-a/commands",
        headers={"X-Admin-Token": "test-admin-token"},
        json={
            "requestId": "req-stop-graceful",
            "action": "stop",
            "mode": "graceful",
            "dir": "/srv/a",
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["accepted"] is True
    assert body["command"]["action"] == "stop"
    assert body["command"]["mode"] == "graceful"
    # 下发给 agent 的帧须带 mode,供 agent 侧 handle_stop 读取
    assert socket.messages[0]["action"] == "stop"
    assert socket.messages[0]["mode"] == "graceful"

    get_response = client.get("/api/commands/req-stop-graceful", headers={"X-Admin-Token": "test-admin-token"})
    assert get_response.status_code == 200
    assert get_response.json()["mode"] == "graceful"


def test_dispatch_accepts_new_actions(client: TestClient) -> None:
    # start / force-restart / pull-redeploy 均应被接受(放宽 Literal)
    import app.main as main_module

    state = main_module.hub_state
    attach_agent(state, "agent-a")

    cases = [
        {"requestId": "req-start", "action": "start", "dir": "/srv/a"},
        {"requestId": "req-force-restart", "action": "force-restart", "mode": "force", "dir": "/srv/a"},
        {"requestId": "req-pull-redeploy", "action": "pull-redeploy", "mode": "graceful", "dir": "/srv/a", "image": "nginx:latest"},
    ]
    for payload in cases:
        response = client.post(
            "/api/agents/agent-a/commands",
            headers={"X-Admin-Token": "test-admin-token"},
            json=payload,
        )
        assert response.status_code == 202, f"action {payload['action']} 应被受理: {response.text}"
        assert response.json()["command"]["action"] == payload["action"]


def test_dispatch_pull_redeploy_without_image_returns_422(client: TestClient) -> None:
    # pull-redeploy 复用 agent 侧 handle_update,需 image;缺失应 422
    import app.main as main_module

    state = main_module.hub_state
    attach_agent(state, "agent-a")

    response = client.post(
        "/api/agents/agent-a/commands",
        headers={"X-Admin-Token": "test-admin-token"},
        json={
            "requestId": "req-pull-no-image",
            "action": "pull-redeploy",
            "dir": "/srv/a",
        },
    )

    assert response.status_code == 422


def test_dispatch_restart_without_mode_persists_null_mode(client: TestClient) -> None:
    # 既有 restart 不带 mode 不回归:受理且 mode 为 None,下发帧不含 mode
    import app.main as main_module

    state = main_module.hub_state
    socket = attach_agent(state, "agent-a")

    response = client.post(
        "/api/agents/agent-a/commands",
        headers={"X-Admin-Token": "test-admin-token"},
        json={
            "requestId": "req-restart-no-mode",
            "action": "restart",
            "dir": "/srv/a",
        },
    )

    assert response.status_code == 202
    assert response.json()["command"]["mode"] is None
    assert "mode" not in socket.messages[0]


def test_dispatch_command_to_offline_agent_returns_409(client: TestClient) -> None:
    import app.main as main_module

    state = main_module.hub_state
    state._register_agent_sync("agent-a", "127.0.0.1:12345")

    response = client.post(
        "/api/agents/agent-a/commands",
        headers={"X-Admin-Token": "test-admin-token"},
        json={
            "requestId": "req-offline-1",
            "action": "restart",
            "dir": "/srv/a",
        },
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Agent is offline"}


def test_get_command_events_returns_created_ack_and_result(client: TestClient) -> None:
    import app.main as main_module

    state = main_module.hub_state
    asyncio.run(
        state.store_command(
            "agent-a",
            {"type": "command", "requestId": "req-events-1", "action": "restart", "dir": "/srv/a"},
            requested_by="platform-api",
            request_source="ops-console",
        )
    )
    asyncio.run(state.mark_ack("req-events-1"))
    asyncio.run(state.mark_result("req-events-1", "success", message="done"))

    response = client.get("/api/commands/req-events-1/events", headers={"X-Admin-Token": "test-admin-token"})

    assert response.status_code == 200
    body = response.json()
    assert [item["eventType"] for item in body] == ["created", "ack", "result"]


def test_get_unknown_command_and_events_return_404(client: TestClient) -> None:
    command_response = client.get("/api/commands/missing", headers={"X-Admin-Token": "test-admin-token"})
    events_response = client.get("/api/commands/missing/events", headers={"X-Admin-Token": "test-admin-token"})

    assert command_response.status_code == 404
    assert events_response.status_code == 404
    assert command_response.json() == {"detail": "Command not found"}
    assert events_response.json() == {"detail": "Command not found"}


def test_list_commands_supports_sort_and_pagination(client: TestClient) -> None:
    import app.main as main_module

    state = main_module.hub_state
    payload1 = {"type": "command", "requestId": "req-1", "action": "restart", "dir": "/srv/a"}
    payload2 = {"type": "command", "requestId": "req-2", "action": "update", "dir": "/srv/a", "image": "nginx:latest"}

    asyncio.run(state.store_command("agent-a", payload1, requested_by="platform-api", request_source="ops-console"))
    asyncio.run(state.mark_result("req-1", "failed", error="boom"))
    asyncio.run(state.store_command("agent-a", payload2, requested_by="scheduler", request_source="batch-job"))
    asyncio.run(state.mark_result("req-2", "success", message="done"))

    response = client.get(
        "/api/commands",
        params={
            "sortBy": "updatedAt",
            "order": "asc",
            "limit": 1,
            "offset": 0,
        },
        headers={"X-Admin-Token": "test-admin-token"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["limit"] == 1
    assert body["hasMore"] is True
    assert body["sortBy"] == "updatedAt"
    assert body["order"] == "asc"
    assert len(body["items"]) == 1


def test_list_commands_tolerates_empty_query_values(client: TestClient) -> None:
    response = client.get(
        "/api/commands",
        params={
            "agentId": "",
            "status": "",
            "action": "",
            "requestedBy": "",
            "requestSource": "",
            "createdAfter": "",
            "createdBefore": "",
            "sortBy": "",
            "order": "",
            "limit": "",
            "offset": "",
        },
        headers={"X-Admin-Token": "test-admin-token"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["sortBy"] == "createdAt"
    assert body["order"] == "desc"
    assert body["limit"] == 100
    assert body["offset"] == 0


def test_list_commands_supports_agent_filter(client: TestClient) -> None:
    import app.main as main_module

    state = main_module.hub_state
    asyncio.run(state.store_command("agent-a", {"type": "command", "requestId": "req-agent-filter-1", "action": "restart", "dir": "/srv/a"}))
    asyncio.run(state.store_command("agent-b", {"type": "command", "requestId": "req-agent-filter-2", "action": "restart", "dir": "/srv/b"}))

    filtered_response = client.get("/api/commands", params={"agentId": "agent-a"}, headers={"X-Admin-Token": "test-admin-token"})

    assert filtered_response.status_code == 200
    filtered_body = filtered_response.json()
    assert filtered_body["total"] == 1
    assert filtered_body["items"][0]["requestId"] == "req-agent-filter-1"


def test_list_commands_tolerates_empty_query_values_after_agent_filter_cleanup(client: TestClient) -> None:
    response = client.get(
        "/api/commands",
        params={
            "agentId": "",
            "status": "",
            "action": "",
            "requestedBy": "",
            "requestSource": "",
            "createdAfter": "",
            "createdBefore": "",
            "sortBy": "",
            "order": "",
            "limit": "",
            "offset": "",
        },
        headers={"X-Admin-Token": "test-admin-token"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["sortBy"] == "createdAt"
    assert body["order"] == "desc"
    assert body["limit"] == 100
    assert body["offset"] == 0


def test_command_history_is_paginated_with_agent_filter(client: TestClient) -> None:
    import app.main as main_module
    state = main_module.hub_state
    asyncio.run(state.store_command("agent-a", {"type": "command", "requestId": "req-1", "action": "restart", "dir": "/srv/a"}, requested_by="platform-api", request_source="ops-console"))
    asyncio.run(state.mark_result("req-1", "failed", error="boom"))
    asyncio.run(state.store_command("agent-b", {"type": "command", "requestId": "req-2", "action": "restart", "dir": "/srv/b"}, requested_by="platform-api", request_source="ops-console"))
    asyncio.run(state.mark_result("req-2", "success", message="done"))

    response = client.get("/api/commands", params={"agentId": "agent-a", "status": "failed"}, headers={"X-Admin-Token": "test-admin-token"})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["requestId"] == "req-1"


def test_retry_failed_command_creates_new_command_and_audit_event(client: TestClient) -> None:
    import app.main as main_module
    state = main_module.hub_state
    fake_socket = attach_agent(state, "agent-a")
    asyncio.run(state.store_command("agent-a", {"type": "command", "requestId": "req-1", "action": "restart", "dir": "/srv/a"}, requested_by="platform-api", request_source="ops-console"))
    asyncio.run(state.mark_result("req-1", "failed", error="boom"))

    response = client.post(
        "/api/commands/req-1/retry",
        headers={
            "X-Admin-Token": "test-admin-token",
            "X-Requested-By": "platform-api",
            "X-Requested-Source": "ops-console",
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["command"]["originalRequestId"] == "req-1"
    assert body["command"]["retryCount"] == 1
    assert fake_socket.messages[0]["requestId"] == body["command"]["requestId"]

    events_response = client.get("/api/commands/req-1/events", headers={"X-Admin-Token": "test-admin-token"})
    assert events_response.status_code == 200
    event_types = [item["eventType"] for item in events_response.json()]
    assert "retry" in event_types


def test_retry_non_failed_command_returns_409(client: TestClient) -> None:
    import app.main as main_module

    state = main_module.hub_state
    attach_agent(state, "agent-a")
    asyncio.run(state.store_command("agent-a", {"type": "command", "requestId": "req-ok", "action": "restart", "dir": "/srv/a"}))
    asyncio.run(state.mark_result("req-ok", "success", message="done"))

    response = client.post(
        "/api/commands/req-ok/retry",
        headers={"X-Admin-Token": "test-admin-token"},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Only failed commands can be retried"}


def test_retry_missing_command_returns_404(client: TestClient) -> None:
    response = client.post(
        "/api/commands/missing/retry",
        headers={"X-Admin-Token": "test-admin-token"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Command not found"}


def test_stream_agent_logs_returns_sse_events(client: TestClient) -> None:
    import app.main as main_module

    state = main_module.hub_state

    async def on_send(payload: dict[str, Any]) -> None:
        assert payload["type"] == "logs_start"
        await state.publish_log_session_event(
            payload["sessionId"],
            "started",
            {"tail": payload.get("tail"), "timestamps": payload.get("timestamps")},
        )
        await state.publish_log_session_event(
            payload["sessionId"],
            "chunk",
            {"chunk": "line-1\n"},
        )
        await state.publish_log_session_event(
            payload["sessionId"],
            "finished",
            {"exitCode": 0, "stopped": False},
        )

    socket = attach_agent(state, "agent-a", on_send=on_send)

    with client.stream(
        "POST",
        "/api/agents/agent-a/logs/stream",
        headers={
            "X-Admin-Token": "test-admin-token",
            "X-Requested-By": "ops-console",
            "X-Requested-Source": "manual-operation",
        },
        json={
            "dir": "/srv/a",
            "tail": 20,
            "timestamps": True,
        },
    ) as response:
        body = "".join(response.iter_text())
        session_id = response.headers["X-Log-Session-Id"]

    assert response.status_code == 200
    # 合并交叉:钉死「SSE 内容类型 + 缓存禁用 + SecurityHeaders 也注入到流式响应」。
    # (logs.py 出 text/event-stream + no-cache;SecurityHeaders 中间件对所有响应——含 SSE——注入安全头。)
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]
    # criticC:requestedBy 由 hub 据 admin token 服务端派生(platform-admin),
    # 不再原样落客户端 X-Requested-By(与 dispatch/retry 审计模型一致)。
    assert socket.messages == [
        {
            "type": "logs_start",
            "sessionId": session_id,
            "dir": "/srv/a",
            "tail": 20,
            "timestamps": True,
            "requestedBy": "platform-admin",
            "requestSource": "manual-operation",
        }
    ]
    assert f'"sessionId": "{session_id}"' in body
    assert "event: started" in body
    assert "event: chunk" in body
    assert "event: finished" in body
    assert '"chunk": "line-1\\n"' in body


def test_stream_agent_logs_derives_requested_by_ignoring_client_header(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # criticC:带 X-Requested-By: attacker 调 logs/stream → 落审计的 requested_by 须是派生身份,
    # 不是 attacker(审计可信度模型不留客户端旁路)。
    import app.main as main_module

    state = main_module.hub_state

    captured: dict[str, Any] = {}

    async def on_send(payload: dict[str, Any]) -> None:
        captured["start_payload"] = payload
        await state.publish_log_session_event(
            payload["sessionId"],
            "finished",
            {"exitCode": 0, "stopped": False},
        )

    attach_agent(state, "agent-a", on_send=on_send)

    with caplog.at_level("INFO", logger="app.main"):
        with client.stream(
            "POST",
            "/api/agents/agent-a/logs/stream",
            headers={
                "X-Admin-Token": "test-admin-token",
                "X-Requested-By": "attacker",
            },
            json={"dir": "/srv/a"},
        ) as response:
            "".join(response.iter_text())

    assert response.status_code == 200
    # 下发给 agent 的 logs_start 帧(即审计来源)requestedBy 是派生身份,而非 attacker。
    assert captured["start_payload"]["requestedBy"] == "platform-admin"
    # 客户端自报值仅作 hint 记入日志,不作权威。
    assert any("attacker" in record.getMessage() for record in caplog.records)


def test_stream_agent_logs_returns_stream_error_event(client: TestClient) -> None:
    import app.main as main_module

    state = main_module.hub_state

    async def on_send(payload: dict[str, Any]) -> None:
        await state.publish_log_session_event(
            payload["sessionId"],
            "error",
            {"error": "No docker-compose.yaml/yml found in /srv/a"},
        )

    attach_agent(state, "agent-a", on_send=on_send)

    with client.stream(
        "POST",
        "/api/agents/agent-a/logs/stream",
        headers={"X-Admin-Token": "test-admin-token"},
        json={"dir": "/srv/a"},
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert "event: error" in body
    assert '"error": "No docker-compose.yaml/yml found in /srv/a"' in body


def test_rolling_router_mounted(client: TestClient) -> None:
    # 未带 token 返回 403 而非 404,证明路由已注册
    resp = client.post("/api/rolling-restart", json={"agentId": "a", "serviceName": "s"})
    assert resp.status_code == 403


def test_rolling_restart_returns_task_id(client: TestClient) -> None:
    resp = client.post(
        "/api/rolling-restart",
        json={"agentId": "a", "serviceName": "s"},
        headers={"X-Admin-Token": "test-admin-token"},
    )
    assert resp.status_code == 200
    assert "taskId" in resp.json()
    # 后台编排会因无在线 agent 很快 finish,此处只验端点契约


def test_rolling_status_404_unknown(client: TestClient) -> None:
    resp = client.get("/api/rolling-restart/nope", headers={"X-Admin-Token": "test-admin-token"})
    assert resp.status_code == 404


def test_rolling_restart_conflict_returns_409(client: TestClient) -> None:
    # L3: 先占锁(running),再对同 (agent,service) 发滚动 → 409
    # (冲突在 create_rolling_task 抛出,早于后台任务,天然规避 flaky)
    import app.main as main_module

    state = main_module.hub_state
    asyncio.run(state.create_rolling_task("task-occupied", "agent-a", "svc", False))

    resp = client.post(
        "/api/rolling-restart",
        json={"agentId": "agent-a", "serviceName": "svc"},
        headers={"X-Admin-Token": "test-admin-token"},
    )
    assert resp.status_code == 409


def test_rolling_acknowledge_requires_admin_token(client: TestClient) -> None:
    # 不带 token → 403(鉴权在端点首行)
    resp = client.post("/api/rolling-restart/whatever/acknowledge")
    assert resp.status_code == 403
    assert resp.json() == {"detail": "Invalid admin token"}


def test_rolling_acknowledge_unknown_task_returns_404(client: TestClient) -> None:
    resp = client.post(
        "/api/rolling-restart/nope/acknowledge",
        headers={"X-Admin-Token": "test-admin-token"},
    )
    assert resp.status_code == 404


def test_rolling_acknowledge_releases_lock_end_to_end(client: TestClient) -> None:
    # 端到端:占锁 → 中断标 interrupted(锁仍在,409)→ acknowledge 释放 → 可再占锁
    import app.main as main_module

    state = main_module.hub_state
    asyncio.run(state.create_rolling_task("task-ack", "agent-a", "svc-ack", False))
    asyncio.run(state.interrupt_running_rolling())

    # 锁仍在:同 key 再发滚动 → 409
    conflict = client.post(
        "/api/rolling-restart",
        json={"agentId": "agent-a", "serviceName": "svc-ack"},
        headers={"X-Admin-Token": "test-admin-token"},
    )
    assert conflict.status_code == 409

    # 非 interrupted 任务(此处用同一个 interrupted 任务的相反场景由 store 测试覆盖)
    ack = client.post(
        "/api/rolling-restart/task-ack/acknowledge",
        headers={"X-Admin-Token": "test-admin-token"},
    )
    assert ack.status_code == 200
    assert ack.json() == {"acknowledged": True}

    # 释放后可再占锁
    again = client.post(
        "/api/rolling-restart",
        json={"agentId": "agent-a", "serviceName": "svc-ack"},
        headers={"X-Admin-Token": "test-admin-token"},
    )
    assert again.status_code == 200


def test_rolling_acknowledge_non_interrupted_returns_409(client: TestClient) -> None:
    # 任务存在但非 interrupted(running)→ 409
    import app.main as main_module

    state = main_module.hub_state
    asyncio.run(state.create_rolling_task("task-running", "agent-b", "svc-b", False))

    resp = client.post(
        "/api/rolling-restart/task-running/acknowledge",
        headers={"X-Admin-Token": "test-admin-token"},
    )
    assert resp.status_code == 409


def test_log_stream_subscriptions_share_upstream_and_stop_on_last_subscriber(client: TestClient) -> None:
    import app.main as main_module

    state = main_module.hub_state

    session_id, subscriber_one, queue_one, start_payload = asyncio.run(
        state.subscribe_log_stream(
            agent_id="agent-a",
            project_dir="/srv/a",
            tail=20,
            timestamps=True,
            requested_by="ops-console",
            request_source="manual-operation",
        )
    )
    shared_session_id, subscriber_two, queue_two, shared_start_payload = asyncio.run(
        state.subscribe_log_stream(
            agent_id="agent-a",
            project_dir="/srv/a",
            tail=5,
            timestamps=True,
        )
    )

    assert shared_session_id == session_id
    assert start_payload == {
        "type": "logs_start",
        "sessionId": session_id,
        "dir": "/srv/a",
        "tail": 20,
        "timestamps": True,
        "requestedBy": "ops-console",
        "requestSource": "manual-operation",
    }
    assert shared_start_payload is None

    asyncio.run(
        state.publish_log_session_event(
            session_id,
            "started",
            {"tail": 20, "timestamps": True},
        )
    )
    asyncio.run(state.publish_log_session_event(session_id, "chunk", {"chunk": "line-1\n"}))
    asyncio.run(state.publish_log_session_event(session_id, "chunk", {"chunk": "line-2\n"}))

    replay_session_id, subscriber_three, replay_queue, replay_start_payload = asyncio.run(
        state.subscribe_log_stream(
            agent_id="agent-a",
            project_dir="/srv/a",
            tail=1,
            timestamps=True,
        )
    )

    assert replay_session_id == session_id
    assert replay_start_payload is None
    assert asyncio.run(queue_one.get()) == {
        "event": "started",
        "tail": 20,
        "timestamps": True,
    }
    assert asyncio.run(queue_one.get()) == {"event": "chunk", "chunk": "line-1\n"}
    assert asyncio.run(queue_one.get()) == {"event": "chunk", "chunk": "line-2\n"}
    assert asyncio.run(queue_two.get()) == {
        "event": "started",
        "tail": 20,
        "timestamps": True,
    }
    assert asyncio.run(queue_two.get()) == {"event": "chunk", "chunk": "line-1\n"}
    assert asyncio.run(queue_two.get()) == {"event": "chunk", "chunk": "line-2\n"}
    assert asyncio.run(replay_queue.get()) == {
        "event": "started",
        "tail": 20,
        "timestamps": True,
    }
    assert asyncio.run(replay_queue.get()) == {"event": "chunk", "chunk": "line-2\n"}

    assert asyncio.run(state.unsubscribe_log_stream(subscriber_one)) is None
    assert asyncio.run(state.unsubscribe_log_stream(subscriber_two)) is None
    assert asyncio.run(state.unsubscribe_log_stream(subscriber_three)) == {
        "agent_id": "agent-a",
        "session_id": session_id,
    }


# === T9a: list-instances REST 端点(供平台节点页查健康实例,token-gated) ===


def _stub_call_agent(state: HubState, monkeypatch: pytest.MonkeyPatch, *, result: dict[str, Any] | None = None, raises: BaseException | None = None) -> dict[str, Any]:
    """把 hub_state.call_agent 替换成 stub。

    - result:返回该 dict;raises:抛出该异常(二选一)。
    - 返回 recorder,recorder["messages"] 记录每次下发给 agent 的 message,用于断言转发内容。
    """
    recorder: dict[str, Any] = {"calls": 0, "messages": []}

    async def fake_call_agent(agent_id: str, message: dict, timeout: float) -> dict:
        recorder["calls"] += 1
        recorder["messages"].append(message)
        if raises is not None:
            raise raises
        return result if result is not None else {"status": "success", "instances": []}

    monkeypatch.setattr(state, "call_agent", fake_call_agent)
    return recorder


def test_list_instances_requires_admin_token(client: TestClient) -> None:
    # 不带 X-Admin-Token → 403(证明该端点 token-gated,不可匿名)。
    response = client.post(
        "/api/agents/agent-a/list-instances",
        json={"serviceName": "memory-share"},
    )

    assert response.status_code == 403


def test_list_instances_returns_instances_for_online_agent(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # agent 在线 + call_agent 返回 success 与健康实例 → 200,且原样回传 instances。
    import app.main as main_module

    state = main_module.hub_state
    attach_agent(state, "agent-a")
    instances = [
        {"address": "h:1800", "containerId": "c0", "healthy": True, "matched": True, "composeProject": "memory-share-1"},
        {"address": "h:1801", "containerId": "c1", "healthy": False, "matched": True, "composeProject": "memory-share-1"},
    ]
    recorder = _stub_call_agent(state, monkeypatch, result={"status": "success", "instances": instances})

    response = client.post(
        "/api/agents/agent-a/list-instances",
        headers={"X-Admin-Token": "test-admin-token"},
        json={"serviceName": "memory-share"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "success"
    assert body["instances"] == instances
    # 转发给 agent 的 message:type/serviceName 正确,且带 requestId。
    assert recorder["calls"] == 1
    sent = recorder["messages"][0]
    assert sent["type"] == "list-instances"
    assert sent["serviceName"] == "memory-share"
    assert sent["requestId"]
    # 未传 expectedComposeProject 时不应注入该键。
    assert "expectedComposeProject" not in sent


def test_list_instances_forwards_expected_compose_project(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # 传了 expectedComposeProject → 透传给 agent 的 message。
    import app.main as main_module

    state = main_module.hub_state
    attach_agent(state, "agent-a")
    recorder = _stub_call_agent(state, monkeypatch, result={"status": "success", "instances": []})

    response = client.post(
        "/api/agents/agent-a/list-instances",
        headers={"X-Admin-Token": "test-admin-token"},
        json={"serviceName": "memory-share", "expectedComposeProject": "memory-share-1"},
    )

    assert response.status_code == 200, response.text
    sent = recorder["messages"][0]
    assert sent["expectedComposeProject"] == "memory-share-1"


def test_list_instances_to_offline_agent_returns_409(client: TestClient) -> None:
    # agent 已登记但离线(无连接)→ 409。
    import app.main as main_module

    state = main_module.hub_state
    state._register_agent_sync("agent-a", "127.0.0.1:12345")

    response = client.post(
        "/api/agents/agent-a/list-instances",
        headers={"X-Admin-Token": "test-admin-token"},
        json={"serviceName": "memory-share"},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Agent is offline"}


def test_list_instances_to_unknown_agent_returns_404(client: TestClient) -> None:
    # agent 不存在 → 404。
    response = client.post(
        "/api/agents/ghost/list-instances",
        headers={"X-Admin-Token": "test-admin-token"},
        json={"serviceName": "memory-share"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Agent not found"}


def test_list_instances_agent_failure_returns_502(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # call_agent 返回 status != success → 502,且回传脱敏后的 error。
    import app.main as main_module

    state = main_module.hub_state
    attach_agent(state, "agent-a")
    _stub_call_agent(state, monkeypatch, result={"status": "failed", "error": "boom"})

    response = client.post(
        "/api/agents/agent-a/list-instances",
        headers={"X-Admin-Token": "test-admin-token"},
        json={"serviceName": "memory-share"},
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "boom"


def test_list_instances_agent_timeout_returns_502(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # call_agent 抛 TimeoutError(agent 不应答)→ 502。
    import app.main as main_module

    state = main_module.hub_state
    attach_agent(state, "agent-a")
    _stub_call_agent(state, monkeypatch, raises=asyncio.TimeoutError())

    response = client.post(
        "/api/agents/agent-a/list-instances",
        headers={"X-Admin-Token": "test-admin-token"},
        json={"serviceName": "memory-share"},
    )

    assert response.status_code == 502


def test_list_instances_agent_connection_lost_returns_502(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # check→call 竞态:online 判定通过后 call_agent 抛 RuntimeError(连接不可用)→ 502 脱敏,
    # 不得逃逸成未脱敏 500。
    import app.main as main_module

    state = main_module.hub_state
    attach_agent(state, "agent-a")
    _stub_call_agent(state, monkeypatch, raises=RuntimeError("connection unavailable"))

    response = client.post(
        "/api/agents/agent-a/list-instances",
        headers={"X-Admin-Token": "test-admin-token"},
        json={"serviceName": "memory-share"},
    )

    assert response.status_code == 502
    # 脱敏:不向调用方暴露内部异常原文。
    assert "connection unavailable" not in response.json().get("detail", "")
