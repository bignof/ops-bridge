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
