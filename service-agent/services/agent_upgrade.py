"""Agent 自升级：持久化任务 + 独立 Docker 执行器；不依赖当前 Agent 进程存活。"""
import contextlib
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import uuid

import yaml

PROTOCOL = 1
TERMINAL = {'success', 'rolled_back', 'failed'}
_guard = threading.RLock()
_active = 0
_starting = False
_runtime = None
_runtime_checked_at = 0.0
RUNTIME_RETRY_SECONDS = 60
# 执行器已退出、任务仍停在非终态超过此时长，按当前运行镜像收敛（执行器正常推进时每一步都会刷新 updatedAt）
STALE_JOB_SECONDS = 120
_boot_id = str(uuid.uuid4())
logger = logging.getLogger(__name__)


def docker(*args, timeout=60):
    # 禁止 shell 拼接；Docker 原始输出可能含仓库凭据/配置，只保留返回码。
    result = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError('Docker 操作失败（exit=%s，步骤=%s）' % (result.returncode, args[0]))
    return result.stdout.strip()


def inspect_container(name):
    return json.loads(docker('inspect', name))[0]


def atomic_json(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False))


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    try:
        with open(temp, 'x', encoding='utf-8') as stream:
            os.chmod(temp, 0o600)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        if os.name != 'nt':
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        if temp.exists():
            temp.unlink()


def state_root():
    identity = hashlib.sha256(os.getenv('AGENT_ID', '').encode()).hexdigest()[:16]
    return Path(os.getenv('AGENT_UPGRADE_DIR', '/data/.service-agent/upgrades')) / identity


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}


def job_state():
    return read_json(state_root() / 'job.json')


def save_job(root, job, status, error=None, **extra):
    job.update(extra, status=status, seq=int(job.get('seq', 0)) + 1, updatedAt=time.time())
    if error is not None:
        job['error'] = error
    atomic_json(Path(root) / 'job.json', job)


def image_repository(image):
    value = image.split('@')[0]
    return value.rsplit(':', 1)[0] if ':' in value.rsplit('/', 1)[-1] else value


def validate_image(image, current):
    if not isinstance(image, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._/:@-]{0,499}', image):
        raise ValueError('镜像地址格式不正确')
    if image_repository(image) != image_repository(current):
        raise ValueError('只能升级到当前 Agent 镜像仓库中的版本')
    if ':' not in image.rsplit('/', 1)[-1] and '@sha256:' not in image:
        raise ValueError('请指定镜像标签或 digest')
    return image


def mapped_path(path, mounts, to_host=False, require_write=True):
    """Docker labels 中的路径属于宿主机；按实际 bind mount 转换，禁止猜测路径。"""
    path = Path(path)
    origin, target = ('Destination', 'Source') if to_host else ('Source', 'Destination')
    for mount in sorted(mounts, key=lambda m: len(m.get(origin, '')), reverse=True):
        if mount.get('Type') != 'bind' or (require_write and not mount.get('RW')):
            continue
        try:
            suffix = path.relative_to(mount[origin])
            return str(Path(mount[target]) / suffix)
        except ValueError:
            continue
    raise ValueError('Agent 的部署目录和升级状态目录需要可写的宿主机挂载')


def own_container():
    return inspect_container(os.getenv('AGENT_CONTAINER_NAME') or os.getenv('HOSTNAME', ''))


def compose_selection(info):
    """返回 (Compose 标签信息, 最后一个定义本服务 image 的 (宿主机路径, 容器内路径))。"""
    labels = info['Config'].get('Labels') or {}
    files = labels.get('com.docker.compose.project.config_files', '').split(',')
    project_dir = labels.get('com.docker.compose.project.working_dir', '')
    service = labels.get('com.docker.compose.service')
    project = labels.get('com.docker.compose.project')
    if not all(f.startswith('/') for f in files) or not project_dir.startswith('/') or not service or not project:
        raise ValueError('此部署暂不支持自升级：未找到 Compose 部署信息')
    local_project = Path(mapped_path(project_dir, info['Mounts']))
    selected = None
    for host_path in files:
        candidate = Path(mapped_path(host_path, info['Mounts']))
        if candidate.is_symlink() or not candidate.is_file():
            raise ValueError('Compose 配置文件不可用')
        candidate.resolve().relative_to(local_project.resolve())
        document = yaml.safe_load(candidate.read_text(encoding='utf-8')) or {}
        config = document.get('services', {}).get(service)
        if isinstance(config, dict) and isinstance(config.get('image'), str):
            selected = (host_path, candidate)
    if selected is None:
        raise ValueError('Compose 中未找到 Agent 镜像配置')
    return {'files': files, 'projectDir': project_dir, 'service': service, 'project': project}, selected


