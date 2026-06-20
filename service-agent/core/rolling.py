import logging
import re
import time

from core.graceful import _validate_health_base_url, shutdown_headers
from core.handlers import send_message
from services import docker_cli, http_client, nacos_client
from services.instance_match import compose_project, match_instance

logger = logging.getLogger(__name__)

# 用于把日志/回传文本里的 nacos accessToken 脱敏（H2 纵深防御）
_TOKEN_RE = re.compile(r"accessToken=[^&\s]+")

# _validate_health_base_url 已抽到 core.graceful（叶子模块），此处 re-export 供
# 既有调用方 / 测试以 rolling._validate_health_base_url 解析；优雅 stop / pull-redeploy 复用同一守卫。


def _redact(text):
    """脱敏：把 accessToken=xxx 替换成 accessToken=***，避免 token 进日志或回 hub。"""
    return _TOKEN_RE.sub("accessToken=***", str(text))


def handle_list_instances(ws, data):
    request_id = data.get("requestId")
    service_name = data.get("serviceName")
    # 可选：上层（BFF/hub）传期望的 compose 工程名做寻址漂移校验；不传则不比对（向后兼容：
    # 现有 hub 滚动重启编排不传此字段，行为完全不变，仅回包多一个 composeProject 字段）。
    expected = data.get("expectedComposeProject")
    try:
        instances = nacos_client.list_healthy_instances(service_name)
        containers = docker_cli.list_running_containers()
        result = []
        for inst in instances:
            container = match_instance(inst, containers)
            proj = compose_project(container) if container else None
            matched = container is not None
            # 容器可寻址但其 compose 工程名与期望不符 → 寻址漂移，标 matched=False，
            # 让上层据此拒绝（优雅按实例、force 按目录会作用到不同容器组，危险）。
            if matched and expected and proj != expected:
                matched = False
            result.append({
                "address": f"{inst['ip']}:{inst['port']}",
                "containerId": container["Id"][:12] if container else None,
                "healthy": True,
                "matched": matched,
                "composeProject": proj,
            })
        send_message(ws, {"type": "list-instances-result", "requestId": request_id,
                          "status": "success", "instances": result})
    except Exception as exc:
        safe_error = _redact(exc)
        logger.error(f"list-instances 失败: {safe_error}")
        send_message(ws, {"type": "list-instances-result", "requestId": request_id,
                          "status": "failed", "error": safe_error})


def _wait_ready(url, timeout):
    # 至少探一次：timeout<=0 时不能直接判失败（节点可能已 ready），见 L1
    deadline = time.time() + timeout
    while True:
        if http_client.get_status(url) == 200:
            return True
        if time.time() >= deadline:
            return False
        time.sleep(3)


def handle_graceful_restart(ws, data):
    request_id = data.get("requestId")
    container_id = data.get("containerId")
    base = data.get("healthBaseUrl")
    settle = int(data.get("settleSec", 35))
    shutdown_timeout = int(data.get("shutdownTimeoutSec", 60))
    ready_timeout = int(data.get("readyTimeoutSec", 180))
    try:
        # H1：先校验 healthBaseUrl，非法/公网地址直接拒绝，绝不发出 shutdown / restart
        try:
            _validate_health_base_url(base)
        except ValueError as ve:
            raise RuntimeError(f"healthBaseUrl 非法或非内网地址: {ve}") from None
        # 方案 A：配了 K8S_SHUTDOWN_TOKEN 才带凭据头，未配则不传 headers 关键字（向后兼容）
        _hdrs = shutdown_headers()
        code, _text = http_client.post(
            f"{base}/api/k8s/shutdown",
            timeout=shutdown_timeout,
            **({"headers": _hdrs} if _hdrs else {}),
        )
        if code != 200:
            raise RuntimeError(f"shutdown 返回 {code}")
        ok, out = docker_cli.restart_container(container_id)
        if not ok:
            raise RuntimeError(f"docker restart 失败: {out}")
        if not _wait_ready(f"{base}/api/health/ready", ready_timeout):
            raise RuntimeError("节点未在超时内 ready")
        time.sleep(settle)
        send_message(ws, {"type": "graceful-restart-result", "requestId": request_id, "status": "success"})
    except Exception as exc:
        safe_error = _redact(exc)
        logger.error(f"graceful-restart 失败: {safe_error}")
        send_message(ws, {"type": "graceful-restart-result", "requestId": request_id,
                          "status": "failed", "error": safe_error})
