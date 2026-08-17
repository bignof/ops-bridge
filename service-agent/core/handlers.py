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

from core import outbox
from services.compose import (
    find_compose_file,
    read_compose_file,
    restore_compose_file,
    run_compose,
    resolve_container_port_mapping,
    update_image_in_compose,
)
from services.app_http import drain, wait_healthy

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

# result 文本字段（output/error）出站前的截断上限（UTF-8 字节）。
# hub 命令表的 output/error 在老版本里是 MySQL TEXT（65535 字节），超限回写报 Data too long →
# hub 按「落库失败不 ack」协议不确认 → 本 agent 无限补投（2026-08-17 事故）。60000 留出余量，
# 对未升级 LONGTEXT 的老 hub 也安全；update 拉镜像的 compose progress 行本就没有排障价值。
RESULT_TEXT_MAX_BYTES = int(os.getenv('RESULT_TEXT_MAX_BYTES', '60000'))


def _clamp_text(s, max_bytes=None):
    """按 UTF-8 字节截断：超限保头 1/4、尾 3/4（compose 报错都在尾部），中间插截断标记。
    切在多字节字符中间的残缺序列直接丢弃（errors='ignore'），总字节只会更小。非 str 原样透传。"""
    if max_bytes is None:
        max_bytes = RESULT_TEXT_MAX_BYTES
    if not isinstance(s, str):
        return s
    raw = s.encode('utf-8')
    if len(raw) <= max_bytes:
        return s
    marker = f"\n…[截断:原始 {len(raw)} 字节,超出上限 {max_bytes},保留头尾]…\n"
    budget = max_bytes - len(marker.encode('utf-8'))
    head_n = budget // 4
    tail_n = budget - head_n
    head = raw[:head_n].decode('utf-8', errors='ignore')
    tail = raw[-tail_n:].decode('utf-8', errors='ignore')
    return head + marker + tail


def send_message(ws, message_dict):
    import json
    if ws:
        try:
            ws.send(json.dumps(message_dict))
            logger.debug(f"Sent: {message_dict.get('type')}")
        except Exception as e:
            logger.error(f"Send error: {e}")


def _send_result(ws, message_dict):
    """result 必达:先记账 outbox(断连丢失可补投,hub result_ack 清账),再立即经当前 ws 发送。"""
    outbox.remember(message_dict)
    send_message(ws, message_dict)


def send_error(ws, request_id, error_msg):
    logger.warning(f"Command failed: request_id={request_id}, error={error_msg}")
    _send_result(ws, {
        'type': 'result',
        'requestId': request_id,
        'status': 'failed',
        'error': _clamp_text(error_msg),
    })


def _reply(ws, request_id, success, output, action, project_dir):
    _send_result(ws, {
        'type': 'result',
        'requestId': request_id,
        'status': 'success' if success else 'failed',
        'output': _clamp_text(output),
        # message 会直接展示在 hub 命令流水的「结果」列，措辞必须与成败一致（曾恒写 finished 误导排障）
        'message': f"Action '{action}' {'succeeded' if success else 'failed'} in {project_dir}.",
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


def _graceful_healthcheck(compose_file):
    """graceful 模式下 docker 命令成功后调用一次。返回 (healthy, output_lines)。"""
    port = resolve_container_port_mapping(compose_file)
    if port is None:
        return False, ["[error] No host port mapped to container port 80; cannot verify health."]
    healthy, health_message = wait_healthy(port)
    return healthy, [f"=== healthcheck ===\n{health_message}"]


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
    # 首尾空白防御性剪净：hub 侧已归一化，但老版本 hub / 手工 WS 调用仍可能带入
    # （尾随空格会让 docker 判 invalid reference format 秒败）
    image = (data.get('image') or '').strip()
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

        # --quiet:非 TTY 下 compose pull 每层每秒刷一行 plain progress,大镜像一次 pull 几十万字符,
        # 全无排障价值还把 result 撑爆(2026-08-17 事故的日志主力);错误输出不受影响照常返回
        ok, out = run_compose(project_dir, ['pull', '--quiet'])
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

    ok = True
    if data.get('graceful'):
        healthy, extra_lines = _graceful_healthcheck(compose_file)
        all_output.extend(extra_lines)
        ok = healthy

    _reply(ws, request_id, ok, '\n'.join(all_output), 'update', project_dir)


def handle_drain(ws, data, request_id, project_dir):
    """drain: 调用本机 NocoBase 应用的 /api/k8s/shutdown，让它体面地准备好被重启。"""
    compose_file = find_compose_file(project_dir)
    if not compose_file:
        send_error(ws, request_id, f"No docker-compose.yaml/yml found in {project_dir}")
        return

    port = resolve_container_port_mapping(compose_file)
    if port is None:
        send_error(ws, request_id, f"No host port mapped to container port 80 in {compose_file}")
        return

    logger.info(f"drain: dir={project_dir}, port={port}")
    send_message(ws, {'type': 'ack', 'requestId': request_id, 'status': 'processing'})

    ok, message = drain(port, token=data.get('shutdownToken'))
    _reply(ws, request_id, ok, message, 'drain', project_dir)


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

    output_lines = [f"=== docker compose restart ===\n{out}"]
    if ok and data.get('graceful'):
        healthy, extra_lines = _graceful_healthcheck(compose_file)
        output_lines.extend(extra_lines)
        ok = healthy

    _reply(ws, request_id, ok, '\n'.join(output_lines), 'restart', project_dir)


# ─────────────────────────────────────────────
# 注册表：新增 action 只需在这里添加一行
# ─────────────────────────────────────────────

HANDLERS = {
    'update':  handle_update,
    'restart': handle_restart,
    'drain':   handle_drain,
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
