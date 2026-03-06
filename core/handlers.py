"""
handlers.py — 业务指令处理层

每新增一种功能，在此模块中添加对应的处理函数，
并在 HANDLERS 字典中注册即可，无需修改其他模块。
"""
import logging
import os
import subprocess

from services.compose import find_compose_file, run_compose, update_image_in_compose

logger = logging.getLogger(__name__)


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
    success = True

    try:
        # 1. 修改 compose 文件中的 image 字段
        updated_services = update_image_in_compose(compose_file, image)
        if not updated_services:
            all_output.append(f"[warn] No service matched repository of '{image}', proceeding anyway.")
        else:
            all_output.append(f"[info] Updated image in services: {', '.join(updated_services)}")

        # 2. docker compose pull
        ok, out = run_compose(project_dir, ['pull'])
        all_output.append(f"=== docker compose pull ===\n{out}")
        if not ok:
            logger.warning("pull returned non-zero, continuing...")

        # 3. docker compose down
        ok, out = run_compose(project_dir, ['down'])
        all_output.append(f"=== docker compose down ===\n{out}")
        if not ok:
            success = False

        # 4. docker compose up -d
        if success:
            ok, out = run_compose(project_dir, ['up', '-d'])
            all_output.append(f"=== docker compose up -d ===\n{out}")
            if not ok:
                success = False

    except subprocess.TimeoutExpired:
        send_error(ws, request_id, "Command execution timed out (5 min)")
        return
    except Exception as e:
        logger.exception("Execution error")
        send_error(ws, request_id, str(e))
        return

    _reply(ws, request_id, success, '\n'.join(all_output), 'update', project_dir)


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
    validated = _validate_base(ws, data)
    if validated is None:
        return

    request_id, action, project_dir = validated

    handler = HANDLERS.get(action)
    if handler is None:
        send_error(ws, request_id,
                   f"Unsupported action '{action}'. Allowed: {', '.join(HANDLERS)}")
        return

    handler(ws, data, request_id, project_dir)
