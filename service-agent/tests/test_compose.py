import os

# compose 实现 validate_managed_dir 后会 import config，config 缺 WS_URL/AGENT_KEY 会 sys.exit。
# 与 test_handlers.py 同款：导入前先注入测试用默认值。
os.environ.setdefault("WS_URL", "ws://test")
os.environ.setdefault("AGENT_KEY", "test-key")

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


# ─────────────────────────────────────────────
# is_image_registry_allowed — 镜像 registry 白名单（按 registry 边界，绝不裸 startswith）
# ─────────────────────────────────────────────

def test_is_image_registry_allowed_empty_allowlist_passes_everything() -> None:
    """空 allowlist = 不限制：任何镜像都放行。"""
    assert compose.is_image_registry_allowed("evil.com/x:1", []) is True
    assert compose.is_image_registry_allowed("registry.example.com/app:1", []) is True
    assert compose.is_image_registry_allowed("nginx:latest", []) is True


def test_is_image_registry_allowed_matches_registry_host() -> None:
    """白名单内 registry 主机（含 . 或 :）的镜像放行。"""
    allowlist = ["registry.example.com"]
    assert compose.is_image_registry_allowed("registry.example.com/app:1.0", allowlist) is True
    assert compose.is_image_registry_allowed("registry.example.com/team/app:1.0", allowlist) is True


def test_is_image_registry_allowed_matches_registry_host_with_port() -> None:
    """带端口的 registry 主机（首段含 :）按主机精确匹配。"""
    allowlist = ["registry.example.com:5000"]
    assert compose.is_image_registry_allowed("registry.example.com:5000/app:1.0", allowlist) is True
    assert compose.is_image_registry_allowed("registry.example.com:5001/app:1.0", allowlist) is False


def test_is_image_registry_allowed_rejects_non_whitelisted_registry() -> None:
    """非白名单 registry 必须拒绝。"""
    allowlist = ["registry.example.com"]
    assert compose.is_image_registry_allowed("evil.com/x:1", allowlist) is False


def test_is_image_registry_allowed_rejects_suffix_lookalike_boundary() -> None:
    """边界反例（核心安全要点）：registry.example.com.evil 不得被误判为白名单内。

    裸 startswith('registry.example.com') 会误放，必须按 registry 主机精确相等判定。
    """
    allowlist = ["registry.example.com"]
    assert compose.is_image_registry_allowed("registry.example.com.evil/x:1", allowlist) is False
    # 反向：前缀作为子串嵌在别处也不能放行
    assert compose.is_image_registry_allowed("evil-registry.example.com/x:1", allowlist) is False


def test_is_image_registry_allowed_docker_io_default_library() -> None:
    """无 registry 主机分量（首段不含 . : 且非 localhost）→ registry 视为 docker.io。"""
    allowlist = ["docker.io"]
    # 官方库镜像（单段）
    assert compose.is_image_registry_allowed("nginx:latest", allowlist) is True
    # 带命名空间但仍是 docker.io（首段 library 不是 registry 主机）
    assert compose.is_image_registry_allowed("library/nginx:latest", allowlist) is True
    # docker.io 不在白名单时拒绝
    assert compose.is_image_registry_allowed("nginx:latest", ["registry.example.com"]) is False


def test_is_image_registry_allowed_localhost_is_registry_host() -> None:
    """首段等于 localhost 时视为 registry 主机。"""
    assert compose.is_image_registry_allowed("localhost/app:1", ["localhost"]) is True
    assert compose.is_image_registry_allowed("localhost:5000/app:1", ["localhost:5000"]) is True
    assert compose.is_image_registry_allowed("localhost/app:1", ["registry.example.com"]) is False


def test_is_image_registry_allowed_prefix_match_with_boundary() -> None:
    """allowlist 项为完整镜像前缀（带 / 边界或精确相等）时也放行。"""
    allowlist = ["registry.example.com/team"]
    # 带 / 边界：前缀后必须是 / 才算命中
    assert compose.is_image_registry_allowed("registry.example.com/team/app:1", allowlist) is True
    # 精确相等（无 tag 场景）
    assert compose.is_image_registry_allowed("registry.example.com/team", allowlist) is True
    # 边界反例：team-evil 不得被前缀 team 误放
    assert compose.is_image_registry_allowed("registry.example.com/team-evil/app:1", allowlist) is False


# ─────────────────────────────────────────────
# validate_managed_dir — 目录受管根安全闸（纯函数，command 与日志流共用）
# ─────────────────────────────────────────────

