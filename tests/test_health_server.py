import importlib
import io
import json
import sys
from types import SimpleNamespace

import pytest


def _import_health_server(monkeypatch: pytest.MonkeyPatch):
    for key, value in {
        "WS_URL": "ws://localhost:8080/ws/agent",
        "AGENT_KEY": "token",
        "AGENT_ID": "agent-health",
        "HEALTH_HOST": "127.0.0.1",
        "HEALTH_PORT": "18081",
    }.items():
        monkeypatch.setenv(key, value)
    for name in ["config", "core.ws_client", "core.health_server"]:
        sys.modules.pop(name, None)
    return importlib.import_module("core.health_server")


def _make_handler(module, path: str, state: dict):
    module.get_connection_state = lambda: state
    handler = module._HealthHandler.__new__(module._HealthHandler)
    handler.path = path
    handler.wfile = io.BytesIO()
    responses: list[int] = []
    headers: list[tuple[str, str]] = []
    handler.send_response = lambda code: responses.append(code)
    handler.send_header = lambda key, value: headers.append((key, value))
    handler.end_headers = lambda: None
    return handler, responses, headers


def test_health_handler_returns_404_for_unknown_path(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _import_health_server(monkeypatch)
    handler, responses, headers = _make_handler(module, "/nope", {})

    handler.do_GET()

    assert responses == [404]
    assert headers == []


def test_health_handler_returns_degraded_and_ok_payloads(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _import_health_server(monkeypatch)

    degraded, responses, headers = _make_handler(module, "/health", {"connected": False, "last_error": "boom"})
    degraded.do_GET()
    degraded_payload = json.loads(degraded.wfile.getvalue().decode("utf-8"))

    assert responses == [503]
    assert ("Content-Type", "application/json") in headers
    assert degraded_payload["status"] == "degraded"
    assert degraded_payload["agentId"] == "agent-health"
    assert degraded_payload["lastError"] == "boom"

    healthy_state = {
        "connected": True,
        "last_connect_ts": 1,
        "last_disconnect_ts": 2,
        "last_heartbeat_ts": 3,
        "last_message_ts": 4,
        "last_error": None,
    }
    healthy, responses, headers = _make_handler(module, "/health", healthy_state)
    healthy.do_GET()
    healthy_payload = json.loads(healthy.wfile.getvalue().decode("utf-8"))

    assert responses == [200]
    assert healthy_payload["status"] == "ok"
    assert healthy_payload["connected"] is True
    assert healthy_payload["lastConnectTs"] == "1970-01-01T08:00:01+08:00"
    assert healthy_payload["lastDisconnectTs"] == "1970-01-01T08:00:02+08:00"
    assert healthy_payload["lastHeartbeatTs"] == "1970-01-01T08:00:03+08:00"
    assert healthy_payload["lastMessageTs"] == "1970-01-01T08:00:04+08:00"
    assert module._HealthHandler.log_message(healthy, "%s") is None


def test_start_health_server_creates_background_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _import_health_server(monkeypatch)
    server_calls: list[tuple[tuple[str, int], object]] = []
    thread_calls: list[tuple[object, bool]] = []

    class FakeServer:
        def __init__(self, address, handler):
            server_calls.append((address, handler))

        def serve_forever(self) -> None:
            return None

    class FakeThread:
        def __init__(self, target, daemon):
            self.target = target
            thread_calls.append((target, daemon))

        def start(self) -> None:
            self.target()

    monkeypatch.setattr(module, "ThreadingHTTPServer", FakeServer)
    monkeypatch.setattr(module.threading, "Thread", FakeThread)

    server = module.start_health_server()

    assert isinstance(server, FakeServer)
    assert server_calls == [(("127.0.0.1", 18081), module._HealthHandler)]
    assert thread_calls[0][1] is True
