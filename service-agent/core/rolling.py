import logging
import time

from core.handlers import send_message
from services import docker_cli, http_client, nacos_client
from services.instance_match import match_instance

logger = logging.getLogger(__name__)


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
        logger.error(f"list-instances 失败: {exc}")
        send_message(ws, {"type": "list-instances-result", "requestId": request_id,
                          "status": "failed", "error": str(exc)})


def _wait_ready(url, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if http_client.get_status(url) == 200:
            return True
        time.sleep(3)
    return False


def handle_graceful_restart(ws, data):
    request_id = data.get("requestId")
    container_id = data.get("containerId")
    base = data.get("healthBaseUrl")
    settle = int(data.get("settleSec", 35))
    shutdown_timeout = int(data.get("shutdownTimeoutSec", 60))
    ready_timeout = int(data.get("readyTimeoutSec", 180))
    try:
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
        logger.error(f"graceful-restart 失败: {exc}")
        send_message(ws, {"type": "graceful-restart-result", "requestId": request_id,
                          "status": "failed", "error": str(exc)})
