import json
import subprocess
import threading
import time

import pytest

from core import handlers


class FakeWebSocket:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.messages: list[str] = []

    def send(self, payload: str) -> None:
        if self.fail:
            raise RuntimeError("send failed")
        self.messages.append(payload)


def _decode_messages(ws: FakeWebSocket) -> list[dict]:
    return [json.loads(item) for item in ws.messages]


def test_send_message_and_send_error_handle_edge_cases(caplog: pytest.LogCaptureFixture) -> None:
    ws = FakeWebSocket()
    caplog.set_level("WARNING")
    handlers.send_message(ws, {"type": "ping"})
    handlers.send_message(None, {"type": "ignored"})
    handlers.send_message(FakeWebSocket(fail=True), {"type": "ignored"})
    handlers.send_error(ws, "req-1", "boom")

    decoded = _decode_messages(ws)
    assert decoded[0] == {"type": "ping"}
    assert decoded[1]["status"] == "failed"
    assert decoded[1]["requestId"] == "req-1"
    assert "Command failed: request_id=req-1, error=boom" in caplog.text


def test_clamp_text_short_passthrough() -> None:
    assert handlers._clamp_text("short") == "short"
    assert handlers._clamp_text(None) is None


def test_clamp_text_truncates_by_bytes_keeping_head_and_tail() -> None:
    text = "HEAD-开始\n" + "x" * 300_000 + "\nTAIL-结尾错误行"
    out = handlers._clamp_text(text)
    assert len(out.encode("utf-8")) <= handlers.RESULT_TEXT_MAX_BYTES
    assert out.startswith("HEAD-")
    assert out.endswith("TAIL-结尾错误行")
    assert "截断" in out


def test_clamp_text_multibyte_boundary_stays_within_limit() -> None:
    out = handlers._clamp_text("⠿" * 100_000)  # compose 进度符,3 字节/个
    assert len(out.encode("utf-8")) <= handlers.RESULT_TEXT_MAX_BYTES


def test_reply_and_send_error_clamp_oversized_text() -> None:
    """update 拉大镜像的全量 compose 输出曾超 hub 命令表 TEXT 列(64KB),回写 Data too long →
    hub 不 ack → 本 agent 无限补投(2026-08-17 事故)。发送前截到 60000 字节内,老 hub 也安全。"""
    ws = FakeWebSocket()
    handlers._reply(ws, "req-big", True, "y" * 300_000, "update", "/data/app")
    sent = _decode_messages(ws)[-1]
    assert sent["status"] == "success"
    assert len(sent["output"].encode("utf-8")) <= handlers.RESULT_TEXT_MAX_BYTES

    ws2 = FakeWebSocket()
    handlers.send_error(ws2, "req-big-err", "e" * 300_000)
    err = _decode_messages(ws2)[-1]
    assert len(err["error"].encode("utf-8")) <= handlers.RESULT_TEXT_MAX_BYTES


def test_results_are_remembered_in_outbox_until_acked(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """result 必达:发送前记账 outbox,hub result_ack 前保持未确认(断连丢失可补投)。"""
    from core import outbox

    ws = FakeWebSocket()
    handlers.send_error(ws, "req-e", "boom")
    assert outbox.pending_count() == 1  # 失败 result 已记账

    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: "compose.yml")
    monkeypatch.setattr(handlers, "run_compose", lambda *args: (True, "restart ok"))
    handlers.handle_restart(ws, {}, "req-r", str(tmp_path))
    assert outbox.pending_count() == 2  # 成功 result 也记账
    assert _decode_messages(ws)[-1]["status"] == "success"  # 同时仍立即经当前 ws 发送

    # ws 发送失败(连接刚死)时 result 不丢——仍在 outbox 等补投
    dead = FakeWebSocket(fail=True)
    handlers.send_error(dead, "req-dead", "gone")
    assert outbox.pending_count() == 3

    outbox.ack("req-r")
    assert outbox.pending_count() == 2

    # ack(processing)不记账
    before = outbox.pending_count()
    handlers.send_message(ws, {"type": "ack", "requestId": "req-x", "status": "processing"})
    assert outbox.pending_count() == before


