"""只读 + 日志端点的 admin token 鉴权回归测试（纵深防御）。

覆盖此前匿名可读的六个端点：
- GET  /api/agents
- GET  /api/agents/{agent_id}
- GET  /api/commands
- GET  /api/commands/{request_id}
- GET  /api/commands/{request_id}/events
- POST /api/agents/{agent_id}/logs/stream

约定（与 tests/test_api.py 一致）：
- 不带 X-Admin-Token → 403 "Invalid admin token"（鉴权先于业务逻辑，证明端点已被 gated）。
- 带正确 token → 过鉴权；无数据时返回 404、agent 离线时 409 均可（≠403 即证明已过鉴权）。

client fixture（见 conftest 同目录的 test_api.py）已把 admin_token 配为 "test-admin-token"。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

# 复用 test_api 里已配置好 admin_token 的 client fixture（import 即让本模块可见该 fixture）。
from tests.test_api import client  # noqa: F401


ADMIN_HEADERS = {"X-Admin-Token": "test-admin-token"}


# --------------------------------------------------------------------------- #
# 不带 token → 全部 403（核心安全断言）
# --------------------------------------------------------------------------- #


def test_list_agents_requires_admin_token(client: TestClient) -> None:
    response = client.get("/api/agents")

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid admin token"


def test_get_agent_requires_admin_token(client: TestClient) -> None:
    response = client.get("/api/agents/agent-a")

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid admin token"


def test_list_commands_requires_admin_token(client: TestClient) -> None:
    response = client.get("/api/commands")

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid admin token"


def test_get_command_requires_admin_token(client: TestClient) -> None:
    response = client.get("/api/commands/req-x")

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid admin token"


def test_get_command_events_requires_admin_token(client: TestClient) -> None:
    response = client.get("/api/commands/req-x/events")

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid admin token"


def test_stream_agent_logs_requires_admin_token(client: TestClient) -> None:
    response = client.post(
        "/api/agents/agent-a/logs/stream",
        json={"dir": "/srv/a"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid admin token"


# --------------------------------------------------------------------------- #
# 带正确 token → 过鉴权（结果可能 200/404/409，但不再是 403）
# --------------------------------------------------------------------------- #


def test_list_agents_passes_with_admin_token(client: TestClient) -> None:
    response = client.get("/api/agents", headers=ADMIN_HEADERS)

    assert response.status_code == 200
    assert response.json() == []


def test_get_agent_passes_with_admin_token(client: TestClient) -> None:
    # 无此 agent → 过鉴权后 404（≠403 即证明已过鉴权）。
    response = client.get("/api/agents/missing-agent", headers=ADMIN_HEADERS)

    assert response.status_code == 404
    assert response.json() == {"detail": "Agent not found"}


def test_list_commands_passes_with_admin_token(client: TestClient) -> None:
    response = client.get("/api/commands", headers=ADMIN_HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["total"] == 0


def test_get_command_passes_with_admin_token(client: TestClient) -> None:
    response = client.get("/api/commands/missing", headers=ADMIN_HEADERS)

    assert response.status_code == 404
    assert response.json() == {"detail": "Command not found"}


def test_get_command_events_passes_with_admin_token(client: TestClient) -> None:
    response = client.get("/api/commands/missing/events", headers=ADMIN_HEADERS)

    assert response.status_code == 404
    assert response.json() == {"detail": "Command not found"}


def test_stream_agent_logs_passes_with_admin_token(client: TestClient) -> None:
    # 带 token 但 agent 未注册 → 过鉴权后 404（≠403 即证明已过鉴权；
    # 鉴权先于 get_agent 业务校验执行）。
    response = client.post(
        "/api/agents/agent-a/logs/stream",
        headers=ADMIN_HEADERS,
        json={"dir": "/srv/a"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Agent not found"}