def compose_image(path, service):
    document = yaml.safe_load(Path(path).read_text(encoding='utf-8')) or {}
    image = ((document.get('services') or {}).get(service) or {}).get('image')
    return image if isinstance(image, str) else None


def deployment_info():
    info = own_container()
    environment = dict(item.split('=', 1) for item in info['Config'].get('Env', []) if '=' in item)
    if environment.get('AGENT_ID', '') != os.getenv('AGENT_ID', '') or not os.getenv('AGENT_ID'):
        raise ValueError('无法确认当前 Agent 容器身份')
    meta, selected = compose_selection(info)
    files, project_dir, service, project = meta['files'], meta['projectDir'], meta['service'], meta['project']
    # 升级只改 image 这一行；无法定位时在能力探测阶段就拒绝，不能等切换后才失败
    replace_image_text(selected[1].read_text(encoding='utf-8'), service, 'orchidea-agent-probe:latest')
    host_file, compose_path = selected
    root = state_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    host_root = mapped_path(root, info['Mounts'], to_host=True)
    image = info['Config']['Image']
    previous = job_state()
    if image.startswith('orchidea-agent-rollback:') and info['Image'] == previous.get('previousImageId'):
        image = previous.get('previousImage') or image
    return {
        'containerId': info['Id'], 'containerName': info['Name'].lstrip('/'),
        'image': image, 'imageId': info['Image'],
        'project': project, 'service': service, 'projectDir': project_dir,
        'composeFile': host_file, 'composeFiles': files, 'localComposeFile': str(compose_path),
        'composeHash': hashlib.sha256(compose_path.read_bytes()).hexdigest(),
        'hostRoot': host_root, 'mounts': info['Mounts'],
    }


def runtime_info():
    """成功结果缓存到进程结束；失败（Docker 繁忙、配置正在编辑等）按间隔重新探测。"""
    global _runtime, _runtime_checked_at
    now = time.monotonic()
    if _runtime is None or (not _runtime.get('selfUpgrade') and now - _runtime_checked_at >= RUNTIME_RETRY_SECONDS):
        base = {'protocol': PROTOCOL, 'bootId': _boot_id, 'version': os.getenv('AGENT_VERSION', 'dev')}
        try:
            dep = deployment_info()
            base.update(image=dep['image'], imageId=dep['imageId'], selfUpgrade=True)
        except Exception as exc:
            base.update(selfUpgrade=False, reason=str(exc)[:250])
            # 不支持自升级也照常上报镜像身份：Hub 收敛丢失的升级记录要靠它
            try:
                info = own_container()
                base.update(image=info['Config']['Image'], imageId=info['Image'])
            except Exception:
                pass
        _runtime = base
        _runtime_checked_at = now
    return dict(_runtime)


def report(ws):
    # 读账本与发送在同一把锁内：心跳读到的旧任务快照不能晚于新任务的上报到达 Hub
    try:
        with _guard:
            state = job_state()
            fields = ('requestId', 'targetImage', 'targetImageId', 'previousImageId', 'status', 'seq', 'error')
            ws.send(json.dumps({'type': 'agent_report', 'runtime': runtime_info(),
                                'upgrade': {key: state[key] for key in fields if key in state}}))
    except Exception:
        # 重连/下一次心跳会补报；不在日志中输出敏感环境或原始 WS 地址。
        pass


def on_connected(ws):
    reconcile_stale_job()
    report(ws)
    if recover_launch():
        return
    job = job_state()
    # Agent 在等待服务任务/启动执行器之前被重建：恢复同一任务，不让 waiting 永久悬挂。
    if job.get('status') == 'waiting' and not _starting:
        try:
            helper = 'agent-upgrader-' + state_root().name
            existing = docker('ps', '-aq', '--filter', 'name=^/' + helper + '$')
            if existing and inspect_container(helper)['State']['Running']:
                return
            start_upgrade(ws, {'requestId': job['requestId'], 'image': job['targetImage']}, resume=True)
        except Exception:
            # Docker 暂不可用时保留任务；下一轮重连仍可恢复。
            pass


