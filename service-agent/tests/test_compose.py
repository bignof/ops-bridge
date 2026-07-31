import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from services import compose


def test_get_compose_cmd_prefers_docker_compose(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(compose.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))

    assert compose.get_compose_cmd() == ["docker", "compose"]


def test_get_compose_cmd_raises_when_v2_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_error(*args, **kwargs):
        raise RuntimeError("docker missing")

    monkeypatch.setattr(compose.subprocess, "run", raise_error)

    with pytest.raises(RuntimeError, match=r"'docker compose' \(v2 plugin\) is required but unavailable"):
        compose.get_compose_cmd()


def test_get_cached_compose_cmd_populates_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    compose._compose_cmd = None
    monkeypatch.setattr(compose, "get_compose_cmd", lambda: ["docker", "compose"])

    assert compose._get_compose_cmd() == ["docker", "compose"]
    assert compose._compose_cmd == ["docker", "compose"]
    compose._compose_cmd = None


def test_find_compose_file_and_update_image(tmp_path: Path) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text(
        yaml.dump(
            {
                "services": {
                    "api": {"image": "repo/app:1.0"},
                    "worker": {"image": "repo/app:2.0"},
                    "skip": "not-a-dict",
                    "other": {"image": "another/image:1"},
                }
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    assert compose.find_compose_file(str(tmp_path)) == str(compose_file)

    updated = compose.update_image_in_compose(str(compose_file), "repo/app:9.9")
    content = yaml.safe_load(compose_file.read_text(encoding="utf-8"))

    assert updated == ["api", "worker"]
    assert content["services"]["api"]["image"] == "repo/app:9.9"
    assert content["services"]["worker"]["image"] == "repo/app:9.9"
    assert content["services"]["other"]["image"] == "another/image:1"


def test_find_compose_file_returns_none_when_absent(tmp_path: Path) -> None:
    assert compose.find_compose_file(str(tmp_path)) is None


def test_read_and_restore_compose_file_round_trip(tmp_path: Path) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    original = "services:\n  api:\n    image: repo/app:1.0\n"
    compose_file.write_text(original, encoding="utf-8")

    snapshot = compose.read_compose_file(str(compose_file))
    compose_file.write_text("services:\n  api:\n    image: repo/app:2.0\n", encoding="utf-8")
    compose.restore_compose_file(str(compose_file), snapshot)

    assert compose_file.read_text(encoding="utf-8") == original


def test_update_image_in_compose_returns_empty_when_no_match(tmp_path: Path) -> None:
    compose_file = tmp_path / "docker-compose.yaml"
    original = {"services": {"api": {"image": "repo/app:1.0"}}}
    compose_file.write_text(yaml.dump(original, allow_unicode=True), encoding="utf-8")

    updated = compose.update_image_in_compose(str(compose_file), "other/app:2.0")

    assert updated == []
    assert yaml.safe_load(compose_file.read_text(encoding="utf-8")) == original


def test_run_compose_uses_cached_command(monkeypatch: pytest.MonkeyPatch) -> None:
    compose._compose_cmd = ["docker", "compose"]
    calls: list[tuple[list[str], str]] = []

    def fake_run(cmd, capture_output, text, timeout, cwd):
        calls.append((cmd, cwd))
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(compose.subprocess, "run", fake_run)

    ok, output = compose.run_compose("/tmp/app", ["restart"])

    assert ok is True
    assert output == "ok"
    assert calls == [(["docker", "compose", "restart"], "/tmp/app")]
    compose._compose_cmd = None


def test_open_compose_process_uses_cached_command(monkeypatch: pytest.MonkeyPatch) -> None:
    compose._compose_cmd = ["docker", "compose"]
    calls: list[tuple[list[str], str]] = []

    def fake_popen(cmd, stdout, stderr, text, cwd, bufsize):
        calls.append((cmd, cwd))
        return SimpleNamespace(stdout=None)

    monkeypatch.setattr(compose.subprocess, "Popen", fake_popen)

    process = compose.open_compose_process("/tmp/app", ["logs", "-f", "--tail", "10", "api"])

    assert process.stdout is None
    assert calls == [(["docker", "compose", "logs", "-f", "--tail", "10", "api"], "/tmp/app")]
    compose._compose_cmd = None


def test_resolve_container_port_mapping_short_syntax(tmp_path: Path) -> None:
    compose_file = tmp_path / "docker-compose.yaml"
    compose_file.write_text(
        yaml.dump(
            {"services": {"app": {"image": "repo/app:1.0", "ports": ["13099:80", "10389:10389"]}}},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    assert compose.resolve_container_port_mapping(str(compose_file)) == 13099


def test_resolve_container_port_mapping_three_part_syntax(tmp_path: Path) -> None:
    compose_file = tmp_path / "docker-compose.yaml"
    compose_file.write_text(
        yaml.dump({"services": {"app": {"ports": ["127.0.0.1:13099:80"]}}}, allow_unicode=True),
        encoding="utf-8",
    )
    assert compose.resolve_container_port_mapping(str(compose_file)) == 13099


def test_resolve_container_port_mapping_tcp_suffix(tmp_path: Path) -> None:
    compose_file = tmp_path / "docker-compose.yaml"
    compose_file.write_text(
        yaml.dump({"services": {"app": {"ports": ["13099:80/tcp"]}}}, allow_unicode=True),
        encoding="utf-8",
    )
    assert compose.resolve_container_port_mapping(str(compose_file)) == 13099


def test_resolve_container_port_mapping_returns_none_when_absent(tmp_path: Path) -> None:
    compose_file = tmp_path / "docker-compose.yaml"
    compose_file.write_text(
        yaml.dump({"services": {"app": {"ports": ["10389:10389"]}}}, allow_unicode=True), encoding="utf-8"
    )
    assert compose.resolve_container_port_mapping(str(compose_file)) is None


def test_resolve_container_port_mapping_returns_none_without_ports(tmp_path: Path) -> None:
    compose_file = tmp_path / "docker-compose.yaml"
    compose_file.write_text(
        yaml.dump({"services": {"app": {"image": "repo/app:1.0"}}}, allow_unicode=True), encoding="utf-8"
    )
    assert compose.resolve_container_port_mapping(str(compose_file)) is None


def test_resolve_container_port_mapping_ignores_random_host_port_entries(tmp_path: Path) -> None:
    """只写容器端口(如 '80')没有宿主机端口——docker 会随机分配，静态解析拿不到，必须跳过而不是误报。"""
    compose_file = tmp_path / "docker-compose.yaml"
    compose_file.write_text(yaml.dump({"services": {"app": {"ports": ["80"]}}}, allow_unicode=True), encoding="utf-8")
    assert compose.resolve_container_port_mapping(str(compose_file)) is None


def test_collect_service_statuses_single_service(monkeypatch: pytest.MonkeyPatch) -> None:
    compose._compose_cmd = ["docker", "compose"]  # 跳过探测调用，否则会被下面的 fake_run 判成意外命令
    ps_line = json.dumps(
        {"ID": "abc123", "Image": "nginx:1", "State": "running", "Name": "app1-test", "Service": "app"}
    )

    def fake_run(cmd, **kwargs):
        if cmd[-3:] == ["ps", "--format", "json"]:
            return SimpleNamespace(returncode=0, stdout=ps_line + "\n", stderr="")
        if cmd[:2] == ["docker", "inspect"]:
            return SimpleNamespace(returncode=0, stdout="2026-07-31T08:20:19.123456Z\n", stderr="")
        raise AssertionError(f"unexpected cmd: {cmd}")

    monkeypatch.setattr(compose.subprocess, "run", fake_run)

    services = compose.collect_service_statuses("/data/app1-test")
    compose._compose_cmd = None

    assert services == [
        {
            "name": "app",
            "image": "nginx:1",
            "state": "running",
            "startedAt": "2026-07-31T08:20:19.123456Z",
            "containerName": "app1-test",
            "containerId": "abc123",
            "raw": json.loads(ps_line),
        }
    ]


def test_collect_service_statuses_multi_service_ndjson(monkeypatch: pytest.MonkeyPatch) -> None:
    """docker compose ps --format json 是一行一个 JSON 对象(NDJSON)，不是 JSON 数组——多 service 逐行输出。"""
    compose._compose_cmd = ["docker", "compose"]  # 跳过探测调用，否则会被下面的 fake_run 判成意外命令
    line1 = json.dumps({"ID": "c1", "Image": "img-a:1", "State": "running", "Name": "svc-a-1", "Service": "a"})
    line2 = json.dumps({"ID": "c2", "Image": "img-b:1", "State": "exited", "Name": "svc-b-1", "Service": "b"})

    def fake_run(cmd, **kwargs):
        if cmd[-3:] == ["ps", "--format", "json"]:
            return SimpleNamespace(returncode=0, stdout=f"{line1}\n{line2}\n", stderr="")
        if cmd[:2] == ["docker", "inspect"]:
            return SimpleNamespace(returncode=0, stdout="2026-01-01T00:00:00Z\n", stderr="")
        raise AssertionError(f"unexpected cmd: {cmd}")

    monkeypatch.setattr(compose.subprocess, "run", fake_run)

    services = compose.collect_service_statuses("/data/multi")
    compose._compose_cmd = None

    assert [s["name"] for s in services] == ["a", "b"]
    assert [s["image"] for s in services] == ["img-a:1", "img-b:1"]


def test_collect_service_statuses_compose_ps_fails_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    compose._compose_cmd = ["docker", "compose"]  # 跳过探测调用，否则会被下面的 fake_run 判成意外命令

    def fake_run(cmd, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="no such directory")

    monkeypatch.setattr(compose.subprocess, "run", fake_run)

    result = compose.collect_service_statuses("/data/missing")
    compose._compose_cmd = None

    assert result == []


def test_collect_service_statuses_inspect_failure_leaves_started_at_none(monkeypatch: pytest.MonkeyPatch) -> None:
    compose._compose_cmd = ["docker", "compose"]  # 跳过探测调用，否则会被下面的 fake_run 判成意外命令
    ps_line = json.dumps(
        {"ID": "abc123", "Image": "nginx:1", "State": "running", "Name": "app1-test", "Service": "app"}
    )

    def fake_run(cmd, **kwargs):
        if cmd[-3:] == ["ps", "--format", "json"]:
            return SimpleNamespace(returncode=0, stdout=ps_line + "\n", stderr="")
        if cmd[:2] == ["docker", "inspect"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="no such container")
        raise AssertionError(f"unexpected cmd: {cmd}")

    monkeypatch.setattr(compose.subprocess, "run", fake_run)

    services = compose.collect_service_statuses("/data/app1-test")
    compose._compose_cmd = None

    assert services[0]["startedAt"] is None
    assert services[0]["image"] == "nginx:1"  # 其它字段不受 inspect 失败影响
