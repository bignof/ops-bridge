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


# ─────────────────────────────────────────────
# handle_update — 镜像 registry 白名单闸（pull 之前拦截）
# ─────────────────────────────────────────────

def test_handle_update_rejects_image_outside_allowlist_without_pulling(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """非白名单 image → failed，且绝不调用 run_compose（pull 未发生）、不改写 compose。"""
    ws = FakeWebSocket()
    compose_calls: list[list[str]] = []
    update_calls = {"n": 0}

    monkeypatch.setattr(handlers.config, "IMAGE_REGISTRY_ALLOWLIST", ["registry.example.com"])
    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: "compose.yml")
    monkeypatch.setattr(handlers, "read_compose_file", lambda compose_file: "services: {}\n")
    monkeypatch.setattr(handlers, "update_image_in_compose", lambda *args: update_calls.__setitem__("n", update_calls["n"] + 1) or ["api"])
    monkeypatch.setattr(handlers, "run_compose", lambda project_dir, args: (compose_calls.append(args) or (True, "ok")))

    handlers.handle_update(ws, {"image": "evil.com/x:1"}, "req-1", str(tmp_path))

    decoded = _decode_messages(ws)
    assert decoded[-1]["status"] == "failed"
    assert "白名单" in decoded[-1]["error"]
    assert "evil.com/x:1" in decoded[-1]["error"]
    # 关键：拦截在 pull 之前——既没跑 run_compose，也没改写 compose
    assert compose_calls == []
    assert update_calls["n"] == 0


def test_handle_update_allows_whitelisted_image_and_proceeds(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """白名单内 image → 放行，走到 update_image_in_compose 与 run_compose(['pull'])。"""
    ws = FakeWebSocket()
    compose_calls: list[list[str]] = []

    monkeypatch.setattr(handlers.config, "IMAGE_REGISTRY_ALLOWLIST", ["registry.example.com"])
    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: "compose.yml")
    monkeypatch.setattr(handlers, "read_compose_file", lambda compose_file: "services: {}\n")
    monkeypatch.setattr(handlers, "update_image_in_compose", lambda *args: ["api"])
    monkeypatch.setattr(handlers, "run_compose", lambda project_dir, args: (compose_calls.append(args) or (True, "ok")))

    handlers.handle_update(ws, {"image": "registry.example.com/app:9"}, "req-1", str(tmp_path))

    decoded = _decode_messages(ws)
    assert decoded[-1]["status"] == "success"
    # 放行后正常进入 pull → down → up -d 流程
    assert compose_calls == [["pull"], ["down"], ["up", "-d"]]


def test_handle_update_empty_allowlist_does_not_block(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """空 allowlist = 不限制：任意 image 都放行（不被白名单闸拦）。"""
    ws = FakeWebSocket()
    compose_calls: list[list[str]] = []

    monkeypatch.setattr(handlers.config, "IMAGE_REGISTRY_ALLOWLIST", [])
    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: "compose.yml")
    monkeypatch.setattr(handlers, "read_compose_file", lambda compose_file: "services: {}\n")
    monkeypatch.setattr(handlers, "update_image_in_compose", lambda *args: ["api"])
    monkeypatch.setattr(handlers, "run_compose", lambda project_dir, args: (compose_calls.append(args) or (True, "ok")))

    handlers.handle_update(ws, {"image": "any-registry.io/x:1"}, "req-1", str(tmp_path))

    assert _decode_messages(ws)[-1]["status"] == "success"
    assert compose_calls == [["pull"], ["down"], ["up", "-d"]]


def test_handle_pull_redeploy_force_blocked_by_allowlist(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """pull-redeploy(force) 复用 handle_update，非白名单 image 同样被拦在 pull 之前。"""
    ws = FakeWebSocket()
    compose_calls: list[list[str]] = []

    monkeypatch.setattr(handlers.config, "IMAGE_REGISTRY_ALLOWLIST", ["registry.example.com"])
    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: "compose.yml")
    monkeypatch.setattr(handlers, "read_compose_file", lambda compose_file: "services: {}\n")
    monkeypatch.setattr(handlers, "update_image_in_compose", lambda *args: ["api"])
    monkeypatch.setattr(handlers, "run_compose", lambda project_dir, args: (compose_calls.append(args) or (True, "ok")))

    handlers.handle_pull_redeploy(ws, {"mode": "force", "image": "evil.com/x:1"}, "req-1", str(tmp_path))

    decoded = _decode_messages(ws)
    assert decoded[-1]["status"] == "failed"
    assert "白名单" in decoded[-1]["error"]
    assert compose_calls == []


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

    handlers.handle_stop(ws, {"mode": "force"}, "req-1", str(tmp_path))

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

    handlers.handle_stop(ws, {"mode": "force"}, "req-1", str(tmp_path))

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
    """stop(force) 的超时与异常分支：分别回 send_error。"""
    ws = FakeWebSocket()
    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: "compose.yml")
    monkeypatch.setattr(handlers, "run_compose", lambda *args: (_ for _ in ()).throw(subprocess.TimeoutExpired("cmd", 1)))
    handlers.handle_stop(ws, {"mode": "force"}, "req-1", str(tmp_path))
    assert _decode_messages(ws)[1]["error"] == "Command execution timed out (5 min)"

    ws = FakeWebSocket()
    monkeypatch.setattr(handlers, "run_compose", lambda *args: (_ for _ in ()).throw(RuntimeError("explode")))
    handlers.handle_stop(ws, {"mode": "force"}, "req-2", str(tmp_path))
    assert _decode_messages(ws)[1]["error"] == "explode"