def recover_launch():
    """接管后不再由启动方写账本；用固定容器名恢复不确定的 create/start。

    job.json 自交接起只由执行器写。无论 inspect/run/start 的响应是否丢失，
    都不使用启动方的旧副本回写，避免检查后又推进的跨进程竞态。
    返回 True 表示已有本任务的交接记录，不能走旧的 waiting 重建流程。
    """
    with _guard:
        try:
            root = state_root()
            job = job_state()
            launch = read_json(root / 'launch.json')
            if not launch or launch.get('requestId') != job.get('requestId'):
                return False
            if job.get('status') != 'waiting':
                return True
            helper = 'agent-upgrader-' + root.name
            manifest = read_json(root / 'manifest.json')
            if launch.get('helper') != helper or manifest.get('requestId') != job['requestId']:
                raise ValueError('升级执行器交接记录不匹配')
            ids = docker('ps', '-aq', '--filter', 'name=^/' + helper + '$')
            if ids:
                info = inspect_container(helper)
                labels = info['Config'].get('Labels') or {}
                if (labels.get('orchidea.agent-upgrader') != root.name
                        or labels.get('orchidea.agent-upgrade-request') != job['requestId']):
                    raise ValueError('同名执行器不属于本次升级，保留任务等待确认')
                if not info['State'].get('Running') and info['State'].get('Status') in ('created', 'exited', 'dead'):
                    docker('start', helper)
            else:
                # 即使此前 run 仍在 daemon 中处理，相同名称也只允许创建一个容器。
                docker(*launch['args'])
        except Exception:
            # Docker 不可用/结果不确定都不是失败证据；保留账本并在心跳/重连时再核对。
            logger.warning('升级执行器启动结果尚未确认，保留任务并等待重试')
        return True


def reconcile_stale_job():
    """执行器已退出但任务停在非终态（unknown、中途被删、回退健康检查失败）时收敛到终态。

    否则 command_slot 会永久拒绝服务命令，Hub 也会一直占用升级位。仍在运行或刚更新过的任务不动。
    运行镜像读不到时保留任务；只有运行镜像、Compose 配置（及成功时的 Hub 确认）三者一致才判
    success / rolled_back，否则记 failed 并提示人工核对，不能把「配置已回退但仍跑新版」记成功。
    返回 True 表示本次改写了任务状态。
    """
    with _guard:
        try:
            job = job_state()
            status = job.get('status')
            if not status or status in TERMINAL or status == 'waiting' or _starting:
                return False
            if time.time() - float(job.get('updatedAt') or 0) < STALE_JOB_SECONDS:
                return False
            helper = 'agent-upgrader-' + state_root().name
            if docker('ps', '-aq', '--filter', 'name=^/' + helper + '$'):
                state = inspect_container(helper)['State']
                if state.get('Running') or state.get('Restarting') or state.get('Status') == 'restarting':
                    return False
            info = own_container()
            image_id = info.get('Image')
            if not image_id:
                return False
            root = state_root()
            try:
                meta, (_, local_file) = compose_selection(info)
                configured = compose_image(local_file, meta['service'])
                before = root / 'compose.before.yml'
                original = compose_image(before, meta['service']) if before.is_file() else None
            except Exception:
                configured, original = None, None
            points_old = bool(configured) and (configured in (original, job.get('previousImage'))
                                               or configured.startswith('orchidea-agent-rollback:'))
            confirmation = read_json(root / 'confirmed.json')
            confirmed = (confirmation.get('requestId') == job.get('requestId')
                         and confirmation.get('imageId') == job.get('targetImageId'))
            if image_id == job.get('targetImageId') and confirmed and configured and not points_old:
                save_job(root, job, 'success', '')
            elif image_id == job.get('previousImageId') and points_old:
                save_job(root, job, 'rolled_back', '升级未完成，当前运行的是升级前的 Agent')
            elif image_id == job.get('targetImageId'):
                save_job(root, job, 'failed', '升级未获 Hub 确认或 Compose 已改回旧版，但仍在运行新版 Agent；'
                                              '请在服务器核对 Compose 与运行镜像后处理')
            elif image_id == job.get('previousImageId'):
                save_job(root, job, 'failed', '仍在运行升级前的 Agent，但 Compose 已指向新版或无法读取；'
                                              '请在服务器核对 Compose 后处理')
            else:
                save_job(root, job, 'failed', '升级执行器已退出，当前 Agent 镜像与升级前后均不一致，请检查服务器')
            return True
        except Exception:
            # Docker 不可用时不能判断执行器是否仍在运行，保留任务等待下一次心跳
            logger.warning('升级任务状态暂时无法核对，等待下一次心跳')
            return False


