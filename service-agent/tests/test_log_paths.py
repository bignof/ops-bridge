import json
import os
from pathlib import Path

import pytest

from core import log_paths


def _project(tmp_path: Path, name: str = "app") -> Path:
    d = tmp_path / name
    d.mkdir()
    (d / "docker-compose.yaml").write_text("services: {}\n", encoding="utf-8")
    return d


def test_resolve_log_root_defaults_to_logs_and_requires_compose(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    (proj / "logs").mkdir()
    assert log_paths.resolve_log_root(str(proj), None) == os.path.realpath(str(proj / "logs"))
    assert log_paths.resolve_log_root(str(proj), "  ") == os.path.realpath(str(proj / "logs"))

    bare = tmp_path / "bare"
    bare.mkdir()
    with pytest.raises(log_paths.LogPathError) as ei:
        log_paths.resolve_log_root(str(bare), None)
    assert ei.value.code == "not_found"
    with pytest.raises(log_paths.LogPathError) as ei2:
        log_paths.resolve_log_root(str(tmp_path / "missing"), None)
    assert ei2.value.code == "not_found"


def test_resolve_log_root_rejects_escape_and_absolute(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    (tmp_path / "outside").mkdir()
    with pytest.raises(log_paths.LogPathError) as ei:
        log_paths.resolve_log_root(str(proj), "../outside")
    assert ei.value.code == "forbidden"
    with pytest.raises(log_paths.LogPathError) as ei2:
        log_paths.resolve_log_root(str(proj), "/etc")
    assert ei2.value.code == "forbidden"


def test_resolve_log_root_rejects_symlink_escape(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        os.symlink(str(outside), str(proj / "logs"))
    except (OSError, NotImplementedError):
        pytest.skip("平台不支持软链")
    with pytest.raises(log_paths.LogPathError) as ei:
        log_paths.resolve_log_root(str(proj), "logs")
    assert ei.value.code == "forbidden"


def test_resolve_log_root_missing_logs_dir_is_not_found(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    with pytest.raises(log_paths.LogPathError) as ei:
        log_paths.resolve_log_root(str(proj), "logs")
    assert ei.value.code == "not_found"


def test_is_log_filename_whitelist() -> None:
    assert log_paths.is_log_filename("orchidea_2026-09-09.log")
    assert log_paths.is_log_filename("orchidea_2026-09-09.log.3")
    assert not log_paths.is_log_filename(".a1b2-audit.json")
    assert not log_paths.is_log_filename("orchidea_2026-09-09.log.gz")
    assert not log_paths.is_log_filename("notes.txt")


def test_list_log_files_depth_sort_and_whitelist(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    root = proj / "logs"
    (root / "main").mkdir(parents=True)
    (root / "workflows" / "deep" / "deeper").mkdir(parents=True)
    old = root / "main" / "orchidea_2026-09-08.log"
    new = root / "main" / "orchidea_2026-09-09.log"
    old.write_text("a\n")
    new.write_text("bb\n")
    (root / "main" / ".x-audit.json").write_text("{}")
    (root / "workflows" / "2026-09-09.log").write_text("w\n")
    (root / "workflows" / "deep" / "deep.log").write_text("d\n")          # 深度 2：保留
    (root / "workflows" / "deep" / "deeper" / "x.log").write_text("x\n")  # 深度 3：裁掉
    os.utime(old, (1_700_000_000, 1_700_000_000))
    os.utime(new, (1_700_000_100, 1_700_000_100))
    os.utime(root / "workflows" / "2026-09-09.log", (1_700_000_050, 1_700_000_050))
    os.utime(root / "workflows" / "deep" / "deep.log", (1_700_000_010, 1_700_000_010))

    files = log_paths.list_log_files(str(root))

    assert [f["path"] for f in files] == [
        "main/orchidea_2026-09-09.log",
        "workflows/2026-09-09.log",
        "workflows/deep/deep.log",
        "main/orchidea_2026-09-08.log",
    ]
    assert files[0]["size"] == new.stat().st_size  # Windows text mode writes CRLF; compare real byte size
    assert files[0]["mtime"] == log_paths.format_mtime(1_700_000_100)


def test_resolve_subdir_and_file(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    root = proj / "logs" / "main"
    root.mkdir(parents=True)
    f = root / "orchidea_2026-09-09.log"
    f.write_text("x\n")
    logroot = str(proj / "logs")
    assert log_paths.resolve_subdir(logroot, "main") == os.path.realpath(str(root))
    assert log_paths.resolve_subdir(logroot, "") == os.path.realpath(logroot)
    assert log_paths.resolve_log_file(logroot, "main/orchidea_2026-09-09.log") == os.path.realpath(str(f))
    with pytest.raises(log_paths.LogPathError) as ei:
        log_paths.resolve_log_file(logroot, "../docker-compose.yaml")
    assert ei.value.code == "forbidden"
    with pytest.raises(log_paths.LogPathError) as ei2:
        log_paths.resolve_log_file(logroot, "main/nope.log")
    assert ei2.value.code == "not_found"
    with pytest.raises(log_paths.LogPathError) as ei3:
        log_paths.resolve_subdir(logroot, "nope")
    assert ei3.value.code == "not_found"


def test_newest_log_file(tmp_path: Path) -> None:
    d = tmp_path / "main"
    d.mkdir()
    assert log_paths.newest_log_file(str(d)) is None
    a = d / "orchidea_2026-09-08.log"
    b = d / "orchidea_2026-09-09.log"
    a.write_text("a")
    b.write_text("b")
    (d / "audit.json").write_text("{}")
    os.utime(a, (1_700_000_000, 1_700_000_000))
    os.utime(b, (1_700_000_100, 1_700_000_100))
    assert log_paths.newest_log_file(str(d)) == str(b)


class FakeWs:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, payload: str) -> None:
        self.messages.append(payload)


def test_handle_list_success_and_errors(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    (proj / "logs" / "main").mkdir(parents=True)
    (proj / "logs" / "main" / "orchidea_2026-09-09.log").write_text("x\n")
    ws = FakeWs()

    log_paths.handle_list(ws, {"requestId": "r1", "dir": str(proj), "logDir": None})
    ok = json.loads(ws.messages[-1])
    assert ok["type"] == "logfile_list_result" and ok["requestId"] == "r1"
    assert ok["root"] == os.path.realpath(str(proj / "logs"))
    assert [f["path"] for f in ok["files"]] == ["main/orchidea_2026-09-09.log"]
    assert "error" not in ok

    log_paths.handle_list(ws, {"requestId": "r2", "dir": str(tmp_path / "nope")})
    err = json.loads(ws.messages[-1])
    assert err["error"]["code"] == "not_found" and err["files"] == []

    log_paths.handle_list(ws, {"dir": str(proj)})  # 缺 requestId：静默忽略
    assert len(ws.messages) == 2


def test_handle_list_io_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    proj = _project(tmp_path)
    (proj / "logs").mkdir()

    def boom(root):
        raise OSError("disk")

    monkeypatch.setattr(log_paths, "list_log_files", boom)
    ws = FakeWs()
    log_paths.handle_list(ws, {"requestId": "r3", "dir": str(proj)})
    assert json.loads(ws.messages[-1])["error"]["code"] == "io_error"
