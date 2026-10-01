"""drain=true 的 restart/update：同一条命令里先下线再重启。

hub 旧做法是 drain、restart 两条命令，drain 成功后 restart 若下发失败（离线/升级中），实例会停在
「已下线未重启」：Nacos 已注销、ready 恒 503，容器却仍 running，巡检不报警，只有重启才恢复。
"""
import json
import subprocess

import pytest

from core import handlers


class FakeWebSocket:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, payload: str) -> None:
        self.messages.append(payload)


def _last_result(ws: FakeWebSocket) -> dict:
    return [json.loads(item) for item in ws.messages][-1]


def _restart_env(monkeypatch, calls, *, port=13099, drain_result=(True, "drained"), restart=(True, "restart ok")):
    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: "compose.yml")
    monkeypatch.setattr(handlers, "resolve_container_port_mapping", lambda compose_file: port)
    monkeypatch.setattr(handlers, "drain", lambda p, token=None: calls.append(("drain", p, token)) or drain_result)

    def run(project_dir, args):
        calls.append(("compose", *args))
        if isinstance(restart, Exception):
            raise restart
        return restart

    monkeypatch.setattr(handlers, "run_compose", run)
    monkeypatch.setattr(handlers, "wait_healthy", lambda p: (True, "healthy"))


def test_restart_with_drain_drains_then_restarts_in_one_command(monkeypatch, tmp_path) -> None:
    ws, calls = FakeWebSocket(), []
    _restart_env(monkeypatch, calls)

    handlers.handle_restart(ws, {"drain": True, "graceful": True, "shutdownToken": "tk"}, "req-d1", str(tmp_path))

    assert calls == [("drain", 13099, "tk"), ("compose", "restart")]
    result = _last_result(ws)
    assert result["status"] == "success"
    assert result["output"].index("=== drain ===") < result["output"].index("=== docker compose restart ===")
    assert "healthcheck" in result["output"]


def test_restart_with_drain_failure_keeps_container_untouched(monkeypatch, tmp_path) -> None:
    ws, calls = FakeWebSocket(), []
    _restart_env(monkeypatch, calls, drain_result=(False, "forbidden: invalid shutdown token"))

    handlers.handle_restart(ws, {"drain": True}, "req-d2", str(tmp_path))

    assert calls == [("drain", 13099, None)]  # 下线失败不重启
    result = _last_result(ws)
    assert result["status"] == "failed"
    assert "forbidden" in result["output"] and "未执行 restart" in result["output"]
    assert handlers.DRAINED_NOT_RESTARTED not in result["output"]


def test_restart_with_drain_without_port_mapping_fails_before_drain(monkeypatch, tmp_path) -> None:
    ws, calls = FakeWebSocket(), []
    _restart_env(monkeypatch, calls, port=None)

    handlers.handle_restart(ws, {"drain": True}, "req-d3", str(tmp_path))

    assert calls == []
    assert _last_result(ws)["status"] == "failed"


@pytest.mark.parametrize(
    "restart",
    [(False, "compose restart failed"), subprocess.TimeoutExpired("docker", 300), RuntimeError("boom")],
)
def test_restart_failure_after_drain_says_instance_is_out_of_traffic(monkeypatch, tmp_path, restart) -> None:
    ws, calls = FakeWebSocket(), []
    _restart_env(monkeypatch, calls, restart=restart)

    handlers.handle_restart(ws, {"drain": True}, "req-d4", str(tmp_path))

    result = _last_result(ws)
    assert result["status"] == "failed"
    assert handlers.DRAINED_NOT_RESTARTED in result["output"]


def test_restart_without_drain_keeps_old_error_shape(monkeypatch, tmp_path) -> None:
    """未带 drain 的超时仍走原来的 error 字段，hub 侧旧展示不变。"""
    ws, calls = FakeWebSocket(), []
    _restart_env(monkeypatch, calls, restart=subprocess.TimeoutExpired("docker", 300))

    handlers.handle_restart(ws, {}, "req-d5", str(tmp_path))

    result = _last_result(ws)
    assert calls == [("compose", "restart")]
    assert result["error"] == "Command execution timed out (5 min)" and "output" not in result


def _update_env(monkeypatch, calls, restored, *, results=None, drain_result=(True, "drained")):
    results = results or {}
    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: "compose.yml")
    monkeypatch.setattr(handlers, "read_compose_file", lambda compose_file: "services: {}\n")
    monkeypatch.setattr(handlers, "update_image_in_compose", lambda *args: ["api"])
    monkeypatch.setattr(handlers, "restore_compose_file", lambda f, content: restored.append(content))
    monkeypatch.setattr(handlers, "resolve_container_port_mapping", lambda compose_file: 13099)
    monkeypatch.setattr(handlers, "drain", lambda p, token=None: calls.append(("drain",)) or drain_result)

    def run(project_dir, args):
        calls.append(tuple(args))
        outcome = results.get(tuple(args), (True, "ok"))
        if callable(outcome):
            outcome = outcome()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(handlers, "run_compose", run)


