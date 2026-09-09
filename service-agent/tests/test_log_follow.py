import json
import os
from pathlib import Path

import pytest

from core import log_constants, log_follow


class FakeWs:
    def __init__(self, fail_after: int | None = None) -> None:
        self.messages: list[dict] = []
        self.fail_after = fail_after

    def send(self, payload: str) -> None:
        if self.fail_after is not None and len(self.messages) >= self.fail_after:
            raise RuntimeError("socket closed")
        self.messages.append(json.loads(payload))


def _project(tmp_path: Path) -> tuple[Path, Path]:
    proj = tmp_path / "app"
    main = proj / "logs" / "main"
    main.mkdir(parents=True)
    (proj / "docker-compose.yaml").write_text("services: {}\n")
    return proj, main


def _line(i: int, level: str = "info ") -> str:
    return f"2026-09-09 10:00:{i % 60:02d} [{level}] line {i}\n"


def _write(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))  # 按字节写：避免 Windows 文本模式塞 CRLF 让偏移断言失真


def _append(path: Path, text: str) -> None:
    with path.open("ab") as fh:
        fh.write(text.encode("utf-8"))


def _session(ws, proj, **overrides) -> log_follow.FollowSession:
    data = {"sessionId": "s1", "dir": str(proj), "logDir": None, "subdir": "main", "tail": 2, "filter": None}
    data.update(overrides)
    return log_follow.FollowSession(ws, data)


@pytest.fixture(autouse=True)
def _clean_sessions():
    log_follow._sessions.clear()
    yield
    log_follow._sessions.clear()


def test_start_sends_started_and_tail_replay(tmp_path: Path) -> None:
    proj, main = _project(tmp_path)
    f = main / "orchidea_2026-09-09.log"
    _write(f, "".join(_line(i) for i in range(5)))
    ws = FakeWs()
    s = _session(ws, proj)

    assert s.start() is True
    types = [m["type"] for m in ws.messages]
    assert types == ["logfile_started", "logfile_entries"]
    assert ws.messages[0]["file"] == "main/orchidea_2026-09-09.log"
    assert ws.messages[0]["fileSize"] == f.stat().st_size
    replay = ws.messages[1]
    assert replay["seq"] == 1 and [e["text"][-6:] for e in replay["entries"]] == ["line 3", "line 4"]
    assert replay["dropped"] == 0


def test_start_errors_when_subdir_or_file_missing(tmp_path: Path) -> None:
    proj, main = _project(tmp_path)
    ws = FakeWs()
    assert _session(ws, proj, subdir="nope").start() is False
    assert ws.messages[-1]["type"] == "logfile_error" and ws.messages[-1]["error"]["code"] == "not_found"
    ws2 = FakeWs()
    assert _session(ws2, proj).start() is False  # 子目录存在但没有日志文件
    assert ws2.messages[-1]["error"]["code"] == "not_found"
    ws3 = FakeWs()
    assert _session(ws3, proj, filter={"keyword": "(", "regex": True}).start() is False
    assert ws3.messages[-1]["error"]["code"] == "invalid"


def test_tick_reads_growth_keeps_partial_line_and_flushes(tmp_path: Path) -> None:
    proj, main = _project(tmp_path)
    f = main / "orchidea_2026-09-09.log"
    _write(f, _line(0))
    ws = FakeWs()
    s = _session(ws, proj, tail=0)
    assert s.start() is True
    assert [m["type"] for m in ws.messages] == ["logfile_started"]  # tail=0 不回放

    _append(f, _line(1) + "2026-09-09 10:00:02 [warn ] half")  # 最后半行无换行
    s.tick(now=100.0)
    s.tick(now=100.0 + log_constants.FLUSH_SEC + 0.01)  # 到点刷帧
    frame = ws.messages[-1]
    assert frame["type"] == "logfile_entries" and frame["seq"] == 1
    assert [e["text"][-6:] for e in frame["entries"]] == ["line 1"]  # 半行仍在缓冲
    assert frame["startOffset"] == len(_line(0)) and frame["endOffset"] == len(_line(0)) + len(_line(1))

    _append(f, " done\n" + _line(3))
    s.tick(now=101.0)
    s.tick(now=101.0 + log_constants.FLUSH_SEC + 0.01)
    frame2 = ws.messages[-1]
    assert [e["text"] for e in frame2["entries"]][0].endswith("half done")
    assert frame2["seq"] == 2