def test_validate_base_and_dispatch_errors(monkeypatch: pytest.MonkeyPatch, tmp_path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO")
    ws = FakeWebSocket()

    assert handlers._validate_base(ws, {"requestId": "req-1"}) is None
    assert "Missing required fields" in _decode_messages(ws)[0]["error"]

    ws = FakeWebSocket()
    missing_dir = tmp_path / "missing"
    assert handlers._validate_base(ws, {"requestId": "req-2", "action": "restart", "dir": str(missing_dir)}) is None
    assert str(missing_dir) in _decode_messages(ws)[0]["error"]

    ws = FakeWebSocket()
    monkeypatch.setattr(handlers.os.path, "isdir", lambda value: True)
    handlers.dispatch(ws, {"requestId": "req-3", "action": "deploy", "dir": "/srv/a"})

    assert "Unsupported action 'deploy'" in _decode_messages(ws)[0]["error"]
    assert "Received command: request_id=req-3, action=deploy, dir=/srv/a" in caplog.text


def test_handle_update_validation_and_errors(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    ws = FakeWebSocket()
    handlers.handle_update(ws, {}, "req-1", str(tmp_path))
    assert _decode_messages(ws)[0]["error"] == "Action 'update' requires the 'image' field"

    ws = FakeWebSocket()
    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: None)
    handlers.handle_update(ws, {"image": "repo/app:1"}, "req-2", str(tmp_path))
    assert "No docker-compose.yaml/yml found" in _decode_messages(ws)[0]["error"]

    ws = FakeWebSocket()
    monkeypatch.setattr(handlers, "read_compose_file", lambda compose_file: "services: {}\n")
    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: "compose.yml")
    monkeypatch.setattr(handlers, "update_image_in_compose", lambda *args: (_ for _ in ()).throw(subprocess.TimeoutExpired("cmd", 1)))
    restore_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(handlers, "restore_compose_file", lambda compose_file, content: restore_calls.append((compose_file, content)))
    handlers.handle_update(ws, {"image": "repo/app:1"}, "req-3", str(tmp_path))
    assert _decode_messages(ws)[1]["error"] == "Command execution timed out (5 min)"
    assert restore_calls == [("compose.yml", "services: {}\n")]

    ws = FakeWebSocket()
    monkeypatch.setattr(handlers, "read_compose_file", lambda compose_file: "services: {}\n")
    monkeypatch.setattr(handlers, "update_image_in_compose", lambda *args: (_ for _ in ()).throw(RuntimeError("explode")))
    restore_calls = []
    monkeypatch.setattr(handlers, "restore_compose_file", lambda compose_file, content: restore_calls.append((compose_file, content)))
    handlers.handle_update(ws, {"image": "repo/app:1"}, "req-4", str(tmp_path))
    assert _decode_messages(ws)[1]["error"] == "explode"
    assert restore_calls == [("compose.yml", "services: {}\n")]


def test_handle_update_stops_when_no_service_matches(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    ws = FakeWebSocket()

    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: "compose.yml")
    monkeypatch.setattr(handlers, "read_compose_file", lambda compose_file: "services: {}\n")
    monkeypatch.setattr(handlers, "update_image_in_compose", lambda *args: [])

    handlers.handle_update(ws, {"image": "repo/app:9"}, "req-5", str(tmp_path))

    decoded = _decode_messages(ws)
    assert decoded[0]["type"] == "ack"
    assert "No service image matched repository" in decoded[1]["error"]


def test_handle_update_success_path(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    ws = FakeWebSocket()
    compose_calls: list[list[str]] = []

    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: "compose.yml")
    monkeypatch.setattr(handlers, "read_compose_file", lambda compose_file: "services: {}\n")
    monkeypatch.setattr(handlers, "update_image_in_compose", lambda *args: ["api"])

    def fake_run(project_dir, args):
        compose_calls.append(args)
        if args == ["pull"]:
            return True, "pull ok"
        if args == ["down"]:
            return True, "down ok"
        return True, "up ok"

    monkeypatch.setattr(handlers, "run_compose", fake_run)

    handlers.handle_update(ws, {"image": "repo/app:9"}, "req-1", str(tmp_path))

    decoded = _decode_messages(ws)
    assert decoded[0]["type"] == "ack"
    assert decoded[-1]["status"] == "success"
    assert "Updated image in services: api" in decoded[-1]["output"]
    assert decoded[-1]["message"] == f"Action 'update' succeeded in {tmp_path}."  # message 措辞与成败一致
    assert compose_calls == [["pull"], ["down"], ["up", "-d"]]


