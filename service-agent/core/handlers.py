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

import config
from core import graceful
from services.compose import find_compose_file, is_image_registry_allowed, read_compose_file, restore_compose_file, run_compose, update_image_in_compose, validate_managed_dir

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

    # ── 节点控制安全闸 ──
    # agent 挂载宿主 docker.sock，目录守卫（受管根 + 拒自身）抽到 services.compose.validate_managed_dir，
    # 与日志流路径（log_sessions）共用同一闸，逻辑保持单一来源。
    ok, reason = validate_managed_dir(project_dir)
    if not ok:
        send_error(ws, request_id, reason)
        return None

    return request_id, action, project_dir


# ─────────────────────────────────────────────
# action 处理函数
# ─────────────────────────────────────────────

class RedeployResult(TypedDict):
    """redeploy_compose_image 的结构化返回（不发 ws，由调用方决定如何回包）。

    - ok：整体是否成功（pull→down→up 全绿）。
    - output：拼好的多段 compose 输出（含回滚痕迹），供 _reply / 日志展示。
    - error：仅「快速失败」（缺 image / 非白名单 / 无 compose 文件 / 无服务匹配 / 超时 / 异常）
      时为可回传的短错误串；compose 步骤失败（pull/down/up 非 0）时 error=None、靠 ok=False + output。
    """
    ok: bool
    output: str
    error: str | None


def redeploy_compose_image(data, request_id, project_dir):
    """重新拉镜像并重部署的**纯核心**（镜像重写 → pull → down → up -d，含回滚 + 白名单校验）。

    抽出来供 handle_update（命令路径，自带 ack/_reply）与 rolling.handle_graceful_redeploy
    （滚动路径，无 ack、单一 result type、需在重建与回结果之间插 wait-ready）共用——
    两条路径的 compose / 回滚 / 白名单逻辑必须单一来源，绝不复制两套。
    本函数**不发任何 ws 消息**，仅返回 RedeployResult；编排（ack / 回包 / wait-ready）交给调用方。
    """
    image = data.get('image')
    if not image:
        return RedeployResult(ok=False, output='', error="Action 'update' requires the 'image' field")

    # 镜像 registry 白名单闸：在 pull / 任何 compose 改写之前拦截非白名单来源。
    # update / pull-redeploy(force) / graceful-redeploy 都经本核心，故该校验同时覆盖三条重部署路径。
    if not is_image_registry_allowed(image, config.IMAGE_REGISTRY_ALLOWLIST):
        return RedeployResult(ok=False, output='', error=f"镜像来源不在白名单: {image}")

    compose_file = find_compose_file(project_dir)
    if not compose_file:
        return RedeployResult(ok=False, output='', error=f"No docker-compose.yaml/yml found in {project_dir}")

    logger.info(f"redeploy: dir={project_dir}, image={image}")

    all_output = []
    original_compose = read_compose_file(compose_file)

    try:
        updated_services = update_image_in_compose(compose_file, image)
        if not updated_services:
            return RedeployResult(
                ok=False, output='\n'.join(all_output),
                error=f"No service image matched repository of '{image}' in {compose_file}",
            )

        all_output.append(f"[info] Updated image in services: {', '.join(updated_services)}")

        ok, out = run_compose(project_dir, ['pull'])
        all_output.append(f"=== docker compose pull ===\n{out}")
        if not ok:
            restore_compose_file(compose_file, original_compose)
            _append_compose_restore(all_output, compose_file)
            return RedeployResult(ok=False, output='\n'.join(all_output), error=None)

        ok, out = run_compose(project_dir, ['down'])
        all_output.append(f"=== docker compose down ===\n{out}")
        if not ok:
            recovered = _recover_previous_compose(project_dir, compose_file, original_compose, all_output)
            if not recovered:
                all_output.append("[error] Recovery failed after unsuccessful docker compose down.")
            return RedeployResult(ok=False, output='\n'.join(all_output), error=None)

        ok, out = run_compose(project_dir, ['up', '-d'])
        all_output.append(f"=== docker compose up -d ===\n{out}")
        if not ok:
            recovered = _recover_previous_compose(project_dir, compose_file, original_compose, all_output)
            if not recovered:
                all_output.append("[error] Recovery failed after unsuccessful docker compose up -d.")
            return RedeployResult(ok=False, output='\n'.join(all_output), error=None)

    except subprocess.TimeoutExpired:
        restore_compose_file(compose_file, original_compose)
        return RedeployResult(ok=False, output='\n'.join(all_output), error="Command execution timed out (5 min)")
    except Exception as e:
        restore_compose_file(compose_file, original_compose)
        logger.exception("Execution error")
        return RedeployResult(ok=False, output='\n'.join(all_output), error=str(e))

    return RedeployResult(ok=True, output='\n'.join(all_output), error=None)


