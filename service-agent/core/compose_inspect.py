"""
compose_inspect.py — compose 项目发现与识别（spec §8.1）。只读文件系统，不执行 docker。

discover：从 PROJECTS_ROOT 向下最多 DISCOVER_MAX_DEPTH 层找 docker-compose.yaml|yml，跳过 . 开头目录与
node_modules，找到项目后不再下钻，最多 DISCOVER_MAX_PROJECTS 个（超出 truncated），按 dir 排序。
inspect：yaml.safe_load，取每个 service 的 container_name / image / ports（只认字符串短语法）。
"""
from __future__ import annotations

import logging
import os

import yaml

from config import PROJECTS_ROOT
from core import log_constants
from core.handlers import send_message
from services.compose import find_compose_file, read_compose_file

logger = logging.getLogger(__name__)

_SKIP_DIRS = {"node_modules"}


class ComposeInspectError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def inspect_compose(project_dir: str | None) -> dict:
    if not project_dir or not os.path.isdir(project_dir):
        raise ComposeInspectError("not_found", f"Directory not found: {project_dir}")
    compose_file = find_compose_file(project_dir)
    if not compose_file:
        raise ComposeInspectError("not_found", f"No docker-compose.yaml/yml found in {project_dir}")
    try:
        content = yaml.safe_load(read_compose_file(compose_file)) or {}
    except yaml.YAMLError as exc:
        raise ComposeInspectError("invalid", f"compose yaml parse failed: {str(exc)[:200]}") from exc
    services: list[dict] = []
    raw_services = content.get("services") if isinstance(content, dict) else None
    for name, cfg in (raw_services or {}).items():
        if not isinstance(cfg, dict):
            continue
        ports = [p for p in (cfg.get("ports") or []) if isinstance(p, str)]
        services.append(
            {"name": str(name), "containerName": cfg.get("container_name"), "image": cfg.get("image"), "ports": ports}
        )
    return {"dir": os.path.abspath(project_dir), "composeFile": os.path.basename(compose_file), "services": services}


def discover_projects(root: str) -> tuple[list[dict], bool]:
    if not os.path.isdir(root):
        return [], False
    root = os.path.abspath(root)
    root_depth = root.rstrip(os.sep).count(os.sep)
    dirs: list[str] = []
    for dirpath, dirnames, _filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in _SKIP_DIRS)
        if find_compose_file(dirpath):
            dirs.append(dirpath)
            dirnames[:] = []  # 项目内不再下钻
            continue
        depth = dirpath.rstrip(os.sep).count(os.sep) - root_depth
        if depth >= log_constants.DISCOVER_MAX_DEPTH:
            dirnames[:] = []
    dirs.sort()
    truncated = len(dirs) > log_constants.DISCOVER_MAX_PROJECTS
    projects: list[dict] = []
    for d in dirs[: log_constants.DISCOVER_MAX_PROJECTS]:
        try:
            projects.append(inspect_compose(d))
        except ComposeInspectError as exc:
            projects.append(
                {
                    "dir": d,
                    "composeFile": os.path.basename(find_compose_file(d) or ""),
                    "services": [],
                    "error": {"code": exc.code, "message": exc.message},
                }
            )
    return projects, truncated


def handle_discover(ws, data: dict) -> None:
    request_id = str(data.get("requestId") or "").strip()
    if not request_id:
        return
    frame: dict = {"type": "compose_discover_result", "requestId": request_id, "root": PROJECTS_ROOT, "projects": [], "truncated": False}
    try:
        frame["projects"], frame["truncated"] = discover_projects(PROJECTS_ROOT)
    except Exception as exc:  # noqa: BLE001 —— 总回一帧
        logger.exception("compose_discover failed: request_id=%s", request_id)
        frame["error"] = {"code": "io_error", "message": str(exc)[:200]}
    send_message(ws, frame)


def handle_inspect(ws, data: dict) -> None:
    request_id = str(data.get("requestId") or "").strip()
    if not request_id:
        return
    frame: dict = {"type": "compose_inspect_result", "requestId": request_id, "dir": data.get("dir"), "composeFile": None, "services": []}
    try:
        info = inspect_compose(data.get("dir"))
        frame.update(info)
    except ComposeInspectError as exc:
        frame["error"] = {"code": exc.code, "message": exc.message}
    except Exception as exc:  # noqa: BLE001
        logger.exception("compose_inspect failed: request_id=%s", request_id)
        frame["error"] = {"code": "io_error", "message": str(exc)[:200]}
    send_message(ws, frame)