@contextlib.contextmanager
def command_slot():
    global _active
    with _guard:
        if _starting or (job_state().get('status') and job_state()['status'] not in TERMINAL):
            raise RuntimeError('Agent 正在升级，暂不接受服务操作')
        _active += 1
    try:
        yield
    finally:
        with _guard:
            _active -= 1


def confirm(data):
    with _guard:
        job = job_state()
        if (job.get('status') == 'verifying' and data.get('requestId') == job.get('requestId')
                and data.get('imageId') == job.get('targetImageId')
                and runtime_info().get('imageId') == job.get('targetImageId')):
            atomic_json(state_root() / 'confirmed.json', {
                'requestId': job['requestId'], 'imageId': job['targetImageId'], 'bootId': _boot_id,
            })


def start_upgrade(ws, data, resume=False):
    global _starting
    request_id = data.get('requestId', '')
    if not isinstance(request_id, str) or not re.fullmatch(r'[a-f0-9-]{36}', request_id):
        return
    with _guard:
        previous = job_state()
        resuming = resume and previous.get('requestId') == request_id and previous.get('status') == 'waiting'
        if previous.get('requestId') == request_id and not resuming:
            report(ws)
            return
        if _starting or (previous.get('status') and previous['status'] not in TERMINAL and not resuming):
            report(ws)
            return
        _starting = True
    root = state_root()
    job = {'requestId': request_id, 'targetImage': data.get('image'), 'seq': previous.get('seq', 0) if resuming else 0}
    handed_off = False
    try:
        dep = deployment_info()
        validate_image(data.get('image'), dep['image'])
        job.update(previousImageId=dep['imageId'], previousImage=dep['image'])
        with _guard:
            save_job(root, job, 'waiting')
            report(ws)
        deadline = time.monotonic() + 600
        while True:
            with _guard:
                if not _active:
                    break
            if time.monotonic() >= deadline:
                raise RuntimeError('等待正在执行的服务操作超时，请稍后重试')
            time.sleep(1)
        helper_name = 'agent-upgrader-' + root.name
        # 上次已完成的辅助容器可删除；仍在运行则拒绝，不能并发切换。
        ids = docker('ps', '-aq', '--filter', 'name=^/' + helper_name + '$')
        if ids:
            old = inspect_container(helper_name)
            if old['State']['Running'] or old['Config'].get('Labels', {}).get('orchidea.agent-upgrader') != root.name:
                raise RuntimeError('升级执行器仍在运行，请等待它结束')
            docker('rm', helper_name)
        manifest = {**dep, 'root': dep['hostRoot'], 'requestId': request_id, 'targetImage': data['image']}
        manifest.pop('mounts')
        manifest.pop('localComposeFile')
        atomic_json(root / 'manifest.json', manifest)
        host_project_mount = next(m for m in dep['mounts'] if m.get('Type') == 'bind'
                                  and Path(dep['projectDir']).is_relative_to(m['Source']) and m.get('RW'))
        host_state_mount = next(m for m in dep['mounts'] if m.get('Type') == 'bind'
                                and Path(dep['hostRoot']).is_relative_to(m['Source']) and m.get('RW'))
        socket_mount = next(m for m in dep['mounts'] if m.get('Destination') == '/var/run/docker.sock')
        args = ['run', '-d', '--name', helper_name, '--restart', 'on-failure:3',
                '--label', 'orchidea.agent-upgrader=' + root.name,
                '--label', 'orchidea.agent-upgrade-request=' + request_id,
                '-v', socket_mount['Source'] + ':/var/run/docker.sock']
        for source in sorted({host_project_mount['Source'], host_state_mount['Source']}):
            args += ['-v', source + ':' + source]
        # 私有仓库配置可按部署显式挂载；不会把配置内容传给 Hub。
        auth_dir = os.getenv('DOCKER_CONFIG')
        if auth_dir:
            host_auth = mapped_path(auth_dir, dep['mounts'], to_host=True, require_write=False)
            args += ['-v', host_auth + ':/root/.docker:ro']
        args += ['--entrypoint', 'python', dep['imageId'], '-m', 'services.agent_upgrade',
                 '--run', dep['hostRoot'] + '/manifest.json']
        # 先持久化可重试的启动意图，再请求 Docker。发布交接记录后启动方永不写终态。
        # 标记必须早于 atomic_json：replace 后 fsync 抛错时记录也可能已对其它线程可见。
        handed_off = True
        atomic_json(root / 'launch.json', {'requestId': request_id, 'helper': helper_name, 'args': args})
        recover_launch()
        report(ws)
    except Exception as exc:
        # 交接记录未落盘（launch.json 写入失败）时执行器不会启动，必须由启动方写失败，否则任务永远 waiting
        if not handed_off or read_json(root / 'launch.json').get('requestId') != request_id:
            save_job(root, job, 'failed', str(exc)[:500])
        report(ws)
    finally:
        with _guard:
            _starting = False


