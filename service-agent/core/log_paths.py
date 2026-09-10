"""
log_paths.py — 日志文件协议的路径解析与列文件。

安全边界（spec §3.2.1）：日志根 = realpath(<compose 目录>/<logDir 或 logs>)，必须落在 compose 目录之内；
file / subdir 是相对日志根的相对路径，拼接 realpath 后仍须在根内；只认普通文件且文件名匹配
`.log` / `.log.N`。任何越界、软链逃逸、非常规文件一律拒绝。
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime

from config import CHINA_TZ
from core.handlers import send_message
from core.log_constants import LIST_MAX_DEPTH, LIST_MAX_FILES
from services.compose import find_compose_file

logger = logging.getLogger(__name__)

LOG_NAME_RE = re.compile(r"\.log(\.\d+)?$")


class LogPathError(Exception):
    """带协议错误码的路径错误：code ∈ not_found / forbidden / invalid。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def error_payload(exc: Exception) -> dict:
    if isinstance(exc, LogPathError):
        return {"code": exc.code, "message": exc.message}
    return {"code": "io_error", "message": str(exc)[:200]}


def _inside(path: str, root: str) -> bool:
    root = root.rstrip(os.sep)
    return path == root or path.startswith(root + os.sep)


def _check_rel(rel: str, what: str) -> str:
    rel = (rel or "").strip()
    if "\0" in rel or os.path.isabs(rel):
        raise LogPathError("forbidden", f"{what} must be a relative path: {rel!r}")
    return rel


def resolve_log_root(project_dir: str | None, log_dir: str | None) -> str:
    if not project_dir or not os.path.isdir(project_dir):
        raise LogPathError("not_found", f"Directory not found: {project_dir}")
    if not find_compose_file(project_dir):
        raise LogPathError("not_found", f"No docker-compose.yaml/yml found in {project_dir}")
    rel = _check_rel(log_dir or "", "logDir") or "logs"
    project_real = os.path.realpath(project_dir)
    root = os.path.realpath(os.path.join(project_real, rel))
    if not _inside(root, project_real):
        raise LogPathError("forbidden", f"logDir escapes compose dir: {rel}")
    if not os.path.isdir(root):
        raise LogPathError("not_found", f"Log dir not found: {root}")
    return root


def _resolve_inside(root: str, rel: str | None, what: str) -> str:
    rel = _check_rel(rel or "", what)
    target = os.path.realpath(os.path.join(root, rel)) if rel else os.path.realpath(root)
    if not _inside(target, os.path.realpath(root)):
        raise LogPathError("forbidden", f"{what} escapes log root: {rel}")
    return target


def is_log_filename(name: str) -> bool:
    return bool(LOG_NAME_RE.search(name))


def resolve_subdir(root: str, subdir: str | None) -> str:
    target = _resolve_inside(root, subdir, "subdir")
    if not os.path.isdir(target):
        raise LogPathError("not_found", f"Subdir not found: {subdir}")
    return target


def resolve_log_file(root: str, rel: str | None) -> str:
    target = _resolve_inside(root, rel, "file")
    if not os.path.isfile(target) or os.path.islink(target) or not is_log_filename(os.path.basename(target)):
        raise LogPathError("not_found", f"Log file not found: {rel}")
    return target


def format_mtime(ts: float) -> str:
    """带时区偏移的 ISO 8601（如 2026-09-05T17:30:15+08:00）。

    绝不能输出无时区标记的本地时间：agent 容器常是 UTC，而中枢与被管应用容器多为 CST，
    中枢 new Date() 会按自己的时区解读这串字符，整表时间平移 8 小时——界面上会出现
    「快照 09:30」与正文最后一行「17:30」并排显示。写法对齐 health_server._format_timestamp。
    """
    return datetime.fromtimestamp(ts, CHINA_TZ).isoformat(timespec="seconds")


def list_log_files(root: str) -> list[dict]:
    """递归 ≤ LIST_MAX_DEPTH 层、白名单文件、mtime 倒序、最多 LIST_MAX_FILES 项。"""
    root = os.path.realpath(root)
    root_depth = root.rstrip(os.sep).count(os.sep)
    found: list[tuple[float, dict]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        depth = dirpath.rstrip(os.sep).count(os.sep) - root_depth
        dirnames[:] = [] if depth >= LIST_MAX_DEPTH else [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if not is_log_filename(name):
                continue
            full = os.path.join(dirpath, name)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            st = os.stat(full)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            found.append((st.st_mtime, {"path": rel, "size": st.st_size, "mtime": format_mtime(st.st_mtime)}))
    found.sort(key=lambda item: (item[0], item[1]["path"]), reverse=True)
    return [item[1] for item in found[:LIST_MAX_FILES]]


def newest_log_file(dirpath: str) -> str | None:
    best: str | None = None
    best_key: tuple[float, str] | None = None
    for name in os.listdir(dirpath):
        full = os.path.join(dirpath, name)
        if not is_log_filename(name) or os.path.islink(full) or not os.path.isfile(full):
            continue
        key = (os.stat(full).st_mtime, name)
        if best_key is None or key > best_key:
            best, best_key = full, key
    return best


def handle_list(ws, data: dict) -> None:
    """`logfile_list` 处理（在独立线程里跑）。总回一帧；缺 requestId 静默忽略。"""
    request_id = str(data.get("requestId") or "").strip()
    if not request_id:
        return
    frame: dict = {"type": "logfile_list_result", "requestId": request_id, "root": None, "files": []}
    try:
        root = resolve_log_root(data.get("dir"), data.get("logDir"))
        frame["root"] = root
        frame["files"] = list_log_files(root)
    except LogPathError as exc:
        logger.warning("logfile_list rejected: request_id=%s code=%s %s", request_id, exc.code, exc.message)
        frame["error"] = error_payload(exc)
    except Exception as exc:  # noqa: BLE001 —— 协议要求总回一帧
        logger.exception("logfile_list failed: request_id=%s", request_id)
        frame["error"] = error_payload(exc)
    send_message(ws, frame)
