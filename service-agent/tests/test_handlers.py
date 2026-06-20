import os

# handlers 现在会 import config，而 config 在缺少 WS_URL/AGENT_KEY 时会 sys.exit。
# 与 test_rolling.py / test_nacos_client.py 同款：导入前先注入测试用默认值。
os.environ.setdefault("WS_URL", "ws://test")
os.environ.setdefault("AGENT_KEY", "test-key")

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
    # /srv/a 须先过 dir 安全闸（受管根设为 /srv、不设自身目录），才能走到「不支持的 action」分支。
    monkeypatch.setattr(handlers.config, "MANAGED_PROJECTS_ROOT", "/srv")
    monkeypatch.setattr(handlers.config, "SELF_PROJECT_DIR", "")
    handlers.dispatch(ws, {"requestId": "req-3", "action": "deploy", "dir": "/srv/a"})

    assert "Unsupported action 'deploy'" in _decode_messages(ws)[0]["error"]
    assert "Received command: request_id=req-3, action=deploy, dir=/srv/a" in caplog.text


def test_validate_base_rejects_dir_outside_root(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """dir 通过 isdir 但 realpath 落在受管根之外时，必须拒绝并提示「不在受管目录」。"""
    ws = FakeWebSocket()
    root = tmp_path / "managed"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    monkeypatch.setattr(handlers.config, "MANAGED_PROJECTS_ROOT", str(root))
    monkeypatch.setattr(handlers.config, "SELF_PROJECT_DIR", "")

    res = handlers._validate_base(
        ws, {"requestId": "r1", "action": "restart", "dir": str(outside)}
    )

    assert res is None
    assert "不在受管目录" in _decode_messages(ws)[0]["error"]


def test_validate_base_rejects_self_project(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """命中 agent 自身 compose 目录（SELF_PROJECT_DIR）时，必须拒绝并提示「禁止操作 agent 自身」。"""
    ws = FakeWebSocket()
    root = tmp_path / "managed"
    root.mkdir()
    self_dir = root / "agent"
    self_dir.mkdir()

    monkeypatch.setattr(handlers.config, "MANAGED_PROJECTS_ROOT", str(root))
    monkeypatch.setattr(handlers.config, "SELF_PROJECT_DIR", str(self_dir))

    res = handlers._validate_base(
        ws, {"requestId": "r1", "action": "stop", "dir": str(self_dir)}
    )

    assert res is None
    assert "禁止操作 agent 自身" in _decode_messages(ws)[0]["error"]


def test_validate_base_passes_for_dir_inside_root(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """根内、且非自身目录的合法 dir 必须放行，返回 (request_id, action, project_dir) 三元组。"""
    ws = FakeWebSocket()
    root = tmp_path / "managed"
    root.mkdir()
    project = root / "biz-app"
    project.mkdir()

    monkeypatch.setattr(handlers.config, "MANAGED_PROJECTS_ROOT", str(root))
    monkeypatch.setattr(handlers.config, "SELF_PROJECT_DIR", str(root / "agent"))

    res = handlers._validate_base(
        ws, {"requestId": "r9", "action": "restart", "dir": str(project)}
    )

    assert res == ("r9", "restart", str(project))
    assert ws.messages == []


def test_validate_base_rejects_path_traversal_escape(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """通过 `..` 穿越逃逸到受管根之外的 dir，realpath 归一后必须被拒绝。"""
    ws = FakeWebSocket()
    root = tmp_path / "managed"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    # root 之内构造一个 `..` 穿越到 outside 的路径；isdir 对该路径为真，但 realpath 落在根外。
    traversal = os.path.join(str(root), "..", "outside")

    monkeypatch.setattr(handlers.config, "MANAGED_PROJECTS_ROOT", str(root))
    monkeypatch.setattr(handlers.config, "SELF_PROJECT_DIR", "")

    res = handlers._validate_base(
        ws, {"requestId": "r1", "action": "force-restart", "dir": traversal}
    )

    assert res is None
    assert "不在受管目录" in _decode_messages(ws)[0]["error"]


def test_validate_base_root_commonpath_valueerror_rejects(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """commonpath 抛 ValueError（如 Windows 跨盘符）时，根判定须从严兜底为「在根外」拒绝。"""
    ws = FakeWebSocket()
    project = tmp_path / "biz"
    project.mkdir()

    monkeypatch.setattr(handlers.config, "MANAGED_PROJECTS_ROOT", str(tmp_path))
    monkeypatch.setattr(handlers.config, "SELF_PROJECT_DIR", "")

    def boom(_paths):
        raise ValueError("paths on different drives")

    monkeypatch.setattr(handlers.os.path, "commonpath", boom)

    res = handlers._validate_base(
        ws, {"requestId": "r1", "action": "restart", "dir": str(project)}
    )

    assert res is None
    assert "不在受管目录" in _decode_messages(ws)[0]["error"]


def test_validate_base_self_commonpath_valueerror_rejects(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """self 判定的 commonpath 抛 ValueError 时，须从严兜底为「命中自身」拒绝。"""
    ws = FakeWebSocket()
    root = tmp_path / "managed"
    root.mkdir()
    project = root / "biz"
    project.mkdir()
    self_dir = root / "agent"
    self_dir.mkdir()

    monkeypatch.setattr(handlers.config, "MANAGED_PROJECTS_ROOT", str(root))
    monkeypatch.setattr(handlers.config, "SELF_PROJECT_DIR", str(self_dir))

    calls = {"n": 0}

    def commonpath_first_ok_then_boom(paths):
        # 第一次（根判定）放行；第二次（self 判定）抛 ValueError，触发从严兜底。
        calls["n"] += 1
        if calls["n"] == 1:
            return os.path.realpath(str(root))
        raise ValueError("paths on different drives")

    monkeypatch.setattr(handlers.os.path, "commonpath", commonpath_first_ok_then_boom)

    res = handlers._validate_base(
        ws, {"requestId": "r1", "action": "stop", "dir": str(project)}
    )

    assert res is None
    assert "禁止操作 agent 自身" in _decode_messages(ws)[0]["error"]


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


def test_handle_start_runs_up_detached(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """start → docker compose up -d；成功时末条消息 status=success。"""
    ws = FakeWebSocket()
    calls: list[list[str]] = []
    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: "compose.yml")
    monkeypatch.setattr(handlers, "run_compose", lambda project_dir, args: (calls.append(args) or (True, "up ok")))

    handlers.handle_start(ws, {}, "req-1", str(tmp_path))

    decoded = _decode_messages(ws)
    assert calls == [["up", "-d"]]
    assert decoded[0]["type"] == "ack"
    assert decoded[-1]["status"] == "success"
    assert "docker compose up -d" in decoded[-1]["output"]


def test_handle_start_is_idempotent_when_already_running(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """幂等：已在运行时 up -d 为 no-op 返回 ok → success。"""
    ws = FakeWebSocket()
    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: "compose.yml")
    monkeypatch.setattr(handlers, "run_compose", lambda *args: (True, "already running"))

    handlers.handle_start(ws, {}, "req-1", str(tmp_path))

    assert _decode_messages(ws)[-1]["status"] == "success"


def test_handle_stop_runs_stop_not_down(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """stop(force) → docker compose stop（绝不能是 down，down 会删容器影响后续 start）。"""
    ws = FakeWebSocket()
    calls: list[list[str]] = []
    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: "compose.yml")
    monkeypatch.setattr(handlers, "run_compose", lambda project_dir, args: (calls.append(args) or (True, "stop ok")))

    handlers.handle_stop(ws, {}, "req-1", str(tmp_path))

    decoded = _decode_messages(ws)
    assert calls == [["stop"]]
    assert ["down"] not in calls
    assert decoded[0]["type"] == "ack"
    assert decoded[-1]["status"] == "success"
    assert "docker compose stop" in decoded[-1]["output"]


def test_handle_force_restart_runs_restart(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """force-restart → docker compose restart；成功时 status=success。"""
    ws = FakeWebSocket()
    calls: list[list[str]] = []
    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: "compose.yml")
    monkeypatch.setattr(handlers, "run_compose", lambda project_dir, args: (calls.append(args) or (True, "restart ok")))

    handlers.handle_force_restart(ws, {}, "req-1", str(tmp_path))

    decoded = _decode_messages(ws)
    assert calls == [["restart"]]
    assert decoded[0]["type"] == "ack"
    assert decoded[-1]["status"] == "success"
    assert "docker compose restart" in decoded[-1]["output"]


def test_handle_start_no_compose_file_sends_error(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """无 compose 文件 → send_error 含「No docker-compose」。"""
    ws = FakeWebSocket()
    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: None)

    handlers.handle_start(ws, {}, "req-1", str(tmp_path))

    assert "No docker-compose" in _decode_messages(ws)[0]["error"]


def test_handle_stop_no_compose_file_sends_error(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    ws = FakeWebSocket()
    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: None)

    handlers.handle_stop(ws, {}, "req-1", str(tmp_path))

    assert "No docker-compose" in _decode_messages(ws)[0]["error"]


def test_handle_force_restart_no_compose_file_sends_error(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    ws = FakeWebSocket()
    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: None)

    handlers.handle_force_restart(ws, {}, "req-1", str(tmp_path))

    assert "No docker-compose" in _decode_messages(ws)[0]["error"]


def test_handle_start_reports_failure(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """失败路径：run_compose 返回 (False, ...) → _reply status=failed。"""
    ws = FakeWebSocket()
    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: "compose.yml")
    monkeypatch.setattr(handlers, "run_compose", lambda *args: (False, "boom"))

    handlers.handle_start(ws, {}, "req-1", str(tmp_path))

    decoded = _decode_messages(ws)
    assert decoded[-1]["status"] == "failed"
    assert "boom" in decoded[-1]["output"]


def test_handle_stop_handles_timeout_and_exception(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """stop 的超时与异常分支：分别回 send_error。"""
    ws = FakeWebSocket()
    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: "compose.yml")
    monkeypatch.setattr(handlers, "run_compose", lambda *args: (_ for _ in ()).throw(subprocess.TimeoutExpired("cmd", 1)))
    handlers.handle_stop(ws, {}, "req-1", str(tmp_path))
    assert _decode_messages(ws)[1]["error"] == "Command execution timed out (5 min)"

    ws = FakeWebSocket()
    monkeypatch.setattr(handlers, "run_compose", lambda *args: (_ for _ in ()).throw(RuntimeError("explode")))
    handlers.handle_stop(ws, {}, "req-2", str(tmp_path))
    assert _decode_messages(ws)[1]["error"] == "explode"


def test_handle_force_restart_handles_timeout_and_exception(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """force-restart 的超时与异常分支：分别回 send_error。"""
    ws = FakeWebSocket()
    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: "compose.yml")
    monkeypatch.setattr(handlers, "run_compose", lambda *args: (_ for _ in ()).throw(subprocess.TimeoutExpired("cmd", 1)))
    handlers.handle_force_restart(ws, {}, "req-1", str(tmp_path))
    assert _decode_messages(ws)[1]["error"] == "Command execution timed out (5 min)"

    ws = FakeWebSocket()
    monkeypatch.setattr(handlers, "run_compose", lambda *args: (_ for _ in ()).throw(RuntimeError("explode")))
    handlers.handle_force_restart(ws, {}, "req-2", str(tmp_path))
    assert _decode_messages(ws)[1]["error"] == "explode"


def test_handlers_registry_includes_node_control_actions() -> None:
    """HANDLERS 必须注册 start / stop / force-restart（force-restart 为连字符 key）。"""
    assert handlers.HANDLERS["start"] is handlers.handle_start
    assert handlers.HANDLERS["stop"] is handlers.handle_stop
    assert handlers.HANDLERS["force-restart"] is handlers.handle_force_restart


def test_dispatch_serializes_commands_for_same_directory(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    # 让 tmp_path 下的业务目录通过 dir 安全闸（否则会在 _validate_base 被拒，走不到锁逻辑）。
    monkeypatch.setattr(handlers.config, "MANAGED_PROJECTS_ROOT", str(tmp_path))
    monkeypatch.setattr(handlers.config, "SELF_PROJECT_DIR", "")

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
    # 让 tmp_path 下的业务目录通过 dir 安全闸。
    monkeypatch.setattr(handlers.config, "MANAGED_PROJECTS_ROOT", str(tmp_path))
    monkeypatch.setattr(handlers.config, "SELF_PROJECT_DIR", "")

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
    # 让 tmp_path 下的业务目录通过 dir 安全闸。
    monkeypatch.setattr(handlers.config, "MANAGED_PROJECTS_ROOT", str(tmp_path))
    monkeypatch.setattr(handlers.config, "SELF_PROJECT_DIR", "")
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