def test_validate_managed_dir_allows_dir_inside_root(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """根内、非自身目录 → (True, None)。"""
    root = tmp_path / "managed"
    root.mkdir()
    project = root / "biz"
    project.mkdir()
    monkeypatch.setattr(compose.config, "MANAGED_PROJECTS_ROOT", str(root))
    monkeypatch.setattr(compose.config, "SELF_PROJECT_DIR", str(root / "agent"))

    ok, reason = compose.validate_managed_dir(str(project))

    assert ok is True
    assert reason is None


def test_validate_managed_dir_rejects_dir_outside_root(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """根外 → (False, 含「不在受管目录」)。"""
    root = tmp_path / "managed"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(compose.config, "MANAGED_PROJECTS_ROOT", str(root))
    monkeypatch.setattr(compose.config, "SELF_PROJECT_DIR", "")

    ok, reason = compose.validate_managed_dir(str(outside))

    assert ok is False
    assert "不在受管目录" in reason


def test_validate_managed_dir_rejects_path_traversal_escape(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """`..` 穿越逃逸到根外，realpath 归一后被拒。"""
    root = tmp_path / "managed"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    traversal = os.path.join(str(root), "..", "outside")
    monkeypatch.setattr(compose.config, "MANAGED_PROJECTS_ROOT", str(root))
    monkeypatch.setattr(compose.config, "SELF_PROJECT_DIR", "")

    ok, reason = compose.validate_managed_dir(traversal)

    assert ok is False
    assert "不在受管目录" in reason


def test_validate_managed_dir_rejects_self_project(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """命中 SELF_PROJECT_DIR（含子目录）→ (False, 含「禁止操作 agent 自身」)。"""
    root = tmp_path / "managed"
    root.mkdir()
    self_dir = root / "agent"
    self_dir.mkdir()
    monkeypatch.setattr(compose.config, "MANAGED_PROJECTS_ROOT", str(root))
    monkeypatch.setattr(compose.config, "SELF_PROJECT_DIR", str(self_dir))

    ok, reason = compose.validate_managed_dir(str(self_dir))

    assert ok is False
    assert "禁止操作 agent 自身" in reason


def test_validate_managed_dir_self_empty_skips_self_check(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """SELF_PROJECT_DIR 为空 → 不做自身判定，根内一律放行（与现 _validate_base 等价）。"""
    root = tmp_path / "managed"
    root.mkdir()
    project = root / "anything"
    project.mkdir()
    monkeypatch.setattr(compose.config, "MANAGED_PROJECTS_ROOT", str(root))
    monkeypatch.setattr(compose.config, "SELF_PROJECT_DIR", "")

    ok, reason = compose.validate_managed_dir(str(project))

    assert ok is True
    assert reason is None


def test_validate_managed_dir_root_commonpath_valueerror_rejects(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """根判定的 commonpath 抛 ValueError（如 Windows 跨盘符）→ 从严兜底为「在根外」拒绝。"""
    project = tmp_path / "biz"
    project.mkdir()
    monkeypatch.setattr(compose.config, "MANAGED_PROJECTS_ROOT", str(tmp_path))
    monkeypatch.setattr(compose.config, "SELF_PROJECT_DIR", "")

    def boom(_paths):
        raise ValueError("paths on different drives")

    monkeypatch.setattr(compose.os.path, "commonpath", boom)

    ok, reason = compose.validate_managed_dir(str(project))

    assert ok is False
    assert "不在受管目录" in reason


def test_validate_managed_dir_self_commonpath_valueerror_rejects(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """自身判定的 commonpath 抛 ValueError → 从严兜底为「命中自身」拒绝。"""
    root = tmp_path / "managed"
    root.mkdir()
    project = root / "biz"
    project.mkdir()
    self_dir = root / "agent"
    self_dir.mkdir()
    monkeypatch.setattr(compose.config, "MANAGED_PROJECTS_ROOT", str(root))
    monkeypatch.setattr(compose.config, "SELF_PROJECT_DIR", str(self_dir))

    calls = {"n": 0}

    def commonpath_first_ok_then_boom(paths):
        # 第一次（根判定）放行；第二次（self 判定）抛 ValueError，触发从严兜底。
        calls["n"] += 1
        if calls["n"] == 1:
            return os.path.realpath(str(root))
        raise ValueError("paths on different drives")

    monkeypatch.setattr(compose.os.path, "commonpath", commonpath_first_ok_then_boom)

    ok, reason = compose.validate_managed_dir(str(project))

    assert ok is False
    assert "禁止操作 agent 自身" in reason
