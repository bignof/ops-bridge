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
    assert compose_calls == [["pull"], ["down"], ["up", "-d"]]


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
