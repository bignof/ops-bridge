from pathlib import Path

import pytest

from core import log_entries


CONSOLE = "2026-09-09 10:23:44 [error] ERP sync failed: read ECONNRESET handler=push module=erp-sync"
STACK1 = "Error: read ECONNRESET"
STACK2 = "    at TCP.onStreamRead (node:internal/stream_base_commons:217:20)"
INFO = "2026-09-09 10:23:45 [info ] request completed method=GET"


def test_strip_ansi_and_parse_header() -> None:
    colored = "\x1b[32m2026-09-09 10:23:45 [info ] hi\x1b[39m k=v"
    assert log_entries.strip_ansi(colored) == "2026-09-09 10:23:45 [info ] hi k=v"
    assert log_entries.parse_header(INFO) == ("2026-09-09 10:23:45", "info")
    assert log_entries.parse_header(CONSOLE) == ("2026-09-09 10:23:44", "error")
    assert log_entries.parse_header(STACK1) is None
    assert log_entries.parse_header('{"level":"WARN","timestamp":"2026-09-09 10:23:46","message":"x"}') == (
        "2026-09-09 10:23:46",
        "warn",
    )
    assert log_entries.parse_header('{"message":"no level"}') is None
    assert log_entries.parse_header("{not json") is None


def test_assembler_groups_continuation_lines_and_tracks_offsets() -> None:
    asm = log_entries.EntryAssembler(100)
    assert asm.feed(CONSOLE, len(CONSOLE) + 1) is None
    assert asm.feed(STACK1, len(STACK1) + 1) is None
    assert asm.feed(STACK2, len(STACK2) + 1) is None
    done = asm.feed(INFO, len(INFO) + 1)
    assert done == {
        "offset": 100,
        "text": CONSOLE + "\n" + STACK1 + "\n" + STACK2,
        "timestamp": "2026-09-09 10:23:44",
        "level": "error",
    }
    last = asm.flush()
    assert last["offset"] == 100 + len(CONSOLE) + 1 + len(STACK1) + 1 + len(STACK2) + 1
    assert last["level"] == "info"
    assert asm.flush() is None


def test_assembler_leading_continuation_becomes_its_own_entry() -> None:
    asm = log_entries.EntryAssembler(0)
    assert asm.feed(STACK2, len(STACK2) + 1) is None
    done = asm.feed(INFO, len(INFO) + 1)
    assert done == {"offset": 0, "text": STACK2, "timestamp": None, "level": None}


def test_assembler_strips_trailing_cr() -> None:
    asm = log_entries.EntryAssembler(0)
    asm.feed(INFO + "\r", len(INFO) + 2)
    entry = asm.flush()
    assert entry["text"] == INFO
    assert asm.offset == len(INFO) + 2  # 偏移仍按真实字节数走


def test_assemble_bytes_handles_missing_trailing_newline_and_utf8() -> None:
    data = ("2026-09-09 10:23:45 [info ] 中文 k=v\n" + "2026-09-09 10:23:46 [warn ] tail").encode("utf-8")
    entries = log_entries.assemble_bytes(data, 10)
    assert [e["level"] for e in entries] == ["info", "warn"]
    assert entries[1]["offset"] == 10 + len("2026-09-09 10:23:45 [info ] 中文 k=v\n".encode("utf-8"))
    assert log_entries.assemble_bytes(b"", 0) == []


def _entry(text: str, ts: str | None, level: str | None, offset: int = 0) -> dict:
    return {"offset": offset, "text": text, "timestamp": ts, "level": level}


def test_filter_levels_keyword_regex_and_invalid() -> None:
    f = log_entries.EntryFilter({"levels": ["error", "WARN"]})
    assert f.matches(_entry("x", "t", "error"))
    assert f.matches(_entry("x", "t", "warn"))
    assert not f.matches(_entry("x", "t", "info"))
    assert not f.matches(_entry("x", None, None))  # 无级别视为 info

    kw = log_entries.EntryFilter({"keyword": "econnreset"})
    assert kw.matches(_entry("Error: read ECONNRESET", None, None))
    assert not kw.matches(_entry("fine", None, None))

    rx = log_entries.EntryFilter({"keyword": "task(Id)?=98\\d+", "regex": True})
    assert rx.matches(_entry("taskId=9871 attempt=1", "t", "warn"))
    assert not rx.matches(_entry("taskId=1234", "t", "warn"))

    with pytest.raises(ValueError):
        log_entries.EntryFilter({"keyword": "(", "regex": True})
    assert log_entries.EntryFilter(None).matches(_entry("anything", None, None))


def test_filter_time_range_follows_previous_for_untimed_entries() -> None:
    f = log_entries.EntryFilter({"since": "2026-09-09 09:00:00", "until": "2026-09-09 10:00:00"})
    assert not f.matches(_entry("a", "2026-09-09 08:59:59", "info"))
    assert not f.matches(_entry("stack", None, None))  # 跟随上一条：不在窗口内
    assert f.matches(_entry("b", "2026-09-09 09:00:00", "info"))  # 同秒包含
    assert f.matches(_entry("stack", None, None))  # 跟随上一条：在窗口内
    assert f.matches(_entry("c", "2026-09-09 10:00:00", "info"))
    assert not f.matches(_entry("d", "2026-09-09 10:00:01", "info"))


def test_read_tail_entries_respects_lookback_and_filter(tmp_path: Path) -> None:
    p = tmp_path / "a.log"
    lines = [f"2026-09-09 10:00:{i:02d} [{'error' if i % 2 else 'info '}] line {i}" for i in range(20)]
    p.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))  # 按字节写，避免 Windows 文本模式塞 CRLF
    size = p.stat().st_size

    entries, got_size = log_entries.read_tail_entries(str(p), 3, log_entries.EntryFilter({"levels": ["error"]}), 10**6)
    assert got_size == size
    assert [e["text"][-7:] for e in entries] == ["line 15", "line 17", "line 19"]

    # lookback 只覆盖最后两行左右：第一段被丢弃到首个换行之后，不会产生半截条目
    line_len = len(lines[-1].encode("utf-8")) + 1  # line 10+ 比 line 0-9 多一个字节，按末行算
    tail_entries, _ = log_entries.read_tail_entries(str(p), 10, log_entries.EntryFilter(None), line_len * 2 + 5)
    assert [e["text"][-7:] for e in tail_entries] == ["line 18", "line 19"]
    assert tail_entries[0]["offset"] == size - line_len * 2

    empty, _ = log_entries.read_tail_entries(str(p), 5, log_entries.EntryFilter(None), 3)  # 窗口内没有整行
    assert empty == []
