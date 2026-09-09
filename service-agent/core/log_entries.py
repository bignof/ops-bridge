"""
log_entries.py — 日志「条目」切分与过滤（spec §3.2.2）。

条目 = 一个带时间戳的首行 + 其后不带时间戳的续行（堆栈）。首行两种：
① console 行 `YYYY-MM-DD HH:MM:SS [level]`；② JSON 行（含 level 与 timestamp）。
每行先剥 ANSI 再识别、再匹配。与 hub 侧 services/log-entries.ts 口径一致，改一处须改两处。
"""
from __future__ import annotations

import json
import os
import re

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
HEADER_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(\w+)\s*\]")


def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


def parse_header(line: str) -> tuple[str, str] | None:
    m = HEADER_RE.match(line)
    if m:
        return m.group(1), m.group(2).lower()
    if line.startswith("{"):
        try:
            obj = json.loads(line)
        except ValueError:
            return None
        if isinstance(obj, dict) and "level" in obj and "timestamp" in obj:
            return str(obj["timestamp"]), str(obj["level"]).lower()
    return None


class EntryAssembler:
    """逐行喂入，按首行切条目；offset 是条目首字节在文件里的偏移。"""

    def __init__(self, start_offset: int = 0) -> None:
        self.offset = start_offset
        self._cur: dict | None = None

    def feed(self, raw_line: str, byte_len: int) -> dict | None:
        line = strip_ansi(raw_line.rstrip("\r"))
        header = parse_header(line)
        done: dict | None = None
        if header is not None or self._cur is None:
            done = self._cur
            ts, level = header if header is not None else (None, None)
            self._cur = {"offset": self.offset, "text": line, "timestamp": ts, "level": level}
        else:
            self._cur["text"] += "\n" + line
        self.offset += byte_len
        return done

    def flush(self) -> dict | None:
        cur, self._cur = self._cur, None
        return cur


class EntryFilter:
    """levels / keyword(+regex) / since / until。时间过滤对无时间戳条目沿用上一条判定（堆栈跟随首行）。"""

    def __init__(self, spec: dict | None) -> None:
        spec = spec or {}
        levels = spec.get("levels") or []
        self.levels = {str(l).lower() for l in levels} if levels else None
        keyword = spec.get("keyword") or ""
        self.pattern: re.Pattern[str] | None = None
        if keyword:
            try:
                self.pattern = re.compile(keyword if spec.get("regex") else re.escape(keyword), re.IGNORECASE)
            except re.error as exc:
                raise ValueError(f"invalid regex: {exc}") from exc
        self.since = spec.get("since") or None
        self.until = spec.get("until") or None
        self._last_time_ok = True

    def matches(self, entry: dict) -> bool:
        level = entry.get("level") or "info"
        if self.levels is not None and level not in self.levels:
            return False
        if self.since or self.until:
            ts = entry.get("timestamp")
            if ts is not None:
                self._last_time_ok = (not self.since or ts >= self.since) and (not self.until or ts <= self.until)
            if not self._last_time_ok:
                return False
        if self.pattern is not None and not self.pattern.search(entry["text"]):
            return False
        return True


def assemble_bytes(data: bytes, start_offset: int) -> list[dict]:
    """把一段字节按行切成条目；最后一段没有换行也算一行（正在写入的半行由调用方自行留缓冲）。"""
    asm = EntryAssembler(start_offset)
    out: list[dict] = []
    pos = 0
    n = len(data)
    while pos < n:
        nl = data.find(b"\n", pos)
        end = n if nl < 0 else nl
        raw = data[pos:end].decode("utf-8", errors="replace")
        byte_len = (end - pos) + (0 if nl < 0 else 1)
        done = asm.feed(raw, byte_len)
        if done is not None:
            out.append(done)
        pos = end + 1
    last = asm.flush()
    if last is not None:
        out.append(last)
    return out


def read_tail_entries(path: str, n: int, flt: EntryFilter, lookback: int) -> tuple[list[dict], int]:
    """从文件尾向前最多读 lookback 字节，切条目、过滤，返回最后 n 条与文件当前大小。"""
    size = os.path.getsize(path)
    start = max(0, size - lookback)
    with open(path, "rb") as fh:
        fh.seek(start)
        data = fh.read()
    if start > 0:
        nl = data.find(b"\n")
        if nl < 0:
            return [], size
        data = data[nl + 1 :]
        start += nl + 1
    matched = [e for e in assemble_bytes(data, start) if flt.matches(e)]
    return matched[-n:] if n > 0 else [], size
