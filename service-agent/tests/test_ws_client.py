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


def test_on_open_starts_status_reporting(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _import_ws_client(monkeypatch)
    monkeypatch.setattr(module, "_start_heartbeat", lambda ws: None)
    calls: list[object] = []
    monkeypatch.setattr(module, "start_status_reporting", lambda ws: calls.append(ws))

    ws = SimpleNamespace(keep_running=True)
    module._on_open(ws)

    assert calls == [ws]


def test_on_message_dispatches_commands_and_ping(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _import_ws_client(monkeypatch)
    dispatch_calls: list[tuple[object, dict]] = []
    logs_start_calls: list[tuple[object, dict]] = []
    logs_stop_calls: list[dict] = []
    sent_messages: list[dict] = []

    class ImmediateThread:
        def __init__(self, target, args, daemon):
            self.target = target
            self.args = args

        def start(self) -> None:
            self.target(*self.args)

    monkeypatch.setattr(module.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(module, "dispatch", lambda ws, data: dispatch_calls.append((ws, data)))
    monkeypatch.setattr(module, "start_log_session", lambda ws, data: logs_start_calls.append((ws, data)))
    monkeypatch.setattr(module, "stop_log_session", lambda data: logs_stop_calls.append(data))
    monkeypatch.setattr(module, "send_message", lambda ws, payload: sent_messages.append(payload))
    monkeypatch.setattr(module.time, "time", lambda: 456.0)

    ws = SimpleNamespace()
    module._on_message(ws, '{"type": "command", "requestId": "req-1"}')
    module._on_message(ws, '{"type": "logs_start", "sessionId": "logs-1"}')
    module._on_message(ws, '{"type": "logs_stop", "sessionId": "logs-1"}')
    module._on_message(ws, '{"type": "ping"}')
    module._on_message(ws, 'not-json')

    state = module.get_connection_state()
    assert dispatch_calls == [(ws, {"type": "command", "requestId": "req-1"})]
    assert logs_start_calls == [(ws, {"type": "logs_start", "sessionId": "logs-1"})]
    assert logs_stop_calls == [{"type": "logs_stop", "sessionId": "logs-1"}]
    assert sent_messages == [{"type": "pong", "timestamp": 456.0}]
    assert state["last_message_ts"] == 456.0


def test_on_message_dispatches_watch_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _import_ws_client(monkeypatch)
    received: list[object] = []
    monkeypatch.setattr(module, "set_watch_targets", lambda targets: received.append(targets))

    ws = SimpleNamespace()
    module._on_message(ws, '{"type": "watch_targets", "targets": [{"deploymentId": 1, "dir": "/data/a"}]}')

    assert received == [[{"deploymentId": 1, "dir": "/data/a"}]]


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
    monkeypatch.setattr(module.outbox, "flush", lambda **kw: None)  # 心跳补投由专门用例覆盖
    monkeypatch.setattr(module.time, "time", lambda: next(timestamps))
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)

    module._start_heartbeat(ws)

    assert sent_messages == [{"type": "heartbeat", "ts": 11.0}]
    assert module.get_connection_state()["last_heartbeat_ts"] == 10.0


def test_result_ack_clears_outbox_and_open_close_wire_sender(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _import_ws_client(monkeypatch)
    from core import outbox

    acked: list[str] = []
    senders: list[object] = []
    flush_calls: list[dict] = []

    monkeypatch.setattr(module.outbox, "ack", lambda rid: acked.append(rid))
    monkeypatch.setattr(module.outbox, "set_sender", lambda fn: senders.append(fn))
    monkeypatch.setattr(module.outbox, "clear_sender", lambda: senders.append(None))
    monkeypatch.setattr(module.outbox, "flush", lambda **kw: flush_calls.append(kw))
    monkeypatch.setattr(module, "_start_heartbeat", lambda ws: None)

    ws = SimpleNamespace(keep_running=True)
    module._on_open(ws)
    assert len(senders) == 1 and senders[0] is not None  # 重连即换当前活跃发送通道
    assert flush_calls and flush_calls[0].get("force") is True  # 重连立即全量补投

    module._on_message(ws, '{"type": "result_ack", "requestId": "req-9"}')
    assert acked == ["req-9"]

    module._on_close(ws, 1000, "bye")
    assert senders[-1] is None  # 断线清 sender,补投等重连


def test_heartbeat_flushes_outbox(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _import_ws_client(monkeypatch)
    flush_calls: list[dict] = []

    class ImmediateThread:
        def __init__(self, target, daemon):
            self.target = target

        def start(self) -> None:
            self.target()

    ws = SimpleNamespace(keep_running=True)

    def fake_send_message(target_ws, payload):
        target_ws.keep_running = False

    monkeypatch.setattr(module.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(module, "send_message", fake_send_message)
    monkeypatch.setattr(module.outbox, "flush", lambda **kw: flush_calls.append(kw))
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    cleanup_calls: list[bool] = []
    monkeypatch.setattr(module.agent_upgrade, "cleanup_executor", lambda: cleanup_calls.append(True))

    module._start_heartbeat(ws)

    assert len(flush_calls) == 1  # 每个心跳周期补投一次(按退避)
    assert cleanup_calls == [True]  # 升级结束后由心跳删除已退出的执行器容器


def test_connect_builds_websocket_app_and_runs_forever(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _import_ws_client(monkeypatch)
    created: dict = {}

    class FakeWebSocketApp:
        def __init__(self, url, header, on_open, on_message, on_error, on_close):
            created.update(
                {
                    "url": url,
                    "header": header,
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

    # 默认 auto：key 走请求头，URL 里不出现（网关/nginx 访问日志不会记下 key）
    assert created["url"] == "ws://hub.example/ws/agent/agent-7"
    assert created["header"] == ["X-Agent-Key: secret-key"]
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
        observed["path"] = websocket.request.path
        observed["key_header"] = websocket.request.headers.get("X-Agent-Key")
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
    assert observed["path"] == "/ws/agent/agent-7" and observed["key_header"] == "secret-key"


def test_on_close_stops_log_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _import_ws_client(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(module, "stop_all_log_sessions", lambda: calls.append("logs") or 0)

    module._on_close(SimpleNamespace(keep_running=False), 1006, "gone")

    assert calls == ["logs"]


@pytest.mark.parametrize(
    "msg_type,target,threaded",
    [
        ("logfile_list", "handle_logfile_list", True),
        ("logfile_fetch", "start_logfile_fetch", False),
        ("logfile_follow", "start_logfile_follow", False),
        ("logfile_unfollow", "stop_logfile_follow", False),
        ("compose_discover", "handle_compose_discover", True),
        ("compose_inspect", "handle_compose_inspect", True),
    ],
)
def test_on_message_routes_logfile_and_compose_frames(monkeypatch: pytest.MonkeyPatch, msg_type, target, threaded) -> None:
    module = _import_ws_client(monkeypatch)
    calls: list[tuple] = []
    started: list[object] = []

    class FakeThread:
        def __init__(self, target=None, args=(), daemon=None, name=None) -> None:
            self._target, self._args = target, args

        def start(self) -> None:
            started.append(self)
            self._target(*self._args)

    monkeypatch.setattr(module.threading, "Thread", FakeThread)
    if msg_type == "logfile_unfollow":
        monkeypatch.setattr(module, target, lambda data: calls.append(("nows", data)))
    else:
        monkeypatch.setattr(module, target, lambda ws, data: calls.append((ws, data)))
    ws = SimpleNamespace(keep_running=True)
    payload = {"type": msg_type, "requestId": "r", "sessionId": "s"}

    module._on_message(ws, json.dumps(payload))

    assert len(calls) == 1 and calls[0][1] == payload
    assert (len(started) == 1) is threaded


def test_on_close_stops_follow_and_aborts_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _import_ws_client(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(module, "stop_all_log_sessions", lambda: calls.append("logs") or 0)
    monkeypatch.setattr(module, "stop_all_follow", lambda: calls.append("follow") or 0)
    monkeypatch.setattr(module, "abort_all_fetch", lambda: calls.append("fetch"))

    module._on_close(SimpleNamespace(keep_running=False), 1006, "gone")

    assert calls == ["logs", "follow", "fetch"]


def test_ops_code_follows_connection_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _import_ws_client(monkeypatch)
    from core import ops_code

    monkeypatch.setattr(module, "_start_heartbeat", lambda ws: None)
    monkeypatch.setattr(module, "start_status_reporting", lambda ws: None)
    monkeypatch.setattr(module.agent_upgrade, "on_connected", lambda ws: None)
    sent: list[dict] = []
    ws = SimpleNamespace(keep_running=True, send=lambda raw: sent.append(json.loads(raw)))
    fields = {"service": "app", "code": "112233", "operation": "backups:restore",
              "operator": {"userId": 1, "name": "", "ip": ""}}

    module._on_open(ws)
    assert ops_code.verify(dict(fields)) == (503, {"reason": "negotiating"})

    module._on_message(ws, json.dumps({"type": "hub_hello", "features": ["header_auth", "ops_code"]}))
    box: dict = {}
    worker = threading.Thread(target=lambda: box.update(result=ops_code.verify(dict(fields), timeout=5)))
    worker.start()
    deadline = time.monotonic() + 2
    while not any(m.get("type") == "ops_code_verify" for m in sent) and time.monotonic() < deadline:
        time.sleep(0.01)
    frame = next(m for m in sent if m.get("type") == "ops_code_verify")
    module._on_message(ws, json.dumps({"type": "ops_code_verify_result", "requestId": frame["requestId"],
                                       "valid": False, "reason": "invalid"}))
    worker.join(2)
    assert box["result"] == (200, {"valid": False, "reason": "invalid"})

    module._on_close(ws, 1006, "gone")
    assert ops_code.verify(dict(fields)) == (503, {"reason": "unreachable"})

    module._on_open(ws)                                   # 重连：特性回到未知
    assert ops_code.verify(dict(fields)) == (503, {"reason": "negotiating"})


def test_hub_hello_passes_features_to_hub_rpc(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _import_ws_client(monkeypatch)
    from core import hub_rpc

    seen: list[object] = []
    monkeypatch.setattr(hub_rpc, "set_features", lambda features: seen.append(features))

    module._on_message(SimpleNamespace(), json.dumps({"type": "hub_hello", "features": ["header_auth", "env_config"]}))
    module._on_message(SimpleNamespace(), json.dumps({"type": "hub_hello"}))      # 旧中枢的 hub_hello 没有 features

    assert seen == [["header_auth", "env_config"], None]


@pytest.mark.parametrize("msg_type", ["env_config_result", "instance_snapshot_result"])
def test_on_message_routes_hub_rpc_result_frames(monkeypatch: pytest.MonkeyPatch, msg_type) -> None:
    module = _import_ws_client(monkeypatch)
    from core import hub_rpc

    seen: list[dict] = []
    monkeypatch.setattr(hub_rpc, "resolve", lambda data: seen.append(data))
    payload = {"type": msg_type, "requestId": "r1", "env": {"allowUi": True, "allowDs": True}}

    module._on_message(SimpleNamespace(), json.dumps(payload))

    assert seen == [payload]


@pytest.mark.parametrize(
    "msg_type",
    ["ops_code_verify_result", "plugin_query_result", "env_config_query", "instance_snapshot_query", "something_else"],
)
def test_on_message_does_not_route_other_frames_to_hub_rpc(monkeypatch: pytest.MonkeyPatch, msg_type) -> None:
    module = _import_ws_client(monkeypatch)
    from core import hub_rpc

    seen: list[dict] = []
    monkeypatch.setattr(hub_rpc, "resolve", lambda data: seen.append(data))

    module._on_message(SimpleNamespace(), json.dumps({"type": msg_type, "requestId": "r1"}))

    assert seen == []


def _finish(worker, timeout=2):
    """等 (线程, 装结果的 box) 结束并取结果；线程没结束就明确报错，而不是读 box 时抛 KeyError。"""
    thread, box = worker
    thread.join(timeout)
    assert not thread.is_alive(), "调用没有按预期返回"
    return box["result"]


def _ask_hub_rpc(frame_type, feature, timeout=5):
    """在线程里发一次 hub_rpc 查询，返回 (线程, 装结果的 box)。"""
    from core import hub_rpc

    box: dict = {}
    worker = threading.Thread(
        target=lambda: box.update(result=hub_rpc.call(frame_type, {"service": "k3f9qm"}, feature, timeout)), daemon=True)
    worker.start()
    return worker, box


def test_hub_rpc_follows_connection_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _import_ws_client(monkeypatch)
    from core import hub_rpc

    monkeypatch.setattr(module, "_start_heartbeat", lambda ws: None)
    monkeypatch.setattr(module, "start_status_reporting", lambda ws: None)
    monkeypatch.setattr(module.agent_upgrade, "on_connected", lambda ws: None)
    sent: list[dict] = []
    ws = SimpleNamespace(keep_running=True, send=lambda raw: sent.append(json.loads(raw)))
    env_query = ("env_config_query", {"service": "k3f9qm"}, "env_config", 5)
    snapshot_query = ("instance_snapshot_query", {"service": "k3f9qm"}, "instance_snapshot", 5)

    def ask(query):
        box: dict = {}
        worker = threading.Thread(target=lambda: box.update(result=hub_rpc.call(*query)), daemon=True)
        worker.start()
        return worker, box

    def frames(frame_type):
        return [message for message in sent if message.get("type") == frame_type]

    def wait_for_frames(frame_type, count):
        deadline = time.monotonic() + 2
        while len(frames(frame_type)) < count and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(frames(frame_type)) == count, f"没有等到第 {count} 个 {frame_type} 帧：{sent}"
        return frames(frame_type)[count - 1]

    assert hub_rpc.call(*env_query) == (503, {"reason": "hub_disconnected"})     # 还没连上

    module._on_open(ws)                                   # 连上：特性未知，请求等 hub_hello，不立即判 501
    waiting = ask(env_query)
    time.sleep(0.1)
    assert sent == []

    module._on_message(ws, json.dumps({"type": "hub_hello", "features": ["header_auth", "env_config", "instance_snapshot"]}))
    frame = wait_for_frames("env_config_query", 1)
    assert frame["service"] == "k3f9qm"
    module._on_message(ws, json.dumps({"type": "env_config_result", "requestId": frame["requestId"],
                                       "env": {"allowUi": False, "allowDs": True}}))
    assert _finish(waiting) == (200, {"allowUi": False, "allowDs": True})

    snapshot_worker = ask(snapshot_query)                 # 实例快照走同一条通道
    frame = wait_for_frames("instance_snapshot_query", 1)
    module._on_message(ws, json.dumps({"type": "instance_snapshot_result", "requestId": frame["requestId"],
                                       "snapshot": {"schemaVersion": 1}}))
    assert _finish(snapshot_worker) == (200, {"schemaVersion": 1})

    pending = ask(env_query)                              # 等结果期间断线：立即返回，不干等超时
    wait_for_frames("env_config_query", 2)
    started = time.monotonic()
    module._on_close(ws, 1006, "gone")
    assert _finish(pending) == (503, {"reason": "hub_disconnected"})
    assert time.monotonic() - started < 1
    assert hub_rpc.call(*env_query) == (503, {"reason": "hub_disconnected"})

    module._on_open(ws)                                   # 重连：特性回到未知，重新等本连接的 hub_hello
    again = ask(env_query)
    time.sleep(0.1)
    assert len(frames("env_config_query")) == 2
    module._on_message(ws, json.dumps({"type": "hub_hello", "features": ["header_auth"]}))   # 新连接上的中枢不支持
    assert _finish(again) == (501, {"reason": "hub_unsupported"})


_OPS_FIELDS = {"service": "app", "code": "112233", "operation": "backups:restore",
               "operator": {"userId": 1, "name": "", "ip": ""}}


def _quiet_open(monkeypatch: pytest.MonkeyPatch, module) -> None:
    """让 _on_open 只剩连接登记：心跳、状态上报、自升级线程都不起。"""
    monkeypatch.setattr(module, "_start_heartbeat", lambda ws: None)
    monkeypatch.setattr(module, "start_status_reporting", lambda ws: None)
    monkeypatch.setattr(module.agent_upgrade, "on_connected", lambda ws: None)


def test_requests_during_outbox_replay_see_the_hub_as_not_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _import_ws_client(monkeypatch)
    from core import hub_rpc, ops_code

    _quiet_open(monkeypatch, module)
    replaying = threading.Event()
    finish_replay = threading.Event()

    def slow_flush(**kwargs):                             # 补投没有条数和时长上限
        replaying.set()
        finish_replay.wait(5)

    monkeypatch.setattr(module.outbox, "flush", slow_flush)
    ws = SimpleNamespace(keep_running=True, send=lambda raw: None)
    opener = threading.Thread(target=module._on_open, args=(ws,), daemon=True)
    opener.start()
    try:
        assert replaying.wait(2)
        # 补投还没做完，通道还没登记这条连接：请求得到「未连上」的 503（应用会重试），不会被判成 501
        assert hub_rpc.call("env_config_query", {"service": "k3f9qm"}, "env_config", 1) == (503, {"reason": "hub_disconnected"})
        assert ops_code.verify(dict(_OPS_FIELDS), timeout=1) == (503, {"reason": "unreachable"})
    finally:
        finish_replay.set()
    opener.join(2)
    assert not opener.is_alive()
    assert ops_code.verify(dict(_OPS_FIELDS), timeout=1) == (503, {"reason": "negotiating"})   # 补投做完才登记，开始等 hub_hello


def test_negotiation_window_starts_after_the_outbox_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _import_ws_client(monkeypatch)
    from core import hub_rpc, ops_code

    _quiet_open(monkeypatch, module)
    now = [100.0]
    monkeypatch.setattr(hub_rpc, "_monotonic", lambda: now[0])
    monkeypatch.setattr(ops_code, "_monotonic", lambda: now[0])
    monkeypatch.setattr(module.outbox, "flush", lambda **kwargs: now.__setitem__(0, now[0] + 60))   # 一次很久的补投
    sent: list[dict] = []
    ws = SimpleNamespace(keep_running=True, send=lambda raw: sent.append(json.loads(raw)))

    module._on_open(ws)

    # 窗口从补投做完起算，此刻仍在窗口内：口令校验回 negotiating，环境开关查询在等 hub_hello，都不是 501
    assert ops_code.verify(dict(_OPS_FIELDS), timeout=1) == (503, {"reason": "negotiating"})
    worker = _ask_hub_rpc("env_config_query", "env_config")
    time.sleep(0.1)
    assert sent == []
    module._on_message(ws, json.dumps({"type": "hub_hello", "features": ["env_config"]}))
    deadline = time.monotonic() + 2
    while not sent and time.monotonic() < deadline:
        time.sleep(0.01)
    assert [message["type"] for message in sent] == ["env_config_query"]
    module._on_message(ws, json.dumps({"type": "env_config_result", "requestId": sent[0]["requestId"],
                                       "env": {"allowUi": True, "allowDs": True}}))
    assert _finish(worker) == (200, {"allowUi": True, "allowDs": True})


def test_channels_are_registered_even_if_the_outbox_replay_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _import_ws_client(monkeypatch)
    from core import hub_rpc, ops_code

    _quiet_open(monkeypatch, module)

    def broken_flush(**kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(module.outbox, "flush", broken_flush)
    ws = SimpleNamespace(keep_running=True, send=lambda raw: None)

    with pytest.raises(OSError):
        module._on_open(ws)

    # 补投抛了异常，连接照样登记：两个通道都在等 hub_hello，而不是停在「未连上」
    assert ops_code.verify(dict(_OPS_FIELDS), timeout=1) == (503, {"reason": "negotiating"})
    assert hub_rpc.call("env_config_query", {"service": "k3f9qm"}, "env_config", 0.2) == (504, {"reason": "timeout"})