def test_update_with_drain_pulls_first_then_drains_before_down(monkeypatch, tmp_path) -> None:
    ws, calls, restored = FakeWebSocket(), [], []
    _update_env(monkeypatch, calls, restored)

    handlers.handle_update(ws, {"image": "repo/app:9", "drain": True}, "req-u1", str(tmp_path))

    assert calls == [("pull", "--quiet"), ("drain",), ("down",), ("up", "-d")]
    assert _last_result(ws)["status"] == "success"


def test_update_pull_failure_never_drains(monkeypatch, tmp_path) -> None:
    ws, calls, restored = FakeWebSocket(), [], []
    _update_env(monkeypatch, calls, restored, results={("pull", "--quiet"): (False, "pull denied")})

    handlers.handle_update(ws, {"image": "repo/app:9", "drain": True}, "req-u2", str(tmp_path))

    assert ("drain",) not in calls
    assert restored  # compose 改回原样


def test_update_drain_failure_restores_compose_and_skips_switch(monkeypatch, tmp_path) -> None:
    ws, calls, restored = FakeWebSocket(), [], []
    _update_env(monkeypatch, calls, restored, drain_result=(False, "unexpected status 500"))

    handlers.handle_update(ws, {"image": "repo/app:9", "drain": True}, "req-u3", str(tmp_path))

    assert calls == [("pull", "--quiet"), ("drain",)]
    assert restored == ["services: {}\n"]
    result = _last_result(ws)
    assert result["status"] == "failed" and "未执行 update" in result["output"]


def test_update_down_failure_after_drain_warns_out_of_traffic(monkeypatch, tmp_path) -> None:
    ws, calls, restored = FakeWebSocket(), [], []
    _update_env(monkeypatch, calls, restored, results={("down",): (False, "down failed")})

    handlers.handle_update(ws, {"image": "repo/app:9", "drain": True}, "req-u4", str(tmp_path))

    assert handlers.DRAINED_NOT_RESTARTED in _last_result(ws)["output"]


def test_update_up_failure_recovered_by_recreate_does_not_warn(monkeypatch, tmp_path) -> None:
    """down 已成功、旧版本 up -d 恢复成功 = 容器已重建，下线标记随新容器消失，不该误报。"""
    ws, calls, restored = FakeWebSocket(), [], []
    ups = iter([(False, "new image crashed"), (True, "recovered")])
    _update_env(monkeypatch, calls, restored, results={("up", "-d"): lambda: next(ups)})

    handlers.handle_update(ws, {"image": "repo/app:9", "drain": True}, "req-u5", str(tmp_path))

    result = _last_result(ws)
    assert result["status"] == "failed"
    assert handlers.DRAINED_NOT_RESTARTED not in result["output"]


def test_update_up_and_recovery_failure_after_drain_warns(monkeypatch, tmp_path) -> None:
    ws, calls, restored = FakeWebSocket(), [], []
    _update_env(monkeypatch, calls, restored, results={("up", "-d"): (False, "boom")})

    handlers.handle_update(ws, {"image": "repo/app:9", "drain": True}, "req-u6", str(tmp_path))

    output = _last_result(ws)["output"]
    assert "Recovery failed" in output and handlers.DRAINED_NOT_RESTARTED in output


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired("docker", 300), RuntimeError("socket gone")])
def test_update_exception_after_drain_reports_output_and_warning(monkeypatch, tmp_path, error) -> None:
    ws, calls, restored = FakeWebSocket(), [], []
    _update_env(monkeypatch, calls, restored, results={("down",): error})

    handlers.handle_update(ws, {"image": "repo/app:9", "drain": True}, "req-u7", str(tmp_path))

    result = _last_result(ws)
    assert result["status"] == "failed"
    assert "=== drain ===" in result["output"] and handlers.DRAINED_NOT_RESTARTED in result["output"]
    assert restored


def test_dispatch_passes_drain_flag_through_project_lock(monkeypatch, tmp_path) -> None:
    """经 dispatch 入口（目录锁 + 升级占位）同样生效，与 restart 的跟踪采集兼容。"""
    ws, calls = FakeWebSocket(), []
    _restart_env(monkeypatch, calls)
    monkeypatch.setattr("core.status_reporter.follow_up", lambda project_dir: None)

    handlers.dispatch(ws, {"requestId": "req-d6", "action": "restart", "dir": str(tmp_path), "drain": True})

    assert calls == [("drain", 13099, None), ("compose", "restart")]
    assert _last_result(ws)["status"] == "success"