# ─────────────────────────────────────────────
# handle_stop — graceful 分流（drain → stop）
# ─────────────────────────────────────────────

def test_handle_stop_graceful_drains_then_stops(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """graceful（默认）：先 graceful.drain 再 run_compose(['stop'])，且 drain 在 stop 之前。"""
    ws = FakeWebSocket()
    order: list[str] = []
    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: "compose.yml")
    monkeypatch.setattr(handlers.graceful, "drain", lambda base, timeout=60: order.append(f"drain:{base}:{timeout}"))
    monkeypatch.setattr(handlers, "run_compose", lambda project_dir, args: (order.append(f"compose:{args}") or (True, "stop ok")))

    handlers.handle_stop(
        ws,
        {"healthBaseUrl": "http://192.168.0.30:18029", "shutdownTimeoutSec": 45},
        "req-1",
        str(tmp_path),
    )

    decoded = _decode_messages(ws)
    # 次序断言：drain 必须先于 compose stop
    assert order == ["drain:http://192.168.0.30:18029:45", "compose:['stop']"]
    assert decoded[0]["type"] == "ack"
    assert decoded[-1]["status"] == "success"
    assert "docker compose stop" in decoded[-1]["output"]


def test_handle_stop_defaults_to_graceful(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """不传 mode 时默认走 graceful：会调用 drain。"""
    ws = FakeWebSocket()
    drained = {"n": 0}
    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: "compose.yml")
    monkeypatch.setattr(handlers.graceful, "drain", lambda base, timeout=60: drained.__setitem__("n", drained["n"] + 1))
    monkeypatch.setattr(handlers, "run_compose", lambda project_dir, args: (True, "stop ok"))

    handlers.handle_stop(ws, {"healthBaseUrl": "http://10.0.0.5:18029"}, "req-1", str(tmp_path))

    assert drained["n"] == 1
    assert _decode_messages(ws)[-1]["status"] == "success"


def test_handle_stop_graceful_default_shutdown_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """graceful 未传 shutdownTimeoutSec 时，drain 收到默认 60。"""
    ws = FakeWebSocket()
    seen = {}
    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: "compose.yml")
    monkeypatch.setattr(handlers.graceful, "drain", lambda base, timeout=60: seen.update(base=base, timeout=timeout))
    monkeypatch.setattr(handlers, "run_compose", lambda project_dir, args: (True, "stop ok"))

    handlers.handle_stop(ws, {"healthBaseUrl": "http://10.0.0.5:18029"}, "req-1", str(tmp_path))

    assert seen == {"base": "http://10.0.0.5:18029", "timeout": 60}


def test_handle_stop_graceful_rejects_bad_url_without_draining_or_stopping(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """graceful：healthBaseUrl 非法（drain 抛 ValueError）→ send_error，绝不 run_compose(['stop'])。"""
    ws = FakeWebSocket()
    compose_calls: list[list[str]] = []
    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: "compose.yml")

    def boom(base, timeout=60):
        raise ValueError(f"host 为公网可路由地址，禁止访问: {base}")

    monkeypatch.setattr(handlers.graceful, "drain", boom)
    monkeypatch.setattr(handlers, "run_compose", lambda project_dir, args: (compose_calls.append(args) or (True, "stop ok")))

    handlers.handle_stop(ws, {"healthBaseUrl": "http://8.8.8.8:18029"}, "req-1", str(tmp_path))

    decoded = _decode_messages(ws)
    assert decoded[-1]["status"] == "failed"
    assert "公网" in decoded[-1]["error"]
    # 关键：drain 失败时绝不能 compose stop
    assert compose_calls == []