def test_tick_applies_filter_and_flush_by_count(tmp_path: Path) -> None:
    proj, main = _project(tmp_path)
    f = main / "orchidea_2026-09-09.log"
    _write(f, _line(0))
    ws = FakeWs()
    s = _session(ws, proj, tail=0, filter={"levels": ["error"]})
    assert s.start()
    body = ""
    for i in range(log_constants.FLUSH_ENTRIES * 2):
        body += _line(i, "error") + _line(i, "info ")
    _append(f, body)
    s.tick(now=200.0)  # 攒满 FLUSH_ENTRIES 立即刷，不等 FLUSH_SEC
    frames = [m for m in ws.messages if m["type"] == "logfile_entries"]
    assert len(frames) >= 1
    assert all(e["level"] == "error" for fr in frames for e in fr["entries"])
    assert len(frames[0]["entries"]) == log_constants.FLUSH_ENTRIES


def test_rate_limit_drops_and_reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(log_constants, "RATE_BURST", 10)
    monkeypatch.setattr(log_constants, "RATE_ENTRIES_PER_SEC", 10)
    proj, main = _project(tmp_path)
    f = main / "orchidea_2026-09-09.log"
    _write(f, _line(0))
    ws = FakeWs()
    s = _session(ws, proj, tail=0)
    assert s.start()
    _append(f, "".join(_line(i) for i in range(30)))
    s.tick(now=300.0)
    s.tick(now=300.0 + log_constants.FLUSH_SEC + 0.01)
    frame = [m for m in ws.messages if m["type"] == "logfile_entries"][-1]
    assert len(frame["entries"]) == 10
    assert frame["dropped"] == 20 and s.dropped_total == 20


def test_relist_switches_to_newer_file_and_sends_rotated(tmp_path: Path) -> None:
    proj, main = _project(tmp_path)
    old = main / "orchidea_2026-09-09.log"
    _write(old, _line(0))
    os.utime(old, (1_700_000_000, 1_700_000_000))
    ws = FakeWs()
    s = _session(ws, proj, tail=0)
    assert s.start()
    _append(old, _line(1))
    os.utime(old, (1_700_000_000, 1_700_000_000))  # 追加不改 mtime，模拟旧文件仍旧
    new = main / "orchidea_2026-09-10.log"
    _write(new, _line(50))
    os.utime(new, (1_700_000_100, 1_700_000_100))

    s.tick(now=400.0 + log_constants.RELIST_SEC + 1)  # 触发重列
    types = [m["type"] for m in ws.messages]
    assert "logfile_rotated" in types
    rot = ws.messages[types.index("logfile_rotated")]
    assert rot == {"type": "logfile_rotated", "sessionId": "s1", "from": "main/orchidea_2026-09-09.log", "to": "main/orchidea_2026-09-10.log"}
    entries_frames = [m for m in ws.messages if m["type"] == "logfile_entries"]
    assert [e["text"][-6:] for e in entries_frames[0]["entries"]] == ["line 1"]  # 旧文件先读到尾
    assert entries_frames[0]["file"] == "main/orchidea_2026-09-09.log"
    s.tick(now=401.0 + log_constants.RELIST_SEC + 1 + log_constants.FLUSH_SEC)
    assert [e["text"][-7:] for e in ws.messages[-1]["entries"]] == ["line 50"]  # 新文件从 0 读
    assert ws.messages[-1]["file"] == "main/orchidea_2026-09-10.log"


def test_truncate_in_place_is_treated_as_rotation(tmp_path: Path) -> None:
    proj, main = _project(tmp_path)
    f = main / "orchidea_2026-09-09.log"
    _write(f, "".join(_line(i) for i in range(3)))
    ws = FakeWs()
    s = _session(ws, proj, tail=0)
    assert s.start()
    _write(f, _line(9))  # 变小
    s.tick(now=500.0)
    s.tick(now=500.0 + log_constants.FLUSH_SEC + 0.01)
    types = [m["type"] for m in ws.messages]
    assert "logfile_rotated" in types
    rot = ws.messages[types.index("logfile_rotated")]
    assert rot["from"] == rot["to"] == "main/orchidea_2026-09-09.log"
    assert [e["text"][-6:] for e in ws.messages[-1]["entries"]] == ["line 9"]


