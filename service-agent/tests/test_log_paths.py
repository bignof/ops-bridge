import json
import os
from datetime import datetime
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


def test_format_mtime_carries_timezone_offset() -> None:
    """必须带时区偏移，且任何时区的解析方都能还原成同一绝对时刻。

    回归：曾用 time.localtime() 输出无时区标记的串。agent 容器多为 UTC、中枢与业务容器多为 CST，
    中枢 new Date() 按自己的时区解读，导致界面上「快照时间」与日志正文时间整体差 8 小时。
    """
    s = log_paths.format_mtime(1_700_000_100)
    assert s == "2023-11-15T06:15:00+08:00"
    assert datetime.fromisoformat(s).timestamp() == 1_700_000_100
    assert s[-6] in "+-"  # 结尾是 ±HH:MM 偏移，不是裸时间


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

    monkeypatch.setattr(log_paths, "list_log_overview", boom)
    ws = FakeWs()
    log_paths.handle_list(ws, {"requestId": "r3", "dir": str(proj)})
    assert json.loads(ws.messages[-1])["error"]["code"] == "io_error"


def _many(dirpath: Path, prefix: str, n: int, base_ts: int) -> None:
    dirpath.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        f = dirpath / f"{prefix}-{i:04d}.log"
        f.write_text("x\n")
        os.utime(f, (base_ts + i, base_ts + i))


def test_overview_gives_every_dir_a_share(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """xxl-job 一次任务一个文件、永远最新；全局取最新 N 个会把 main/ 挤出清单（2026-09-11 现场）。"""
    monkeypatch.setattr(log_paths, "LIST_PAGE_SIZE", 5)
    monkeypatch.setattr(log_paths, "LIST_MAX_FILES", 8)
    proj = _project(tmp_path)
    root = proj / "logs"
    _many(root / "main", "orchidea", 3, 1_700_000_000)      # 老的按天日志
    _many(root / "xxljob", "job", 30, 1_700_100_000)        # 更新、更多

    files, dirs = log_paths.list_log_overview(str(root))

    assert [(d["dir"], d["total"], len(d["files"])) for d in dirs] == [("main", 3, 3), ("xxljob", 30, 5)]
    assert dirs[1]["files"][0]["path"] == "xxljob/job-0029.log"  # 组内 mtime 倒序
    paths = [f["path"] for f in files]
    assert sum(p.startswith("main/") for p in paths) == 3        # main 一个都没被挤掉
    assert len(paths) == 8


def test_overview_root_level_files_use_empty_dir(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    root = proj / "logs"
    root.mkdir()
    (root / "app.log").write_text("x\n")
    (root / "empty").mkdir()                                    # 没有日志的目录不出现
    _, dirs = log_paths.list_log_overview(str(root))
    assert [d["dir"] for d in dirs] == [""]


def test_list_log_page_slices_one_dir(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    root = proj / "logs"
    _many(root / "xxljob", "job", 30, 1_700_100_000)
    _many(root / "xxljob" / "sub", "deep", 2, 1_800_000_000)   # 子目录不算进本目录

    page = log_paths.list_log_page(str(root), "xxljob/", 5, 5)
    assert page["dir"] == "xxljob" and page["total"] == 30 and page["offset"] == 5
    assert [f["path"] for f in page["files"]] == [f"xxljob/job-{i:04d}.log" for i in (24, 23, 22, 21, 20)]
    assert log_paths.list_log_page(str(root), "xxljob", 100, 5)["files"] == []

    with pytest.raises(log_paths.LogPathError) as ei:
        log_paths.list_log_page(str(root), "../x", 0, 5)
    assert ei.value.code == "forbidden"
    with pytest.raises(log_paths.LogPathError) as ei2:
        log_paths.list_log_page(str(root), "nope", 0, 5)
    assert ei2.value.code == "not_found"


def test_handle_list_overview_and_page_frames(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    root = proj / "logs"
    _many(root / "main", "orchidea", 3, 1_700_000_000)
    _many(root / "xxljob", "job", 30, 1_700_100_000)
    ws = FakeWs()

    log_paths.handle_list(ws, {"requestId": "o1", "dir": str(proj)})
    ov = json.loads(ws.messages[-1])
    assert [d["dir"] for d in ov["dirs"]] == ["main", "xxljob"] and ov["dirs"][1]["total"] == 30
    assert "total" not in ov  # 概览帧不带单目录字段

    log_paths.handle_list(ws, {"requestId": "p1", "dir": str(proj), "subdir": "xxljob", "offset": 20, "limit": 20})
    pg = json.loads(ws.messages[-1])
    assert pg["dir"] == "xxljob" and pg["total"] == 30 and pg["offset"] == 20 and len(pg["files"]) == 10
    assert "dirs" not in pg

    # 非法 / 越界参数回落：offset 非数字按 0，limit 超上限按上限、非数字按默认
    log_paths.handle_list(ws, {"requestId": "p2", "dir": str(proj), "subdir": "xxljob", "offset": "x", "limit": 10**6})
    assert len(json.loads(ws.messages[-1])["files"]) == 30
    log_paths.handle_list(ws, {"requestId": "p3", "dir": str(proj), "subdir": "xxljob", "limit": "?"})
    p3 = json.loads(ws.messages[-1])
    assert p3["offset"] == 0 and len(p3["files"]) == log_paths.LIST_PAGE_SIZE

    log_paths.handle_list(ws, {"requestId": "p4", "dir": str(proj), "subdir": "../../etc"})
    assert json.loads(ws.messages[-1])["error"]["code"] == "forbidden"