def test_handle_stop_graceful_drain_failure_does_not_stop(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """graceful：drain 抛 RuntimeError（shutdown 非 200）→ send_error，不自动转 force，不 compose stop。"""
    ws = FakeWebSocket()
    compose_calls: list[list[str]] = []
    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: "compose.yml")
    monkeypatch.setattr(handlers.graceful, "drain", lambda base, timeout=60: (_ for _ in ()).throw(RuntimeError("shutdown 返回 503")))
    monkeypatch.setattr(handlers, "run_compose", lambda project_dir, args: (compose_calls.append(args) or (True, "stop ok")))

    handlers.handle_stop(ws, {"healthBaseUrl": "http://192.168.0.30:18029"}, "req-1", str(tmp_path))

    decoded = _decode_messages(ws)
    assert decoded[-1]["status"] == "failed"
    assert "shutdown" in decoded[-1]["error"]
    assert compose_calls == []


def test_handle_stop_graceful_unexpected_drain_error_does_not_stop(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """graceful：drain 抛非 ValueError/RuntimeError 的异常（如 worker 不可达时 requests 抛的
    ConnectionError，属 OSError 子类）→ 必须走兜底 send_error（status=failed），
    绝不 compose stop、不自动转 force；否则异常逃出 handler 死在 daemon 线程，命令永卡 queued。"""
    ws = FakeWebSocket()
    compose_calls: list[list[str]] = []
    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: "compose.yml")
    monkeypatch.setattr(
        handlers.graceful,
        "drain",
        lambda base, timeout=60: (_ for _ in ()).throw(ConnectionError("worker 不可达")),
    )
    monkeypatch.setattr(handlers, "run_compose", lambda project_dir, args: (compose_calls.append(args) or (True, "stop ok")))

    handlers.handle_stop(ws, {"healthBaseUrl": "http://192.168.0.30:18029"}, "req-1", str(tmp_path))

    decoded = _decode_messages(ws)
    assert decoded[-1]["status"] == "failed"
    assert "drain 失败" in decoded[-1]["error"]
    assert "worker 不可达" in decoded[-1]["error"]
    # 关键：drain 兜底失败时绝不能 compose stop
    assert compose_calls == []


def test_graceful_stop_drain_oserror_propagation_is_caught(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """直接调 _graceful_stop：drain 抛 OSError 子类时不得逃出函数（否则会死在 daemon 线程，
    hub 命令永卡 queued）。验证函数正常返回、回 failed、且未 compose stop。"""
    ws = FakeWebSocket()
    compose_calls: list[list[str]] = []
    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: "compose.yml")
    monkeypatch.setattr(
        handlers.graceful,
        "drain",
        lambda base, timeout=60: (_ for _ in ()).throw(TimeoutError("shutdown 阻塞超时")),
    )
    monkeypatch.setattr(handlers, "run_compose", lambda project_dir, args: (compose_calls.append(args) or (True, "stop ok")))

    # 不应抛异常
    handlers._graceful_stop(ws, {"healthBaseUrl": "http://192.168.0.30:18029"}, "req-1", str(tmp_path))

    decoded = _decode_messages(ws)
    assert decoded[-1]["status"] == "failed"
    assert "drain 失败" in decoded[-1]["error"]
    assert compose_calls == []


def test_handle_stop_graceful_no_compose_file_sends_error(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """graceful：无 compose 文件 → 提前 send_error，不应 drain。"""
    ws = FakeWebSocket()
    drained = {"n": 0}
    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: None)
    monkeypatch.setattr(handlers.graceful, "drain", lambda base, timeout=60: drained.__setitem__("n", drained["n"] + 1))

    handlers.handle_stop(ws, {"healthBaseUrl": "http://192.168.0.30:18029"}, "req-1", str(tmp_path))

    assert "No docker-compose" in _decode_messages(ws)[0]["error"]
    assert drained["n"] == 0


def test_handle_stop_graceful_handles_timeout_and_exception(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """graceful：drain 成功后 run_compose 超时/异常 → 走兜底 send_error。"""
    ws = FakeWebSocket()
    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: "compose.yml")
    monkeypatch.setattr(handlers.graceful, "drain", lambda base, timeout=60: None)
    monkeypatch.setattr(handlers, "run_compose", lambda *args: (_ for _ in ()).throw(subprocess.TimeoutExpired("cmd", 1)))
    handlers.handle_stop(ws, {"healthBaseUrl": "http://192.168.0.30:18029"}, "req-1", str(tmp_path))
    assert _decode_messages(ws)[-1]["error"] == "Command execution timed out (5 min)"

    ws = FakeWebSocket()
    monkeypatch.setattr(handlers, "run_compose", lambda *args: (_ for _ in ()).throw(RuntimeError("explode")))
    handlers.handle_stop(ws, {"healthBaseUrl": "http://192.168.0.30:18029"}, "req-2", str(tmp_path))
    assert _decode_messages(ws)[-1]["error"] == "explode"


# ─────────────────────────────────────────────
# handle_pull_redeploy — graceful（drain→update）/ force（复用 handle_update）
# ─────────────────────────────────────────────