def test_file_gone_finishes_session(tmp_path: Path) -> None:
    proj, main = _project(tmp_path)
    f = main / "orchidea_2026-09-09.log"
    _write(f, _line(0))
    ws = FakeWs()
    s = _session(ws, proj, tail=0)
    assert s.start()
    f.unlink()
    s.tick(now=600.0 + log_constants.RELIST_SEC + 1)
    assert ws.messages[-1] == {"type": "logfile_finished", "sessionId": "s1", "reason": "file_gone"}
    assert s.stopped


def test_io_error_sends_error_and_finished(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proj, main = _project(tmp_path)
    f = main / "orchidea_2026-09-09.log"
    _write(f, _line(0))
    ws = FakeWs()
    s = _session(ws, proj, tail=0)
    assert s.start()

    def boom(path):
        raise OSError("disk gone")

    monkeypatch.setattr(log_follow.os.path, "getsize", boom)
    s.tick(now=650.0)
    assert [m["type"] for m in ws.messages[-2:]] == ["logfile_error", "logfile_finished"]
    assert ws.messages[-2]["error"]["code"] == "io_error"
    assert ws.messages[-1]["reason"] == "error" and s.stop_reason == "error"


def test_send_failure_stops_without_finished_frame(tmp_path: Path) -> None:
    proj, main = _project(tmp_path)
    f = main / "orchidea_2026-09-09.log"
    _write(f, _line(0))
    ws = FakeWs(fail_after=1)  # started 成功，之后全失败
    s = _session(ws, proj, tail=0)
    assert s.start()
    _append(f, _line(1))
    s.tick(now=700.0)
    s.tick(now=700.0 + log_constants.FLUSH_SEC + 0.01)
    assert s.stopped
    assert [m["type"] for m in ws.messages] == ["logfile_started"]


def test_tail_value_parsing() -> None:
    ws = FakeWs()
    assert log_follow.FollowSession(ws, {"sessionId": "x", "tail": "abc"}).tail == log_constants.TAIL_DEFAULT
    assert log_follow.FollowSession(ws, {"sessionId": "x", "tail": 99999}).tail == log_constants.TAIL_MAX
    assert log_follow.FollowSession(ws, {"sessionId": "x", "tail": -5}).tail == 0


def test_start_follow_registry_limits_idempotency_and_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proj, main = _project(tmp_path)
    _write(main / "orchidea_2026-09-09.log", _line(0))
    started_threads: list[object] = []

    class FakeThread:
        def __init__(self, target=None, daemon=None, name=None) -> None:
            self.target = target

        def start(self) -> None:
            started_threads.append(self)

    monkeypatch.setattr(log_follow.threading, "Thread", FakeThread)
    ws = FakeWs()
    base = {"dir": str(proj), "subdir": "main", "tail": 0}

    log_follow.start_follow(ws, {"sessionId": "a", **base})
    assert "a" in log_follow._sessions and len(started_threads) == 1
    log_follow.start_follow(ws, {"sessionId": "a", **base})  # 幂等：只重发 started
    assert len(started_threads) == 1 and ws.messages[-1]["type"] == "logfile_started"

    for sid in ("b", "c"):
        log_follow.start_follow(ws, {"sessionId": sid, **base})
    log_follow.start_follow(ws, {"sessionId": "d", **base})
    assert ws.messages[-1] == {
        "type": "logfile_error",
        "sessionId": "d",
        "error": {"code": "busy", "message": "too many follow sessions (max 3)"},
    }

    log_follow.start_follow(ws, {"dir": str(proj)})  # 缺 sessionId：忽略
    assert "" not in log_follow._sessions

    log_follow.start_follow(ws, {"sessionId": "e", "dir": str(proj), "subdir": "nope", "tail": 0})  # start 失败：不登记
    assert "e" not in log_follow._sessions

    log_follow.stop_follow({"sessionId": "b"})
    assert log_follow._sessions["b"].stopped
    log_follow.stop_follow({"sessionId": "zzz"})  # 未知：忽略
    assert log_follow.stop_all() == 3
    assert not log_follow._sessions


def test_run_loop_sends_finished_on_unfollow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proj, main = _project(tmp_path)
    _write(main / "orchidea_2026-09-09.log", _line(0))
    ws = FakeWs()
    s = _session(ws, proj, tail=0)
    log_follow._sessions["s1"] = s
    monkeypatch.setattr(log_follow.time, "sleep", lambda _: s.stop("unfollow"))
    monkeypatch.setattr(log_follow.time, "monotonic", lambda: 1.0)

    s.run()

    assert ws.messages[-1] == {"type": "logfile_finished", "sessionId": "s1", "reason": "unfollow"}
    assert "s1" not in log_follow._sessions
