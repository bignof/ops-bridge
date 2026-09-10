"""
log_follow.py — 实时跟随会话（spec §3.2.5）。

纯 Python 轮询文件增长，不依赖 tail：每 FOLLOW_POLL_SEC stat 一次当前文件；每 RELIST_SEC 重列子目录，
出现更新的文件先把旧文件读到尾再切过去并发 logfile_rotated；体积变小视同原地截断。
条目经 EntryFilter 过滤、令牌桶限速（超出直接丢并在下一帧 dropped 报数）、按时间/条数/字节合批。
send 失败即视为 hub 已断，静默退出（不发 finished）。
"""
from __future__ import annotations

import logging
import os
import threading
import time

from core import log_constants
from core.handlers import send_message
from core.log_entries import EntryAssembler, EntryFilter, assemble_bytes, read_tail_entries
from core.log_paths import LogPathError, error_payload, newest_log_file, resolve_log_root, resolve_subdir

logger = logging.getLogger(__name__)

_sessions: dict[str, "FollowSession"] = {}
_sessions_guard = threading.Lock()


class _TokenBucket:
    def __init__(self, rate: float, burst: float, now: float) -> None:
        self.rate, self.burst, self.tokens, self.last = rate, burst, burst, now

    def take(self, now: float) -> bool:
        if now > self.last:
            self.tokens = min(self.burst, self.tokens + (now - self.last) * self.rate)
            self.last = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