def handle_update(ws, data, request_id, project_dir):
    """
    update: 修改 compose 文件中的 image 字段，然后执行
    docker compose pull -> docker compose down -> docker compose up -d
    """
    image = data.get('image')
    if not image:
        send_error(ws, request_id, "Action 'update' requires the 'image' field")
        return

    # 镜像 registry 白名单闸：在 pull / 任何 compose 改写之前拦截非白名单来源。
    # pull-redeploy(force) 复用 handle_update，因此该校验同样覆盖重部署路径。
    if not is_image_registry_allowed(image, config.IMAGE_REGISTRY_ALLOWLIST):
        send_error(ws, request_id, f"镜像来源不在白名单: {image}")
        return

    compose_file = find_compose_file(project_dir)
    if not compose_file:
        send_error(ws, request_id, f"No docker-compose.yaml/yml found in {project_dir}")
        return

    # ack 后把 pull/down/up + 回滚交给共享核心 redeploy_compose_image（与 graceful-redeploy 同一份逻辑）；
    # 本函数只负责命令路径的 ack / send_error / _reply 编排，保持对外行为不变。
    send_message(ws, {'type': 'ack', 'requestId': request_id, 'status': 'processing'})

    result = redeploy_compose_image(data, request_id, project_dir)
    # 快速失败（无服务匹配 / 超时 / 异常）→ send_error（保持原 update 语义）；其中超时/异常前核心已回滚 compose。
    if not result['ok'] and result['error'] is not None:
        send_error(ws, request_id, result['error'])
        return

    _reply(ws, request_id, result['ok'], result['output'], 'update', project_dir)


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


def handle_start(ws, data, request_id, project_dir):
    """start: docker compose up -d（幂等，已在运行则为 no-op）"""
    compose_file = find_compose_file(project_dir)
    if not compose_file:
        send_error(ws, request_id, f"No docker-compose.yaml/yml found in {project_dir}")
        return

    logger.info(f"start: dir={project_dir}")
    send_message(ws, {'type': 'ack', 'requestId': request_id, 'status': 'processing'})

    try:
        ok, out = run_compose(project_dir, ['up', '-d'])
    except subprocess.TimeoutExpired:
        send_error(ws, request_id, "Command execution timed out (5 min)")
        return
    except Exception as e:
        logger.exception("Execution error")
        send_error(ws, request_id, str(e))
        return

    _reply(ws, request_id, ok, f"=== docker compose up -d ===\n{out}", 'start', project_dir)


def _force_stop(ws, data, request_id, project_dir):
    """stop 的 force 语义（T2 实现，原样保留）: docker compose stop。

    用 stop 而非 down——down 会删除容器/网络，影响后续 start。
    """
    compose_file = find_compose_file(project_dir)
    if not compose_file:
        send_error(ws, request_id, f"No docker-compose.yaml/yml found in {project_dir}")
        return

    logger.info(f"stop(force): dir={project_dir}")
    send_message(ws, {'type': 'ack', 'requestId': request_id, 'status': 'processing'})

    try:
        ok, out = run_compose(project_dir, ['stop'])
    except subprocess.TimeoutExpired:
        send_error(ws, request_id, "Command execution timed out (5 min)")
        return
    except Exception as e:
        logger.exception("Execution error")
        send_error(ws, request_id, str(e))
        return

    _reply(ws, request_id, ok, f"=== docker compose stop ===\n{out}", 'stop', project_dir)


