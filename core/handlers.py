"""
handlers.py — 业务指令处理层

每新增一种功能，在此模块中添加对应的处理函数，
并在 HANDLERS 字典中注册即可，无需修改其他模块。
"""
import logging
import os
import subprocess
import threading
import time
from typing import TypedDict, cast

from services.compose import find_compose_file, read_compose_file, restore_compose_file, run_compose, update_image_in_compose

logger = logging.getLogger(__name__)


class ProjectExecutionState(TypedDict):
    projectDir: str
    activeRequestId: str | None
    activeAction: str | None
    activeSinceTs: float | None
    queuedCount: int


_project_locks: dict[str, threading.Lock] = {}
_project_locks_guard = threading.Lock()
_project_states: dict[str, ProjectExecutionState] = {}


def _project_lock_key(project_dir):
    return os.path.normcase(os.path.abspath(project_dir))


def _get_project_lock(project_dir):
    key = _project_lock_key(project_dir)
    with _project_locks_guard:
        project_lock = _project_locks.get(key)
        if project_lock is None:
            project_lock = threading.Lock()
            _project_locks[key] = project_lock
        return project_lock


def _enqueue_project_command(project_dir, request_id, action):
    key = _project_lock_key(project_dir)
    with _project_locks_guard:
        state = _project_states.get(key)
        if state is None:
            state = cast(
                ProjectExecutionState,
                {
                    'projectDir': os.path.abspath(project_dir),
                    'activeRequestId': None,
                    'activeAction': None,
                    'activeSinceTs': None,
                    'queuedCount': 0,
                },
            )
            _project_states[key] = state
        state['queuedCount'] += 1
        waiting_ahead = state['queuedCount'] - 1 + (1 if state['activeRequestId'] else 0)
        return key, waiting_ahead


def _start_project_command(key, request_id, action):
    with _project_locks_guard:
        state = _project_states[key]
        state['queuedCount'] = max(0, state['queuedCount'] - 1)
        state['activeRequestId'] = request_id
        state['activeAction'] = action
        state['activeSinceTs'] = time.time()


def _finish_project_command(key):
    with _project_locks_guard:
        state = _project_states.get(key)
        if state is None:
            return
        state['activeRequestId'] = None
        state['activeAction'] = None
        state['activeSinceTs'] = None
        if state['queuedCount'] == 0:
            _project_states.pop(key, None)


def get_command_execution_state():
    with _project_locks_guard:
        projects = []
        active_commands = 0
        queued_commands = 0
        for state in _project_states.values():
            active_request_id = state['activeRequestId']
            queued_count = state['queuedCount']
            if active_request_id:
                active_commands += 1
            queued_commands += queued_count
            projects.append(
                {
                    'projectDir': state['projectDir'],
                    'activeRequestId': active_request_id,
                    'activeAction': state['activeAction'],
                    'activeSinceTs': state['activeSinceTs'],
                    'queuedCount': queued_count,
                }
            )
        projects.sort(key=lambda item: item['projectDir'])
        return {
            'activeCommands': active_commands,
            'queuedCommands': queued_commands,
            'projects': projects,
        }


# ─────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────

def send_message(ws, message_dict):
    import json
    if ws:
        try:
            ws.send(json.dumps(message_dict))
            logger.debug(f"Sent: {message_dict.get('type')}")
        except Exception as e:
            logger.error(f"Send error: {e}")


def send_error(ws, request_id, error_msg):
    logger.warning(f"Command failed: request_id={request_id}, error={error_msg}")
    send_message(ws, {
        'type': 'result',
        'requestId': request_id,
        'status': 'failed',
        'error': error_msg,
    })


def _reply(ws, request_id, success, output, action, project_dir):
    send_message(ws, {
        'type': 'result',
        'requestId': request_id,
        'status': 'success' if success else 'failed',
        'output': output,
        'message': f"Action '{action}' finished in {project_dir}.",
    })
    (logger.info if success else logger.warning)(
        f"Action '{action}' {'succeeded' if success else 'failed'} in {project_dir}"
    )


def _append_compose_restore(output_lines, compose_file):
    output_lines.append(f"[info] Restored compose file: {compose_file}")


def _recover_previous_compose(project_dir, compose_file, original_compose, output_lines):
    restore_compose_file(compose_file, original_compose)
    _append_compose_restore(output_lines, compose_file)
    ok, out = run_compose(project_dir, ['up', '-d'])
    output_lines.append(f"=== recovery: docker compose up -d ===\n{out}")
    return ok


# ─────────────────────────────────────────────
# 公共参数校验
# ─────────────────────────────────────────────

def _validate_base(ws, data):
    """校验所有命令共用的必填字段，返回 (request_id, action, project_dir) 或 None（已回复错误）。"""
    request_id  = data.get('requestId', 'unknown')
    action      = data.get('action')
    project_dir = data.get('dir')

    if not action or not project_dir:
        send_error(ws, request_id, "Missing required fields: 'action' and 'dir'")
        return None

    if not os.path.isdir(project_dir):
        send_error(ws, request_id, f"Directory not found: {project_dir}")
        return None

    return request_id, action, project_dir


