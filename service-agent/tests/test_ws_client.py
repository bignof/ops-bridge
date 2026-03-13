import asyncio
import importlib
import json
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from websockets.asyncio.server import serve


def _import_ws_client(monkeypatch: pytest.MonkeyPatch, **env_overrides: str):
    defaults = {
        "WS_URL": "ws://hub.example/ws/agent",
        "AGENT_KEY": "secret-key",
        "AGENT_ID": "agent-7",
        "HEARTBEAT_INTERVAL": "1",
    }
    for key, value in defaults.items():
        monkeypatch.setenv(key, value)
    for key, value in env_overrides.items():
        monkeypatch.setenv(key, value)
    for name in ["config", "core.ws_client"]:
        sys.modules.pop(name, None)
    return importlib.import_module("core.ws_client")


def test_connection_state_and_open_close_error(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _import_ws_client(monkeypatch)
    heartbeat_calls: list[object] = []

    monkeypatch.setattr(module, "_start_heartbeat", lambda ws: heartbeat_calls.append(ws))
    monkeypatch.setattr(module.time, "time", lambda: 123.0)

    ws = SimpleNamespace(keep_running=True)
    module._on_open(ws)
    module._on_error(ws, RuntimeError("boom"))
    module._on_close(ws, 1000, "bye")

    state = module.get_connection_state()
    assert state["connected"] is False
    assert state["last_connect_ts"] == 123.0
    assert state["last_disconnect_ts"] == 123.0
    assert state["last_error"] == "boom"
    assert heartbeat_calls == [ws]


def test_on_message_dispatches_commands_and_ping(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _import_ws_client(monkeypatch)
    dispatch_calls: list[tuple[object, dict]] = []
    sent_messages: list[dict] = []

    class ImmediateThread:
        def __init__(self, target, args, daemon):
            self.target = target
            self.args = args

        def start(self) -> None:
            self.target(*self.args)

    monkeypatch.setattr(module.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(module, "dispatch", lambda ws, data: dispatch_calls.append((ws, data)))
    monkeypatch.setattr(module, "send_message", lambda ws, payload: sent_messages.append(payload))
    monkeypatch.setattr(module.time, "time", lambda: 456.0)

    ws = SimpleNamespace()
    module._on_message(ws, '{"type": "command", "requestId": "req-1"}')
    module._on_message(ws, '{"type": "ping"}')
    module._on_message(ws, 'not-json')

    state = module.get_connection_state()
    assert dispatch_calls == [(ws, {"type": "command", "requestId": "req-1"})]
    assert sent_messages == [{"type": "pong", "timestamp": 456.0}]
    assert state["last_message_ts"] == 456.0


def test_start_heartbeat_sends_periodic_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _import_ws_client(monkeypatch)
    sent_messages: list[dict] = []
    timestamps = iter([10.0, 11.0])

    class ImmediateThread:
        def __init__(self, target, daemon):
            self.target = target

        def start(self) -> None:
            self.target()

    ws = SimpleNamespace(keep_running=True)

    def fake_send_message(target_ws, payload):
        sent_messages.append(payload)
        target_ws.keep_running = False

    monkeypatch.setattr(module.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(module, "send_message", fake_send_message)
    monkeypatch.setattr(module.time, "time", lambda: next(timestamps))
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)

    module._start_heartbeat(ws)

    assert sent_messages == [{"type": "heartbeat", "ts": 11.0}]
    assert module.get_connection_state()["last_heartbeat_ts"] == 10.0


def test_connect_builds_websocket_app_and_runs_forever(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _import_ws_client(monkeypatch)
    created: dict = {}

    class FakeWebSocketApp:
        def __init__(self, url, on_open, on_message, on_error, on_close):
            created.update(
                {
                    "url": url,
                    "on_open": on_open,
                    "on_message": on_message,
                    "on_error": on_error,
                    "on_close": on_close,
                }
            )

        def run_forever(self, ping_interval, ping_timeout):
            created["ping_interval"] = ping_interval
            created["ping_timeout"] = ping_timeout

    monkeypatch.setattr(module.websocket, "WebSocketApp", FakeWebSocketApp)

    module.connect()

    assert created["url"] == "ws://hub.example/ws/agent/agent-7?key=secret-key"
    assert created["ping_interval"] == 20
    assert created["ping_timeout"] == 10


def test_connect_handles_real_websocket_round_trip(monkeypatch: pytest.MonkeyPatch, free_tcp_port: int) -> None:
    module = _import_ws_client(
        monkeypatch,
        WS_URL=f"ws://127.0.0.1:{free_tcp_port}/ws/agent",
        HEARTBEAT_INTERVAL="3600",
    )
    observed: dict[str, object] = {}
    server_connected = threading.Event()
    server_done = threading.Event()
    command_received = threading.Event()

    def fake_dispatch(ws, data):
        observed["command"] = data
        command_received.set()
        ws.close()

    monkeypatch.setattr(module, "dispatch", fake_dispatch)

    def run_client() -> None:
        module.connect()

    client_thread = threading.Thread(target=run_client)

    async def handler(websocket) -> None:
        server_connected.set()
        await websocket.send(json.dumps({"type": "ping"}))
        observed["pong"] = json.loads(await websocket.recv())
        await websocket.send(json.dumps({"type": "command", "requestId": "req-real"}))
        assert command_received.wait(5)
        server_done.set()

    async def run_server() -> None:
        async with serve(handler, "127.0.0.1", free_tcp_port):
            while not server_done.is_set():
                await asyncio.sleep(0.05)

    server_thread = threading.Thread(target=lambda: asyncio.run(run_server()))

    server_thread.start()
    time.sleep(0.1)
    client_thread.start()
    client_thread.join(timeout=5)
    server_done.wait(5)
    server_thread.join(timeout=5)

    for _ in range(20):
        if not client_thread.is_alive():
            break
        time.sleep(0.05)

    assert server_connected.is_set() is True
    assert client_thread.is_alive() is False
    assert observed["pong"] == {"type": "pong", "timestamp": observed["pong"]["timestamp"]}
    assert observed["command"] == {"type": "command", "requestId": "req-real"}