def compose(manifest, *args):
    files = [arg for path in manifest['composeFiles'] for arg in ('-f', path)]
    return docker('compose', '--project-directory', manifest['projectDir'], '-p', manifest['project'],
                  *files, *args, timeout=180)


SERVICE_KEY = re.compile(r'(["\']?)([^"\':#\s][^"\':#]*)\1\s*:\s*(#.*)?')


def replace_image_text(text, service, image):
    """只替换 services.<service>.image 这一行，保留其余内容、注释和引号。

    整份 YAML 重新序列化会丢注释，并按 YAML 1.1 改写未加引号的值（如端口 22:22、on/off）。
    仅支持块样式 Compose；定位不到时抛错，由能力探测提前拒绝自升级。
    """
    lines = text.splitlines(keepends=True)
    in_services = False
    service_indent = None
    child_indent = None
    in_service = False
    for index, line in enumerate(lines):
        body = line.rstrip('\r\n')
        stripped = body.strip()
        if not stripped or stripped.startswith('#'):
            continue
        indent = len(body) - len(body.lstrip(' '))
        if indent == 0:
            in_services = re.fullmatch(r'services\s*:\s*(#.*)?', stripped) is not None
            service_indent = None
            in_service = False
            continue
        if not in_services:
            continue
        if service_indent is None or indent <= service_indent:
            match = SERVICE_KEY.fullmatch(stripped)
            if not match:
                continue
            service_indent = indent
            in_service = match.group(2).strip() == service
            child_indent = None
            continue
        if not in_service:
            continue
        if child_indent is None:
            child_indent = indent
        if indent == child_indent and re.match(r'image\s*:', stripped):
            comment = re.search(r'\s+#.*$', re.sub(r'"[^"]*"|\'[^\']*\'', lambda m: '_' * len(m.group(0)), body))
            tail = body[comment.start():] if comment else ''
            lines[index] = ' ' * indent + 'image: ' + json.dumps(image) + tail + line[len(body):]
            result = ''.join(lines)
            document = yaml.safe_load(result) or {}
            if ((document.get('services') or {}).get(service) or {}).get('image') == image:
                return result
            break
    raise ValueError('无法在 Compose 文件中定位 Agent 的 image 行，请改为块样式配置后再升级')


def write_image(manifest, image):
    path = Path(manifest['composeFile'])
    atomic_text(path, replace_image_text(path.read_text(encoding='utf-8'), manifest['service'], image))


# HTTP 错误码（含未连 Hub 的 503）也说明进程在响应；连接失败才抛错
LIVENESS_PROBE = (
    "import os,urllib.request,urllib.error\n"
    "try:urllib.request.urlopen('http://127.0.0.1:'+os.getenv('HEALTH_PORT','18081')+'/health',timeout=3)\n"
    "except urllib.error.HTTPError:pass"
)


