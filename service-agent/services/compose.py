import json
import logging
import os
import subprocess
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


def get_compose_cmd():
    """
    仅使用 Docker Compose v2 插件。
    """
    try:
        result = subprocess.run(['docker', 'compose', 'version'], capture_output=True, timeout=5)
        if result.returncode == 0:
            logger.info("Using 'docker compose' (v2 plugin).")
            return ['docker', 'compose']
    except Exception:
        pass

    raise RuntimeError("'docker compose' (v2 plugin) is required but unavailable.")


_compose_cmd = None


def _get_compose_cmd():
    global _compose_cmd
    if _compose_cmd is None:
        _compose_cmd = get_compose_cmd()
    return _compose_cmd


def find_compose_file(project_dir):
    """按常见文件名在 project_dir 下查找 compose 文件，找不到返回 None。"""
    for name in ('docker-compose.yaml', 'docker-compose.yml'):
        path = os.path.join(project_dir, name)
        if os.path.isfile(path):
            return path
    return None


def read_compose_file(compose_file):
    return Path(compose_file).read_text(encoding='utf-8')


def restore_compose_file(compose_file, original_content):
    Path(compose_file).write_text(original_content, encoding='utf-8')


def update_image_in_compose(compose_file, new_image):
    """
    将 compose 文件中与 new_image 同仓库（忽略 tag）的服务镜像更新为 new_image。
    返回被更新的服务名列表。
    """
    content = yaml.safe_load(read_compose_file(compose_file)) or {}

    new_repo = new_image.rsplit(':', 1)[0]
    updated = []

    for svc_name, svc_cfg in (content.get('services') or {}).items():
        if not isinstance(svc_cfg, dict):
            continue
        current_image = svc_cfg.get('image', '')
        if current_image.rsplit(':', 1)[0] == new_repo:
            svc_cfg['image'] = new_image
            updated.append(svc_name)
            logger.info(f"Updated service '{svc_name}': {current_image} -> {new_image}")

    if updated:
        with open(compose_file, 'w', encoding='utf-8', newline='\n') as f:
            yaml.safe_dump(content, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    return updated


def collect_service_statuses(compose_dir):
    """
    在 compose_dir 下采集所有 service 的真实运行状态：
    docker compose ps --format json（每行一个 service 的 JSON，NDJSON）+
    对每个拿到的容器 ID 补一次 docker inspect 拿精确 StartedAt（compose ps 本身不给机器可比较的时间戳，
    只有 RunningFor/Status 这类人话字符串）。
    compose ps 本身失败（目录没有 compose 文件/容器未创建）返回空列表，调用方按"这轮跳过"处理。
    单个容器 inspect 失败只影响该 service 的 startedAt（置 None），不影响其它字段、不影响其它 service。
    """
    cmd = _get_compose_cmd() + ['ps', '--format', 'json']
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, cwd=compose_dir)
    except Exception:
        return []
    if result.returncode != 0:
        return []

    services = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        container_id = entry.get('ID')
        started_at = None
        if container_id:
            try:
                inspect_result = subprocess.run(
                    ['docker', 'inspect', '--format', '{{.State.StartedAt}}', container_id],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if inspect_result.returncode == 0:
                    started_at = inspect_result.stdout.strip() or None
            except Exception:
                started_at = None
        services.append(
            {
                'name': entry.get('Service'),
                'image': entry.get('Image'),
                'state': entry.get('State'),
                'startedAt': started_at,
                'containerName': entry.get('Name'),
                'containerId': container_id,
                'raw': entry,
            }
        )
    return services


def resolve_container_port_mapping(compose_file, container_port='80'):
    """
    在 compose 文件里查找映射到指定容器端口的宿主机端口。
    只支持短语法字符串（'host:container'、'ip:host:container'，可带 '/tcp' 后缀），
    找不到匹配、格式不支持（如只写容器端口没写宿主机端口）时返回 None——
    调用方必须把 None 当失败处理，不允许静默降级。
    """
    content = yaml.safe_load(read_compose_file(compose_file)) or {}
    for svc_cfg in (content.get('services') or {}).values():
        if not isinstance(svc_cfg, dict):
            continue
        for entry in (svc_cfg.get('ports') or []):
            if not isinstance(entry, str):
                continue
            parts = entry.split(':')
            if len(parts) < 2:
                continue
            container_part = parts[-1].split('/')[0]
            if container_part != str(container_port):
                continue
            try:
                return int(parts[-2])
            except ValueError:
                continue
    return None


def run_compose(project_dir, args):
    """在 project_dir 下执行 compose 子命令，返回 (success: bool, output: str)。"""
    cmd = _get_compose_cmd() + args
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, cwd=project_dir)
    return result.returncode == 0, result.stdout + result.stderr


def open_compose_process(project_dir, args):
    """在 project_dir 下启动 compose 子进程，适合持续输出场景。"""
    cmd = _get_compose_cmd() + args
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=project_dir,
        bufsize=1,
    )