# ─────────────────────────────────────────────
# action 处理函数
# ─────────────────────────────────────────────

def handle_update(ws, data, request_id, project_dir):
    """
    update: 修改 compose 文件中的 image 字段，然后执行
    docker compose pull -> docker compose down -> docker compose up -d
    """
    image = data.get('image')
    if not image:
        send_error(ws, request_id, "Action 'update' requires the 'image' field")
        return

    compose_file = find_compose_file(project_dir)
    if not compose_file:
        send_error(ws, request_id, f"No docker-compose.yaml/yml found in {project_dir}")
        return

    logger.info(f"update: dir={project_dir}, image={image}")
    send_message(ws, {'type': 'ack', 'requestId': request_id, 'status': 'processing'})

    all_output = []
    original_compose = read_compose_file(compose_file)

    try:
        updated_services = update_image_in_compose(compose_file, image)
        if not updated_services:
            send_error(ws, request_id, f"No service image matched repository of '{image}' in {compose_file}")
            return

        all_output.append(f"[info] Updated image in services: {', '.join(updated_services)}")

        ok, out = run_compose(project_dir, ['pull'])
        all_output.append(f"=== docker compose pull ===\n{out}")
        if not ok:
            restore_compose_file(compose_file, original_compose)
            _append_compose_restore(all_output, compose_file)
            _reply(ws, request_id, False, '\n'.join(all_output), 'update', project_dir)
            return

        ok, out = run_compose(project_dir, ['down'])
        all_output.append(f"=== docker compose down ===\n{out}")
        if not ok:
            recovered = _recover_previous_compose(project_dir, compose_file, original_compose, all_output)
            if not recovered:
                all_output.append("[error] Recovery failed after unsuccessful docker compose down.")
            _reply(ws, request_id, False, '\n'.join(all_output), 'update', project_dir)
            return

        ok, out = run_compose(project_dir, ['up', '-d'])
        all_output.append(f"=== docker compose up -d ===\n{out}")
        if not ok:
            recovered = _recover_previous_compose(project_dir, compose_file, original_compose, all_output)
            if not recovered:
                all_output.append("[error] Recovery failed after unsuccessful docker compose up -d.")
            _reply(ws, request_id, False, '\n'.join(all_output), 'update', project_dir)
            return

    except subprocess.TimeoutExpired:
        restore_compose_file(compose_file, original_compose)
        send_error(ws, request_id, "Command execution timed out (5 min)")
        return
    except Exception as e:
        restore_compose_file(compose_file, original_compose)
        logger.exception("Execution error")
        send_error(ws, request_id, str(e))
        return

    _reply(ws, request_id, True, '\n'.join(all_output), 'update', project_dir)


def handle_restart(ws, data, request_id, project_dir):
    """restart: docker compose restart"""
    compose_file = find_compose_file(project_dir)
    if not compose_file:
        send_error(ws, request_id, f"No docker-compose.yaml/yml found in {project_dir}")
        return

    logger.info(f"restart: dir={project_dir}")
    send_message(ws, {'type': 'ack', 'requestId': request_id, 'status': 'processing'})

    try:
        ok, out = run_compose(project_dir, ['restart'])
    except subprocess.TimeoutExpired:
        send_error(ws, request_id, "Command execution timed out (5 min)")
        return
    except Exception as e:
        logger.exception("Execution error")
        send_error(ws, request_id, str(e))
        return

    _reply(ws, request_id, ok, f"=== docker compose restart ===\n{out}", 'restart', project_dir)


# ─────────────────────────────────────────────
# 注册表：新增 action 只需在这里添加一行
# ─────────────────────────────────────────────

HANDLERS = {
    'update':  handle_update,
    'restart': handle_restart,
}


# ─────────────────────────────────────────────
# 入口：由 ws_client 调用
# ─────────────────────────────────────────────

def dispatch(ws, data):
    """解析命令并分发到对应的 handler。"""
    logger.info(
        "Received command: request_id=%s, action=%s, dir=%s",
        data.get('requestId', 'unknown'),
        data.get('action'),
        data.get('dir'),
    )

    validated = _validate_base(ws, data)
    if validated is None:
        return

    request_id, action, project_dir = validated

    handler = HANDLERS.get(action)
    if handler is None:
        send_error(ws, request_id,
                   f"Unsupported action '{action}'. Allowed: {', '.join(HANDLERS)}")
        return

    project_key, waiting_ahead = _enqueue_project_command(project_dir, request_id, action)
    if waiting_ahead > 0:
        logger.info(
            "Command queued on project lock: request_id=%s, action=%s, dir=%s, waiting_ahead=%s",
            request_id,
            action,
            project_dir,
            waiting_ahead,
        )

    project_lock = _get_project_lock(project_dir)
    try:
        with project_lock:
            _start_project_command(project_key, request_id, action)
            logger.info(
                "Command acquired project lock: request_id=%s, action=%s, dir=%s",
                request_id,
                action,
                project_dir,
            )
            handler(ws, data, request_id, project_dir)
    finally:
        _finish_project_command(project_key)
        logger.info(
            "Command released project lock: request_id=%s, action=%s, dir=%s",
            request_id,
            action,
            project_dir,
        )