def _graceful_stop(ws, data, request_id, project_dir):
    """stop 的 graceful 语义: 先 drain（POST /api/k8s/shutdown 阻塞至 worker 排空）再 docker compose stop。

    drain 失败（healthBaseUrl 非法 / shutdown 非 200）→ send_error，**不自动转 force**（人工决定）。
    """
    compose_file = find_compose_file(project_dir)
    if not compose_file:
        send_error(ws, request_id, f"No docker-compose.yaml/yml found in {project_dir}")
        return

    logger.info(f"stop(graceful): dir={project_dir}")
    send_message(ws, {'type': 'ack', 'requestId': request_id, 'status': 'processing'})

    # 先 drain：单独 try，只把 drain 自身的失败（ValueError/RuntimeError）翻成 drain 语义错误，
    # 避免误把后续 compose 的异常也归类成「drain 失败」。drain 失败时绝不 compose stop、不自动转 force。
    try:
        graceful.drain(data.get('healthBaseUrl'), int(data.get('shutdownTimeoutSec', 60)))
    except ValueError as e:
        # healthBaseUrl 非法 / 非内网：在发 shutdown 前就被拒，绝不 compose stop
        send_error(ws, request_id, f"healthBaseUrl 非法或非内网地址: {e}")
        return
    except RuntimeError as e:
        # drain 失败（shutdown 非 200）：不自动转 force，由人工决定
        send_error(ws, request_id, f"优雅停机 drain 失败: {e}")
        return
    except Exception as e:
        # worker 不可达时 requests 抛 ConnectionError/Timeout（OSError 子类，非 Value/Runtime）。
        # 若不兜底，异常逃出 handler 死在 daemon 线程 → agent 既不 send_error 也不 send result → hub 命令永卡 queued。
        # 与 handle_pull_redeploy 的 graceful 分支对称：兜底 send_error，不 compose stop、不自动转 force。
        logger.exception("Execution error")
        send_error(ws, request_id, f"优雅停机 drain 失败: {e}")
        return

    # drain 成功后再 compose stop，沿用 force 路径同款的超时/异常兜底
    try:
        ok, out = run_compose(project_dir, ['stop'])
    except subprocess.TimeoutExpired:
        send_error(ws, request_id, "Command execution timed out (5 min)")
        return
    except Exception as e:
        logger.exception("Execution error")
        send_error(ws, request_id, str(e))
        return

    _reply(ws, request_id, ok, f"=== drain → docker compose stop ===\n{out}", 'stop', project_dir)


def handle_stop(ws, data, request_id, project_dir):
    """stop: 按 mode 分流。

    - mode='force'   → 直接 docker compose stop（T2 语义）。
    - 否则（默认 graceful）→ 先 drain（优雅排空）再 docker compose stop。
    """
    mode = data.get('mode', 'graceful')
    if mode == 'force':
        _force_stop(ws, data, request_id, project_dir)
    else:
        _graceful_stop(ws, data, request_id, project_dir)


def handle_force_restart(ws, data, request_id, project_dir):
    """force-restart: docker compose restart"""
    compose_file = find_compose_file(project_dir)
    if not compose_file:
        send_error(ws, request_id, f"No docker-compose.yaml/yml found in {project_dir}")
        return

    logger.info(f"force-restart: dir={project_dir}")
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

    _reply(ws, request_id, ok, f"=== docker compose restart ===\n{out}", 'force-restart', project_dir)


def handle_pull_redeploy(ws, data, request_id, project_dir):
    """pull-redeploy: 按 mode 分流，重新拉镜像并重部署（pull → down → up -d，含回滚）。

    - mode='force'   → 直接复用 handle_update（image 重写 → pull → down → up -d + 回滚）。
    - 否则（默认 graceful）→ 先 drain（优雅排空）再 handle_update；drain 失败则 send_error，**不继续 update、不自动转 force**。

    注：与 update 一样要求 data['image']；handle_update 自带 ack/_reply（label 'update'，requestId 对账）。
    """
    mode = data.get('mode', 'graceful')
    if mode == 'force':
        handle_update(ws, data, request_id, project_dir)
        return

    logger.info(f"pull-redeploy(graceful): dir={project_dir}")
    try:
        graceful.drain(data.get('healthBaseUrl'), int(data.get('shutdownTimeoutSec', 60)))
    except ValueError as e:
        send_error(ws, request_id, f"healthBaseUrl 非法或非内网地址: {e}")
        return
    except RuntimeError as e:
        send_error(ws, request_id, f"优雅停机 drain 失败: {e}")
        return
    except Exception as e:
        logger.exception("Execution error")
        send_error(ws, request_id, str(e))
        return

    # drain 成功后再重部署，复用 handle_update 的镜像重写 / pull / down / up -d + 回滚
    handle_update(ws, data, request_id, project_dir)


# ─────────────────────────────────────────────
# 注册表：新增 action 只需在这里添加一行
# ─────────────────────────────────────────────

HANDLERS = {
    'update':        handle_update,
    'restart':       handle_restart,
    'start':         handle_start,
    'stop':          handle_stop,
    'force-restart': handle_force_restart,
    'pull-redeploy': handle_pull_redeploy,
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
