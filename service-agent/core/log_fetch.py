"""
log_fetch.py — 把某个日志文件 gzip 流式上传到 hub（spec §3.2.4）。

hub 在 logfile_fetch 帧里给 uploadPath + 一次性 uploadToken；agent 边压边传（不落临时文件），
请求头带原始大小与文件 mtime。结果帧只是旁路通知，状态真源是 hub 的 HTTP 接收。
同一 agent 同时只跑 1 个 fetch，第二个直接回 busy。hub 断连时 abort_all() 让进行中的上传中止。
"""
from __future__ import annotations

import logging
import os
import threading
import zlib
from urllib.parse import urlsplit, urlunsplit

import requests

from config import HUB_HTTP_URL, WS_URL
from core import log_constants
from core.handlers import send_message
from core.log_paths import LogPathError, error_payload, format_mtime, resolve_log_file, resolve_log_root

logger = logging.getLogger(__name__)

_slots = threading.Semaphore(log_constants.MAX_FETCH_CONCURRENCY)
_abort = threading.Event()


class FetchAborted(Exception):
    pass


def hub_http_base(ws_url: str, override: str) -> str:
    if override:
        return override.strip().rstrip("/")
    parts = urlsplit(ws_url)
    scheme = "https" if parts.scheme == "wss" else "http"
    path = parts.path or ""
    idx = path.find("/ws/agent")
    base_path = path[:idx] if idx >= 0 else path
    return urlunsplit((scheme, parts.netloc, base_path.rstrip("/"), "", ""))


def gzip_chunks(path: str, abort: threading.Event, counter: dict):
    """生成器：按块读文件、gzip 压缩后产出；abort 置位即抛 FetchAborted。counter['sent'] 累计压缩字节。"""
    comp = zlib.compressobj(log_constants.FETCH_GZIP_LEVEL, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    counter["sent"] = 0
    with open(path, "rb") as fh:
        while True:
            if abort.is_set():
                raise FetchAborted()
            buf = fh.read(log_constants.FETCH_CHUNK_BYTES)
            if not buf:
                break
            out = comp.compress(buf)
            if out:
                counter["sent"] += len(out)
                yield out
    tail = comp.flush()
    counter["sent"] += len(tail)
    yield tail


def _result(ws, data: dict, ok: bool, **extra) -> None:
    frame = {"type": "logfile_fetch_result", "requestId": data.get("requestId"), "archiveId": data.get("archiveId"), "ok": ok}
    frame.update(extra)
    send_message(ws, frame)


def _release() -> None:
    _slots.release()


def _run_fetch(ws, data: dict, path: str) -> None:
    """线程体（start_fetch 已占槽位，这里负责释放）。"""
    counter: dict = {}
    size_raw = os.path.getsize(path)
    mtime = format_mtime(os.path.getmtime(path))
    url = hub_http_base(WS_URL, HUB_HTTP_URL) + str(data["uploadPath"])
    headers = {
        "Content-Type": "application/gzip",
        "X-Hub-Upload-Token": str(data.get("uploadToken") or ""),
        "X-Hub-Raw-Size": str(size_raw),
        "X-Hub-File-Mtime": mtime,
    }
    try:
        resp = requests.post(
            url,
            data=gzip_chunks(path, _abort, counter),
            headers=headers,
            timeout=(log_constants.FETCH_CONNECT_TIMEOUT_SEC, log_constants.FETCH_READ_TIMEOUT_SEC),
        )
        if 200 <= resp.status_code < 300:
            logger.info("logfile_fetch uploaded: archive=%s raw=%s sent=%s", data.get("archiveId"), size_raw, counter.get("sent"))
            _result(ws, data, True, sizeRaw=size_raw, sizeSent=counter.get("sent", 0))
        else:
            msg = f"HTTP {resp.status_code}: {(resp.text or '')[:200]}"
            logger.warning("logfile_fetch rejected by hub: archive=%s %s", data.get("archiveId"), msg)
            _result(ws, data, False, sizeRaw=size_raw, error={"code": "upload_failed", "message": msg})
    except FetchAborted:
        _result(ws, data, False, sizeRaw=size_raw, error={"code": "upload_failed", "message": "aborted: agent disconnected from hub"})
    except Exception as exc:  # noqa: BLE001 —— requests/urllib3 可能把生成器异常包一层，统一按上传失败上报
        logger.warning("logfile_fetch failed: archive=%s %s", data.get("archiveId"), exc)
        _result(ws, data, False, sizeRaw=size_raw, error={"code": "upload_failed", "message": str(exc)[:200]})
    finally:
        _abort.clear()
        _release()


def _reset_for_tests() -> None:
    """仅测试使用：复位槽位与中止事件（生产代码禁调）。"""
    global _slots
    _slots = threading.Semaphore(log_constants.MAX_FETCH_CONCURRENCY)
    _abort.clear()


def start_fetch(ws, data: dict) -> None:
    request_id = str(data.get("requestId") or "").strip()
    if not request_id:
        return
    try:
        root = resolve_log_root(data.get("dir"), data.get("logDir"))
        path = resolve_log_file(root, data.get("file"))
    except LogPathError as exc:
        _result(ws, data, False, error=error_payload(exc))
        return
    if not str(data.get("uploadPath") or "").startswith("/") or not data.get("uploadToken"):
        _result(ws, data, False, error={"code": "invalid", "message": "uploadPath/uploadToken required"})
        return
    if not _slots.acquire(blocking=False):
        _result(ws, data, False, error={"code": "busy", "message": "another fetch is in progress"})
        return
    threading.Thread(target=_run_fetch, args=(ws, data, path), daemon=True, name=f"logfetch-{request_id}").start()


def abort_all() -> None:
    """hub 断连：让进行中的上传在下一块时中止。"""
    _abort.set()