def test_handle_update_strips_image_whitespace(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """UI 粘贴带首尾空格的镜像曾致 docker invalid reference format——写入 compose 前必须剪净。"""
    ws = FakeWebSocket()
    seen_images: list[str] = []

    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: "compose.yml")
    monkeypatch.setattr(handlers, "read_compose_file", lambda compose_file: "services: {}\n")

    def fake_update(compose_file, image):
        seen_images.append(image)
        return ["api"]

    monkeypatch.setattr(handlers, "update_image_in_compose", fake_update)
    monkeypatch.setattr(handlers, "run_compose", lambda *args: (True, "ok"))

    handlers.handle_update(ws, {"image": "  repo/app:9  "}, "req-strip", str(tmp_path))

    assert seen_images == ["repo/app:9"]
    assert _decode_messages(ws)[-1]["status"] == "success"

    # 全空白视同未填
    ws2 = FakeWebSocket()
    handlers.handle_update(ws2, {"image": "   "}, "req-blank", str(tmp_path))
    assert _decode_messages(ws2)[0]["error"] == "Action 'update' requires the 'image' field"


def test_handle_update_stops_before_up_when_down_fails(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    ws = FakeWebSocket()
    compose_calls: list[list[str]] = []
    restore_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: "compose.yml")
    monkeypatch.setattr(handlers, "read_compose_file", lambda compose_file: "services: {}\n")
    monkeypatch.setattr(handlers, "restore_compose_file", lambda compose_file, content: restore_calls.append((compose_file, content)))
    monkeypatch.setattr(handlers, "update_image_in_compose", lambda *args: ["api"])

    def fake_run(project_dir, args):
        compose_calls.append(args)
        if args == ["pull"]:
            return True, "pull ok"
        if args == ["down"]:
            return False, "down failed"
        return True, "recovery ok"

    monkeypatch.setattr(handlers, "run_compose", fake_run)

    handlers.handle_update(ws, {"image": "repo/app:9"}, "req-1", str(tmp_path))

    decoded = _decode_messages(ws)
    assert decoded[-1]["status"] == "failed"
    assert "Restored compose file" in decoded[-1]["output"]
    assert "recovery: docker compose up -d" in decoded[-1]["output"]
    assert compose_calls == [["pull"], ["down"], ["up", "-d"]]
    assert restore_calls == [("compose.yml", "services: {}\n")]


def test_handle_update_restores_when_pull_fails(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    ws = FakeWebSocket()
    compose_calls: list[list[str]] = []
    restore_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: "compose.yml")
    monkeypatch.setattr(handlers, "read_compose_file", lambda compose_file: "services: {}\n")
    monkeypatch.setattr(handlers, "restore_compose_file", lambda compose_file, content: restore_calls.append((compose_file, content)))
    monkeypatch.setattr(handlers, "update_image_in_compose", lambda *args: ["api"])

    def fake_run(project_dir, args):
        compose_calls.append(args)
        return False, "pull failed"

    monkeypatch.setattr(handlers, "run_compose", fake_run)

    handlers.handle_update(ws, {"image": "repo/app:9"}, "req-pull", str(tmp_path))

    decoded = _decode_messages(ws)
    assert decoded[-1]["status"] == "failed"
    assert decoded[-1]["message"] == f"Action 'update' failed in {tmp_path}."  # 失败不再写 finished
    assert compose_calls == [["pull"]]
    assert restore_calls == [("compose.yml", "services: {}\n")]