class FollowSession:
    def __init__(self, ws, data: dict) -> None:
        self.ws = ws
        self.session_id = str(data.get("sessionId") or "").strip()
        self.project_dir = data.get("dir")
        self.log_dir = data.get("logDir")
        self.subdir = data.get("subdir") or ""
        tail = data.get("tail", log_constants.TAIL_DEFAULT)
        try:
            self.tail = max(0, min(int(tail), log_constants.TAIL_MAX))
        except (TypeError, ValueError):
            self.tail = log_constants.TAIL_DEFAULT
        self.filter_spec = data.get("filter") or None
        self.stopped = False
        self.stop_reason: str | None = None
        self.seq = 0
        self.dropped_total = 0
        self._root = ""
        self._subdir_path = ""
        self._file = ""
        self._offset = 0
        self._buffer = b""
        self._pending: list[dict] = []
        self._pending_bytes = 0
        self._dropped_since_flush = 0
        self._last_flush = 0.0
        self._last_relist = 0.0
        self._flt: EntryFilter | None = None
        self._bucket: _TokenBucket | None = None

    # ── 帧 ────────────────────────────────────────────────────────────────
    def _rel(self, path: str) -> str:
        return os.path.relpath(path, self._root).replace(os.sep, "/")

    def _send(self, frame: dict) -> bool:
        frame["sessionId"] = self.session_id
        if not send_message(self.ws, frame):
            self.stopped = True
            self.stop_reason = "send_failed"
            return False
        return True

    def send_started(self) -> bool:
        return self._send({"type": "logfile_started", "file": self._rel(self._file), "fileSize": os.path.getsize(self._file)})

    def _error(self, code: str, message: str) -> None:
        self._send({"type": "logfile_error", "error": {"code": code, "message": message}})

    # ── 生命周期 ──────────────────────────────────────────────────────────
    def start(self) -> bool:
        """解析路径、编译过滤器、发 started、起手回放。失败已回 error 帧并返回 False。"""
        try:
            self._flt = EntryFilter(self.filter_spec)
        except ValueError as exc:
            self._error("invalid", str(exc))
            return False
        try:
            self._root = resolve_log_root(self.project_dir, self.log_dir)
            self._subdir_path = resolve_subdir(self._root, self.subdir)
        except LogPathError as exc:
            err = error_payload(exc)
            self._error(err["code"], err["message"])
            return False
        newest = newest_log_file(self._subdir_path)
        if newest is None:
            self._error("not_found", f"No log file in subdir: {self.subdir or '.'}")
            return False
        self._file = newest
        # 时钟基准取 0：首轮 tick 立即刷帧 / 重列一次（无副作用），测试可用合成 now 驱动，不依赖真实 monotonic
        self._bucket = _TokenBucket(log_constants.RATE_ENTRIES_PER_SEC, log_constants.RATE_BURST, 0.0)
        self._last_flush = self._last_relist = 0.0
        if not self.send_started():
            return False
        entries, size = read_tail_entries(self._file, self.tail, self._flt, log_constants.TAIL_LOOKBACK_BYTES)
        self._offset = size
        if entries:
            self._emit(entries, self._rel(self._file))
        return not self.stopped

    def stop(self, reason: str) -> None:
        self.stopped = True
        self.stop_reason = reason

    # ── 一轮 ──────────────────────────────────────────────────────────────
    def tick(self, now: float) -> None:
        if self.stopped:
            return
        try:
            self._poll(now)
            if now - self._last_relist >= log_constants.RELIST_SEC:
                self._last_relist = now
                self._relist(now)
            if self._pending and now - self._last_flush >= log_constants.FLUSH_SEC:
                self._flush(now)
        except OSError as exc:
            logger.warning("follow session %s io error: %s", self.session_id, exc)
            self._flush(now)
            self._error("io_error", str(exc)[:200])
            self._send({"type": "logfile_finished", "reason": "error"})
            self.stop("error")

    def _poll(self, now: float) -> None:
        if not os.path.isfile(self._file):
            return  # 交给 _relist 决定：切文件或 file_gone
        size = os.path.getsize(self._file)
        if size < self._offset:
            self._drain(now)
            self._send({"type": "logfile_rotated", "from": self._rel(self._file), "to": self._rel(self._file)})
            self._offset, self._buffer = 0, b""
            size = os.path.getsize(self._file)
        if size > self._offset:
            # 单轮读入封顶：容器一次 flush 出几百 MB（超大 JSON、误打的二进制）时，
            # 原来会把整段增量一次性读进内存，8 路会话一起撞上足以把 agent 撑到 OOM，
            # 而 agent 一死，该机所有服务的日志会话同时断。剩余部分下一轮（0.25s 后）接着读，
            # 不丢数据——文件还在，offset 会追上去。
            want = min(size - self._offset, log_constants.FOLLOW_READ_MAX_BYTES)
            with open(self._file, "rb") as fh:
                fh.seek(self._offset)
                data = fh.read(want)
            self._consume(data, now)

    def _consume(self, data: bytes, now: float) -> None:
        start_offset = self._offset - len(self._buffer)
        data = self._buffer + data
        self._offset += len(data) - len(self._buffer)
        nl = data.rfind(b"\n")
        if nl >= 0:
            complete, data = data[: nl + 1], data[nl + 1 :]
            for entry in assemble_bytes(complete, start_offset):
                self._admit(entry, now)
            start_offset += len(complete)
        # 剩下的是还没写完的半行，长度不受任何约束——应用把超大 JSON 或二进制一次性写出来时，
        # 它会一直攒在缓冲里直到出现下一个换行符。超限就强制断行发出去：
        # 一行被拆成几条只是显示上难看，把 agent 拖到 OOM 则是全机日志一起断。
        while len(data) > log_constants.FOLLOW_LINE_MAX_BYTES:
            chunk, data = data[: log_constants.FOLLOW_LINE_MAX_BYTES], data[log_constants.FOLLOW_LINE_MAX_BYTES :]
            for entry in assemble_bytes(chunk, start_offset):
                self._admit(entry, now)
            start_offset += len(chunk)
        self._buffer = data

    def _admit(self, entry: dict, now: float) -> None:
        assert self._flt is not None and self._bucket is not None
        if not self._flt.matches(entry):
            return
        if not self._bucket.take(now):
            self._dropped_since_flush += 1
            self.dropped_total += 1
            return
        self._pending.append(entry)
        self._pending_bytes += len(entry["text"])
        if len(self._pending) >= log_constants.FLUSH_ENTRIES or self._pending_bytes >= log_constants.FLUSH_BYTES:
            self._flush(now)

    def _drain(self, now: float) -> None:
        """把当前文件读到尾（含缓冲区里的半行）并刷帧——切文件前调用。"""
        if os.path.isfile(self._file):
            size = os.path.getsize(self._file)
            if size > self._offset:
                with open(self._file, "rb") as fh:
                    fh.seek(self._offset)
                    self._consume(fh.read(size - self._offset), now)
        if self._buffer:
            for entry in assemble_bytes(self._buffer, self._offset - len(self._buffer)):
                self._admit(entry, now)
            self._buffer = b""
        self._flush(now)

    def _relist(self, now: float) -> None:
        newest = newest_log_file(self._subdir_path) if os.path.isdir(self._subdir_path) else None
        if newest is None:
            self._flush(now)
            self._send({"type": "logfile_finished", "reason": "file_gone"})
            self.stop("file_gone")
            return
        if os.path.realpath(newest) != os.path.realpath(self._file):
            old_rel = self._rel(self._file)
            self._drain(now)
            self._send({"type": "logfile_rotated", "from": old_rel, "to": self._rel(newest)})
            self._file, self._offset, self._buffer = newest, 0, b""

    def _emit(self, entries: list[dict], file_rel: str) -> None:
        self.seq += 1
        last = entries[-1]
        frame = {
            "type": "logfile_entries",
            "seq": self.seq,
            "file": file_rel,
            "startOffset": entries[0]["offset"],
            "endOffset": last["offset"] + len(last["text"].encode("utf-8")) + 1,
            "entries": entries,
            "dropped": self._dropped_since_flush,
        }
        self._dropped_since_flush = 0
        self._send(frame)

    def _flush(self, now: float) -> None:
        self._last_flush = now
        if not self._pending:
            return
        entries, self._pending, self._pending_bytes = self._pending, [], 0
        self._emit(entries, self._rel(self._file))

    def run(self) -> None:
        """线程体：轮询直到 stop；unfollow 发 finished，send 失败 / hub 断连静默退出。"""
        try:
            while not self.stopped:
                time.sleep(log_constants.FOLLOW_POLL_SEC)
                self.tick(time.monotonic())
            if self.stop_reason == "unfollow":
                self._flush(time.monotonic())
                self._send({"type": "logfile_finished", "reason": "unfollow"})
        finally:
            with _sessions_guard:
                if _sessions.get(self.session_id) is self:
                    _sessions.pop(self.session_id, None)


