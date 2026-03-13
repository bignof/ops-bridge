from __future__ import annotations

import logging
import os
import subprocess
import threading
from typing import Any

from core.handlers import send_message
from services.compose import find_compose_file, open_compose_process


logger = logging.getLogger(__name__)

_sessions: dict[str, dict[str, Any]] = {}
_sessions_guard = threading.Lock()


def _send_logs_error(ws, session_id: str, error: str) -> None:
    logger.warning("Log session failed: session_id=%s, error=%s", session_id, error)
    send_message(
        ws,
        {
            "type": "logs_error",
            "sessionId": session_id,
            "error": error,
        },
    )


def _register_process(session_id: str, process: subprocess.Popen[str]) -> None:
    with _sessions_guard:
        _sessions[session_id] = {
            "process": process,
            "stop_requested": False,
        }


def _mark_stop_requested(session_id: str) -> subprocess.Popen[str] | None:
    with _sessions_guard:
        state = _sessions.get(session_id)
        if state is None:
            return None
        state["stop_requested"] = True
        return state["process"]


def _pop_session(session_id: str) -> dict[str, Any] | None:
    with _sessions_guard:
        return _sessions.pop(session_id, None)


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return

    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def stop_log_session(data: dict[str, Any]) -> None:
    session_id = str(data.get("sessionId") or "").strip()
    if not session_id:
        return

    process = _mark_stop_requested(session_id)
    if process is None:
        return

    logger.info("Stopping log session: session_id=%s", session_id)
    _stop_process(process)


def _stream_logs(ws, *, session_id: str, project_dir: str, tail: int, timestamps: bool) -> None:
    args = ["logs", "-f", "--tail", str(tail)]
    if timestamps:
        args.append("--timestamps")

    try:
        process = open_compose_process(project_dir, args)
    except Exception as exc:
        logger.exception("Failed to start log session %s", session_id)
        _send_logs_error(ws, session_id, str(exc))
        return

    _register_process(session_id, process)
    send_message(
        ws,
        {
            "type": "logs_started",
            "sessionId": session_id,
            "tail": tail,
            "timestamps": timestamps,
        },
    )

    chunks_sent = 0
    try:
        stdout = process.stdout
        if stdout is None:
            raise RuntimeError("compose log stream did not expose stdout")

        for chunk in iter(stdout.readline, ""):
            if not chunk:
                break
            chunks_sent += 1
            send_message(
                ws,
                {
                    "type": "logs_chunk",
                    "sessionId": session_id,
                    "chunk": chunk,
                },
            )

        process.wait(timeout=5)
    except Exception as exc:
        logger.exception("Log session %s crashed", session_id)
        _send_logs_error(ws, session_id, str(exc))
        _stop_process(process)
        _pop_session(session_id)
        return
    finally:
        if process.stdout is not None:
            process.stdout.close()

    state = _pop_session(session_id)
    stopped = bool(state and state.get("stop_requested"))
    send_message(
        ws,
        {
            "type": "logs_finished",
            "sessionId": session_id,
            "exitCode": process.returncode,
            "stopped": stopped,
            "chunks": chunks_sent,
        },
    )


def start_log_session(ws, data: dict[str, Any]) -> None:
    session_id = str(data.get("sessionId") or "").strip()
    project_dir = data.get("dir")
    tail = data.get("tail", 200)
    timestamps = bool(data.get("timestamps", False))

    if not session_id:
        return
    if not project_dir:
        _send_logs_error(ws, session_id, "Missing required field: 'dir'")
        return
    if not os.path.isdir(project_dir):
        _send_logs_error(ws, session_id, f"Directory not found: {project_dir}")
        return
    if not find_compose_file(project_dir):
        _send_logs_error(ws, session_id, f"No docker-compose.yaml/yml found in {project_dir}")
        return

    try:
        tail_value = int(tail)
    except (TypeError, ValueError):
        _send_logs_error(ws, session_id, f"Invalid tail value: {tail}")
        return
    if tail_value < 1:
        _send_logs_error(ws, session_id, "Tail must be a positive integer")
        return

    logger.info(
        "Starting log session: session_id=%s, dir=%s, tail=%s, timestamps=%s",
        session_id,
        project_dir,
        tail_value,
        timestamps,
    )
    threading.Thread(
        target=_stream_logs,
        kwargs={
            "ws": ws,
            "session_id": session_id,
            "project_dir": project_dir,
            "tail": tail_value,
            "timestamps": timestamps,
        },
        daemon=True,
    ).start()