def test_handle_update_restores_after_up_failure(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    ws = FakeWebSocket()
    compose_calls: list[list[str]] = []
    restore_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: "compose.yml")
    monkeypatch.setattr(handlers, "read_compose_file", lambda compose_file: "services: {}\n")
    monkeypatch.setattr(handlers, "restore_compose_file", lambda compose_file, content: restore_calls.append((compose_file, content)))
    monkeypatch.setattr(handlers, "update_image_in_compose", lambda *args: ["api"])

    def fake_run(project_dir, args):
        compose_calls.append(args)
        if args == ["pull"]:
            return True, "pull ok"
        if args == ["down"]:
            return True, "down ok"
        if len(compose_calls) == 3:
            return False, "up failed"
        return True, "recovery ok"

    monkeypatch.setattr(handlers, "run_compose", fake_run)

    handlers.handle_update(ws, {"image": "repo/app:9"}, "req-up", str(tmp_path))

    decoded = _decode_messages(ws)
    assert decoded[-1]["status"] == "failed"
    assert "Recovery failed" not in decoded[-1]["output"]
    assert compose_calls == [["pull"], ["down"], ["up", "-d"], ["up", "-d"]]
    assert restore_calls == [("compose.yml", "services: {}\n")]


def test_handle_restart_paths(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    ws = FakeWebSocket()
    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: None)
    handlers.handle_restart(ws, {}, "req-1", str(tmp_path))
    assert "No docker-compose.yaml/yml found" in _decode_messages(ws)[0]["error"]

    ws = FakeWebSocket()
    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: "compose.yml")
    monkeypatch.setattr(handlers, "run_compose", lambda *args: (_ for _ in ()).throw(subprocess.TimeoutExpired("cmd", 1)))
    handlers.handle_restart(ws, {}, "req-2", str(tmp_path))
    assert _decode_messages(ws)[1]["error"] == "Command execution timed out (5 min)"

    ws = FakeWebSocket()
    monkeypatch.setattr(handlers, "run_compose", lambda *args: (_ for _ in ()).throw(RuntimeError("explode")))
    handlers.handle_restart(ws, {}, "req-3", str(tmp_path))
    assert _decode_messages(ws)[1]["error"] == "explode"

    ws = FakeWebSocket()
    monkeypatch.setattr(handlers, "run_compose", lambda *args: (True, "restart ok"))
    handlers.handle_restart(ws, {}, "req-4", str(tmp_path))
    assert _decode_messages(ws)[-1]["status"] == "success"


