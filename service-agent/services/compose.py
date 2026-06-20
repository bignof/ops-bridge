import logging
import os
import subprocess
from pathlib import Path

import yaml

import config

logger = logging.getLogger(__name__)


def validate_managed_dir(project_dir):
    """节点控制目录安全闸（纯函数），command 路径与日志流路径共用。

    agent 挂载宿主 docker.sock，这里是防越权（任意目录 / `..` 穿越）与防自杀
    （操作 agent 自身 compose）的最终闸。返回 (ok: bool, reason: str | None)——
    ok=True 时 reason=None；ok=False 时 reason 为可直接回传客户端的拒绝原因。

    放本叶子模块（不引入环：compose 仅多依赖 config，config 不 import compose），
    供 handlers._validate_base 与 log_sessions.start_log_session 复用，逻辑保持单一来源。
    """
    real = os.path.realpath(project_dir)
    root = os.path.realpath(config.MANAGED_PROJECTS_ROOT)
    # ① realpath 归一后必须落在受管根之下；commonpath 在跨盘符/混绝对相对时会 raise ValueError，一律视为「在根外」拒绝。
    try:
        in_root = os.path.commonpath([real, root]) == root
    except ValueError:
        in_root = False
    if not in_root:
        return False, f"dir 不在受管目录 {root} 内: {project_dir}"

    # ② 拒绝命中 agent 自身 compose 目录（含其子目录）；同样把 ValueError 兜底为「命中」以从严拒绝。
    if config.SELF_PROJECT_DIR:
        self_dir = os.path.realpath(config.SELF_PROJECT_DIR)
        try:
            hits_self = os.path.commonpath([real, self_dir]) == self_dir
        except ValueError:
            hits_self = True
        if hits_self:
            return False, "禁止操作 agent 自身 project"

    return True, None


def _image_registry_host(image):
    """提取镜像的 registry 主机分量（按 Docker 规则）。

    单段镜像（不含 `/`，如 `nginx:latest`）一律是 docker.io 官方库——首段里的 `:` 是 tag 而非端口，
    不能当 registry 主机。仅当镜像含 `/` 且首段含 `.` 或 `:` 或等于 `localhost` 时，首段才算 registry 主机；
    否则（如 `library/nginx`）首段是 docker.io 上的命名空间，registry 视为 `docker.io`。
    """
    if '/' not in image:
        return 'docker.io'
    first = image.split('/', 1)[0]
    if '.' in first or ':' in first or first == 'localhost':
        return first
    return 'docker.io'


def is_image_registry_allowed(image, allowlist):
    """校验镜像来源是否在白名单内（按 registry 边界匹配，绝不裸 startswith）。

    - 空 allowlist → True（不限制，放行全部）。
    - 非空 allowlist：满足任一即放行——
      ① 镜像的 registry 主机分量 == 某 allowlist 项（精确相等）；
      ② image == prefix（精确相等）或 image.startswith(prefix + '/')（带 `/` 边界，防 `foo` 误放 `foo-evil`）。

    反例必须被拒：allowlist=["registry.example.com"] 时 "registry.example.com.evil/x:1" → False。
    """
    if not allowlist:
        return True

    registry_host = _image_registry_host(image)
    for prefix in allowlist:
        if registry_host == prefix:
            return True
        if image == prefix or image.startswith(prefix + '/'):
            return True
    return False


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