def start_follow(ws, data: dict) -> None:
    session_id = str(data.get("sessionId") or "").strip()
    if not session_id:
        return
    with _sessions_guard:
        existing = _sessions.get(session_id)
        if existing is not None and not existing.stopped:
            existing.send_started()  # 幂等：不重开，只重发 started
            return
        live = sum(1 for s in _sessions.values() if not s.stopped)
        if live >= log_constants.MAX_FOLLOW_SESSIONS:
            send_message(
                ws,
                {
                    "type": "logfile_error",
                    "sessionId": session_id,
                    "error": {"code": "busy", "message": f"too many follow sessions (max {log_constants.MAX_FOLLOW_SESSIONS})"},
                },
            )
            return
        session = FollowSession(ws, data)
        _sessions[session_id] = session
    if not session.start():
        with _sessions_guard:
            _sessions.pop(session_id, None)
        return
    threading.Thread(target=session.run, daemon=True, name=f"logfollow-{session_id}").start()


def stop_follow(data: dict) -> None:
    session_id = str(data.get("sessionId") or "").strip()
    with _sessions_guard:
        session = _sessions.get(session_id)
    if session is not None:
        session.stop("unfollow")


def stop_all() -> int:
    with _sessions_guard:
        sessions = list(_sessions.values())
        _sessions.clear()
    for s in sessions:
        s.stop("hub_disconnected")
    return len(sessions)