def test_dispatch_serializes_commands_for_same_directory(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()

    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    call_order: list[str] = []
    observed_states: list[dict] = []

    def fake_restart(ws, data, request_id, project_dir):
        call_order.append(request_id)
        observed_states.append(handlers.get_command_execution_state())
        if request_id == "req-1":
            first_entered.set()
            assert not second_entered.is_set()
            assert release_first.wait(timeout=1)
        else:
            second_entered.set()

    monkeypatch.setitem(handlers.HANDLERS, "restart", fake_restart)

    first = threading.Thread(
        target=handlers.dispatch,
        args=(FakeWebSocket(), {"requestId": "req-1", "action": "restart", "dir": str(shared_dir)}),
    )
    second = threading.Thread(
        target=handlers.dispatch,
        args=(FakeWebSocket(), {"requestId": "req-2", "action": "restart", "dir": str(shared_dir)}),
    )

    first.start()
    assert first_entered.wait(timeout=1)

    second.start()
    time.sleep(0.05)
    assert not second_entered.is_set()

    release_first.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert call_order == ["req-1", "req-2"]
    assert second_entered.is_set()
    assert observed_states[0]["activeCommands"] == 1
    assert observed_states[0]["queuedCommands"] == 0
    assert observed_states[0]["projects"][0]["activeRequestId"] == "req-1"
    assert handlers.get_command_execution_state() == {"activeCommands": 0, "queuedCommands": 0, "projects": []}


def test_dispatch_allows_parallel_commands_for_different_directories(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()

    entered = threading.Barrier(2)
    release = threading.Event()
    started: list[str] = []

    def fake_restart(ws, data, request_id, project_dir):
        started.append(request_id)
        entered.wait(timeout=1)
        assert release.wait(timeout=1)

    monkeypatch.setitem(handlers.HANDLERS, "restart", fake_restart)

    first = threading.Thread(
        target=handlers.dispatch,
        args=(FakeWebSocket(), {"requestId": "req-a", "action": "restart", "dir": str(dir_a)}),
    )
    second = threading.Thread(
        target=handlers.dispatch,
        args=(FakeWebSocket(), {"requestId": "req-b", "action": "restart", "dir": str(dir_b)}),
    )

    first.start()
    second.start()

    deadline = time.time() + 1
    while len(started) < 2 and time.time() < deadline:
        time.sleep(0.01)

    assert sorted(started) == ["req-a", "req-b"]

    release.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert handlers.get_command_execution_state() == {"activeCommands": 0, "queuedCommands": 0, "projects": []}


def test_dispatch_logs_when_command_waits_for_project_lock(monkeypatch: pytest.MonkeyPatch, tmp_path, caplog: pytest.LogCaptureFixture) -> None:
    shared_dir = tmp_path / "shared-log"
    shared_dir.mkdir()
    first_entered = threading.Event()
    release_first = threading.Event()

    def fake_restart(ws, data, request_id, project_dir):
        if request_id == "req-1":
            first_entered.set()
            assert release_first.wait(timeout=1)

    monkeypatch.setitem(handlers.HANDLERS, "restart", fake_restart)
    caplog.set_level("INFO")

    first = threading.Thread(
        target=handlers.dispatch,
        args=(FakeWebSocket(), {"requestId": "req-1", "action": "restart", "dir": str(shared_dir)}),
    )
    second = threading.Thread(
        target=handlers.dispatch,
        args=(FakeWebSocket(), {"requestId": "req-2", "action": "restart", "dir": str(shared_dir)}),
    )

    first.start()
    assert first_entered.wait(timeout=1)
    second.start()
    time.sleep(0.05)
    release_first.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert "Command queued on project lock: request_id=req-2" in caplog.text
    assert "Command acquired project lock: request_id=req-1" in caplog.text
    assert "Command released project lock: request_id=req-2" in caplog.text


def test_handle_restart_graceful_waits_for_health_before_success(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    ws = FakeWebSocket()
    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: "compose.yml")
    monkeypatch.setattr(handlers, "run_compose", lambda *args: (True, "restart ok"))
    monkeypatch.setattr(handlers, "resolve_container_port_mapping", lambda compose_file: 13099)
    monkeypatch.setattr(handlers, "wait_healthy", lambda port: (True, "healthy"))

    handlers.handle_restart(ws, {"graceful": True}, "req-1", str(tmp_path))

    decoded = _decode_messages(ws)
    assert decoded[-1]["status"] == "success"
    assert "healthcheck" in decoded[-1]["output"]


def test_handle_restart_graceful_fails_when_unhealthy(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    ws = FakeWebSocket()
    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: "compose.yml")
    monkeypatch.setattr(handlers, "run_compose", lambda *args: (True, "restart ok"))
    monkeypatch.setattr(handlers, "resolve_container_port_mapping", lambda compose_file: 13099)
    monkeypatch.setattr(handlers, "wait_healthy", lambda port: (False, "healthcheck timed out after 120s"))

    handlers.handle_restart(ws, {"graceful": True}, "req-2", str(tmp_path))

    decoded = _decode_messages(ws)
    assert decoded[-1]["status"] == "failed"
    assert "timed out" in decoded[-1]["output"]


def test_handle_restart_graceful_fails_when_port_not_resolvable(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    ws = FakeWebSocket()
    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: "compose.yml")
    monkeypatch.setattr(handlers, "run_compose", lambda *args: (True, "restart ok"))
    monkeypatch.setattr(handlers, "resolve_container_port_mapping", lambda compose_file: None)

    handlers.handle_restart(ws, {"graceful": True}, "req-3", str(tmp_path))

    assert _decode_messages(ws)[-1]["status"] == "failed"


def test_handle_restart_non_graceful_unaffected(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """graceful 未传时不应该调用健康检查——回归现有行为。"""
    ws = FakeWebSocket()
    called = []
    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: "compose.yml")
    monkeypatch.setattr(handlers, "run_compose", lambda *args: (True, "restart ok"))
    monkeypatch.setattr(handlers, "wait_healthy", lambda port: called.append(port) or (True, "healthy"))

    handlers.handle_restart(ws, {}, "req-4", str(tmp_path))

    assert _decode_messages(ws)[-1]["status"] == "success"
    assert called == []


def test_handle_update_graceful_waits_for_health_before_success(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    ws = FakeWebSocket()
    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: "compose.yml")
    monkeypatch.setattr(handlers, "read_compose_file", lambda compose_file: "services: {}\n")
    monkeypatch.setattr(handlers, "update_image_in_compose", lambda *args: ["api"])
    monkeypatch.setattr(handlers, "run_compose", lambda project_dir, args: (True, f"{args} ok"))
    monkeypatch.setattr(handlers, "resolve_container_port_mapping", lambda compose_file: 13099)
    monkeypatch.setattr(handlers, "wait_healthy", lambda port: (True, "healthy"))

    handlers.handle_update(ws, {"image": "repo/app:9", "graceful": True}, "req-5", str(tmp_path))

    decoded = _decode_messages(ws)
    assert decoded[-1]["status"] == "success"
    assert "healthcheck" in decoded[-1]["output"]


def test_handle_update_graceful_fails_when_unhealthy(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    ws = FakeWebSocket()
    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: "compose.yml")
    monkeypatch.setattr(handlers, "read_compose_file", lambda compose_file: "services: {}\n")
    monkeypatch.setattr(handlers, "update_image_in_compose", lambda *args: ["api"])
    monkeypatch.setattr(handlers, "run_compose", lambda project_dir, args: (True, f"{args} ok"))
    monkeypatch.setattr(handlers, "resolve_container_port_mapping", lambda compose_file: 13099)
    monkeypatch.setattr(handlers, "wait_healthy", lambda port: (False, "healthcheck timed out after 120s"))

    handlers.handle_update(ws, {"image": "repo/app:9", "graceful": True}, "req-6", str(tmp_path))

    assert _decode_messages(ws)[-1]["status"] == "failed"


def test_handle_drain_missing_compose_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    ws = FakeWebSocket()
    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: None)

    handlers.handle_drain(ws, {}, "req-1", str(tmp_path))

    assert "No docker-compose.yaml/yml found" in _decode_messages(ws)[0]["error"]


def test_handle_drain_port_not_resolvable(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    ws = FakeWebSocket()
    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: "compose.yml")
    monkeypatch.setattr(handlers, "resolve_container_port_mapping", lambda compose_file: None)

    handlers.handle_drain(ws, {}, "req-2", str(tmp_path))

    decoded = _decode_messages(ws)
    assert decoded[-1]["status"] == "failed"
    assert "container port 80" in decoded[-1]["error"]


def test_handle_drain_success_forwards_token(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    ws = FakeWebSocket()
    drain_calls = []

    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: "compose.yml")
    monkeypatch.setattr(handlers, "resolve_container_port_mapping", lambda compose_file: 13099)
    monkeypatch.setattr(handlers, "drain", lambda port, token=None: drain_calls.append((port, token)) or (True, "drained"))

    handlers.handle_drain(ws, {"shutdownToken": "secret"}, "req-3", str(tmp_path))

    decoded = _decode_messages(ws)
    assert decoded[0]["type"] == "ack"
    assert decoded[-1]["status"] == "success"
    assert drain_calls == [(13099, "secret")]


def test_handle_drain_failure_reported(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    ws = FakeWebSocket()
    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: "compose.yml")
    monkeypatch.setattr(handlers, "resolve_container_port_mapping", lambda compose_file: 13099)
    monkeypatch.setattr(handlers, "drain", lambda port, token=None: (False, "forbidden"))

    handlers.handle_drain(ws, {}, "req-4", str(tmp_path))

    decoded = _decode_messages(ws)
    assert decoded[-1]["status"] == "failed"
    assert "forbidden" in decoded[-1]["output"]


def test_dispatch_routes_drain_action(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    ws = FakeWebSocket()
    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: "compose.yml")
    monkeypatch.setattr(handlers, "resolve_container_port_mapping", lambda compose_file: 13099)
    monkeypatch.setattr(handlers, "drain", lambda port, token=None: (True, "drained"))

    handlers.dispatch(ws, {"requestId": "req-5", "action": "drain", "dir": str(tmp_path)})

    assert _decode_messages(ws)[-1]["status"] == "success"
