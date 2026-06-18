import ipaddress
import logging
import re
import time
from urllib.parse import urlparse

from core.handlers import send_message
from services import docker_cli, http_client, nacos_client
from services.instance_match import match_instance

logger = logging.getLogger(__name__)

# 用于把日志/回传文本里的 nacos accessToken 脱敏（H2 纵深防御）
_TOKEN_RE = re.compile(r"accessToken=[^&\s]+")


def _redact(text):
    """脱敏：把 accessToken=xxx 替换成 accessToken=***，避免 token 进日志或回 hub。"""
    return _TOKEN_RE.sub("accessToken=***", str(text))


def _validate_health_base_url(base):
    """
    校验 graceful-restart 的 healthBaseUrl，防 SSRF（H1）。
    要求：scheme ∈ {http, https}，且 host 必须是非公网（private/loopback/link-local）IP。
    非法时抛 ValueError（由调用方转成 failed 结果，不冒泡）。
    """
    if not base:
        raise ValueError("healthBaseUrl 为空")
    parsed = urlparse(base)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"scheme 非法（仅允许 http/https）: {parsed.scheme or '空'}")
    host = parsed.hostname
    if not host:
        raise ValueError("缺少 host")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # 非 IP（如域名）一律拒绝，避免 DNS 解析到公网
        raise ValueError(f"host 必须是内网 IP，非域名: {host}")
    if ip.is_global:
        raise ValueError(f"host 为公网可路由地址，禁止访问: {host}")
    return base


def handle_list_instances(ws, data):
    request_id = data.get("requestId")
    service_name = data.get("serviceName")
    try:
        instances = nacos_client.list_healthy_instances(service_name)
        containers = docker_cli.list_running_containers()
        result = []
        for inst in instances:
            container = match_instance(inst, containers)
            result.append({
                "address": f"{inst['ip']}:{inst['port']}",
                "containerId": container["Id"][:12] if container else None,
                "healthy": True,
                "matched": container is not None,
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
        code, _text = http_client.post(f"{base}/api/k8s/shutdown", timeout=shutdown_timeout)
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