def test_handle_pull_redeploy_force_reuses_update(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """force：直接复用 handle_update，不 drain。"""
    ws = FakeWebSocket()
    order: list[str] = []
    monkeypatch.setattr(handlers.graceful, "drain", lambda base, timeout=60: order.append("drain"))
    monkeypatch.setattr(handlers, "handle_update", lambda w, d, rid, pd: order.append(f"update:{d.get('image')}"))

    handlers.handle_pull_redeploy(ws, {"mode": "force", "image": "repo/app:9"}, "req-1", str(tmp_path))

    assert order == ["update:repo/app:9"]


def test_handle_pull_redeploy_graceful_drains_then_updates(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """graceful（默认）：先 drain 再 handle_update，次序固定。"""
    ws = FakeWebSocket()
    order: list[str] = []
    monkeypatch.setattr(handlers.graceful, "drain", lambda base, timeout=60: order.append(f"drain:{base}:{timeout}"))
    monkeypatch.setattr(handlers, "handle_update", lambda w, d, rid, pd: order.append(f"update:{d.get('image')}"))

    handlers.handle_pull_redeploy(
        ws,
        {"image": "repo/app:9", "healthBaseUrl": "http://192.168.0.30:18029", "shutdownTimeoutSec": 30},
        "req-1",
        str(tmp_path),
    )

    assert order == ["drain:http://192.168.0.30:18029:30", "update:repo/app:9"]


def test_handle_pull_redeploy_graceful_drain_failure_skips_update(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """graceful：drain 失败 → send_error，绝不调 handle_update。"""
    ws = FakeWebSocket()
    update_calls = {"n": 0}
    monkeypatch.setattr(handlers.graceful, "drain", lambda base, timeout=60: (_ for _ in ()).throw(RuntimeError("shutdown 返回 500")))
    monkeypatch.setattr(handlers, "handle_update", lambda w, d, rid, pd: update_calls.__setitem__("n", update_calls["n"] + 1))

    handlers.handle_pull_redeploy(
        ws,
        {"image": "repo/app:9", "healthBaseUrl": "http://192.168.0.30:18029"},
        "req-1",
        str(tmp_path),
    )

    decoded = _decode_messages(ws)
    assert decoded[-1]["status"] == "failed"
    assert "shutdown" in decoded[-1]["error"]
    assert update_calls["n"] == 0


def test_handle_pull_redeploy_graceful_rejects_bad_url_skips_update(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """graceful：healthBaseUrl 非法（drain ValueError）→ send_error，不 update。"""
    ws = FakeWebSocket()
    update_calls = {"n": 0}
    monkeypatch.setattr(handlers.graceful, "drain", lambda base, timeout=60: (_ for _ in ()).throw(ValueError("host 必须是内网 IP，非域名: evil.example.com")))
    monkeypatch.setattr(handlers, "handle_update", lambda w, d, rid, pd: update_calls.__setitem__("n", update_calls["n"] + 1))

    handlers.handle_pull_redeploy(
        ws,
        {"image": "repo/app:9", "healthBaseUrl": "http://evil.example.com:18029"},
        "req-1",
        str(tmp_path),
    )

    decoded = _decode_messages(ws)
    assert decoded[-1]["status"] == "failed"
    assert "内网" in decoded[-1]["error"]
    assert update_calls["n"] == 0


def test_handle_pull_redeploy_graceful_unexpected_drain_error_skips_update(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """graceful：drain 抛非 ValueError/RuntimeError 的异常（兜底分支）→ send_error，不 update。"""
    ws = FakeWebSocket()
    update_calls = {"n": 0}
    monkeypatch.setattr(handlers.graceful, "drain", lambda base, timeout=60: (_ for _ in ()).throw(OSError("socket boom")))
    monkeypatch.setattr(handlers, "handle_update", lambda w, d, rid, pd: update_calls.__setitem__("n", update_calls["n"] + 1))

    handlers.handle_pull_redeploy(
        ws,
        {"image": "repo/app:9", "healthBaseUrl": "http://192.168.0.30:18029"},
        "req-1",
        str(tmp_path),
    )

    decoded = _decode_messages(ws)
    assert decoded[-1]["status"] == "failed"
    assert "socket boom" in decoded[-1]["error"]
    assert update_calls["n"] == 0


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
    """HANDLERS 必须注册 start / stop / force-restart / pull-redeploy（连字符 key）。"""
    assert handlers.HANDLERS["start"] is handlers.handle_start
    assert handlers.HANDLERS["stop"] is handlers.handle_stop
    assert handlers.HANDLERS["force-restart"] is handlers.handle_force_restart
    assert handlers.HANDLERS["pull-redeploy"] is handlers.handle_pull_redeploy


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