def wait_ready(manifest, expected, root, confirm_hub, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            info = inspect_container(manifest['containerName'])
            if confirm_hub:
                healthy = info['State'].get('Health', {}).get('Status') == 'healthy'
                # 无 Docker healthcheck 的既有部署也必须检查 HTTP /health（含已连接 Hub）。
                if 'Health' not in info['State']:
                    docker('exec', manifest['containerName'], 'python', '-c',
                           "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'"
                           "+os.getenv('HEALTH_PORT','18081')+'/health',timeout=3)", timeout=8)
                    healthy = True
            else:
                # 回退只确认旧 Agent 进程已起来：/health 在未连上 Hub 时返回 503，Hub 故障期间不能据此判回退失败
                docker('exec', manifest['containerName'], 'python', '-c', LIVENESS_PROBE, timeout=8)
                healthy = True
            confirmation = read_json(Path(root) / 'confirmed.json')
            confirmed = (confirmation.get('requestId') == manifest['requestId']
                         and confirmation.get('imageId') == expected)
            if info['Image'] == expected and info['State']['Running'] and healthy and (not confirm_hub or confirmed):
                return True
        except (RuntimeError, ValueError, subprocess.TimeoutExpired):
            pass
        time.sleep(2)
    return False


def run_upgrade(manifest_path):
    """只在独立容器运行。重启执行器时依据落盘阶段补偿，不重复启动新升级。"""
    import fcntl
    manifest = read_json(manifest_path)
    root = Path(manifest['root'])
    with open(root / 'executor.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        job = read_json(root / 'job.json')
        if job.get('requestId') != manifest['requestId'] or job.get('status') in TERMINAL:
            return
        backup = root / 'compose.before.yml'
        switched = job.get('status') in ('switching', 'verifying', 'rolling_back', 'unknown')
        try:
            if switched:
                raise RuntimeError('升级执行器中断，恢复旧版本')
            save_job(root, job, 'pulling')
            # 保留旧镜像，避免拉取同名 latest 后旧 ID 被常规悬空镜像清理移除。
            docker('tag', manifest['imageId'], 'orchidea-agent-rollback:' + root.name)
            docker('pull', '--quiet', manifest['targetImage'], timeout=600)
            target = json.loads(docker('image', 'inspect', manifest['targetImage']))[0]
            target_id = target['Id']
            # digest 防止拉取后 tag 被其它任务覆盖；本地 Image ID 用于最终核对。
            digests = target.get('RepoDigests') or []
            repo = image_repository(manifest['targetImage'])
            pinned = next((d for d in digests if image_repository(d) == repo), None)
            if not pinned:
                raise RuntimeError('目标镜像未返回仓库 digest，无法固定升级版本')
            save_job(root, job, 'pulling', targetImageId=target_id)
            # 拉取期间如有人手工变更部署，停止升级，不能覆盖他人的新配置。
            if hashlib.sha256(Path(manifest['composeFile']).read_bytes()).hexdigest() != manifest['composeHash']:
                raise RuntimeError('下载期间 Compose 配置已被修改，请重新发起升级')
            # 备份先落盘，switching 必须早于任何配置/容器变更落盘。
            atomic_text(backup, Path(manifest['composeFile']).read_text(encoding='utf-8'))
            save_job(root, job, 'switching')
            switched = True
            write_image(manifest, pinned)
            compose(manifest, 'up', '-d', '--no-deps', '--pull', 'never', manifest['service'])
            save_job(root, job, 'verifying')
            if not wait_ready(manifest, target_id, root, True):
                raise RuntimeError('新版未在 3 分钟内通过健康检查并获得 Hub 确认')
            save_job(root, job, 'success', '')
        except Exception as exc:
            if not switched:
                save_job(root, job, 'failed', str(exc)[:500])
                return
            save_job(root, job, 'rolling_back', str(exc)[:500])
            try:
                # 不依赖旧 tag：它可能就是被覆盖的 latest。为旧 ID 保留一个本地标签。
                rollback_image = 'orchidea-agent-rollback:' + root.name
                docker('tag', manifest['imageId'], rollback_image)
                atomic_text(manifest['composeFile'], backup.read_text(encoding='utf-8'))
                # 已发布的旧版优先保留原 digest；无 digest 的首次接入才回退本地标签。
                rollback_config = manifest.get('image', '')
                write_image(manifest, rollback_config if '@sha256:' in rollback_config else rollback_image)
                compose(manifest, 'up', '-d', '--no-deps', '--pull', 'never', manifest['service'])
                # 配置保留旧镜像专用标签，下一次 compose up 也不会误拉失败的 latest。
                # 原始配置另存 compose.before.yml，供人工回溯。
                if not wait_ready(manifest, manifest['imageId'], root, False):
                    raise RuntimeError('旧 Agent 尚未恢复健康，请检查服务器')
                save_job(root, job, 'rolled_back')
            except Exception:
                save_job(root, job, 'unknown', '自动回退未确认完成，请检查服务器；升级记录已保留')


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', required=True)
    run_upgrade(parser.parse_args().run)


if __name__ == '__main__':
    main()
