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


def run_compose(project_dir, args):
    """在 project_dir 下执行 compose 子命令，返回 (success: bool, output: str)。"""
    cmd = _get_compose_cmd() + args
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, cwd=project_dir)
    return result.returncode == 0, result.stdout + result.stderr
