"""
app_http.py — 与本机 NocoBase 应用（同机容器，经宿主机端口映射访问）对话的两个 HTTP 原语。

drain：优雅停机（调用 @orchisky/plugin-service-k8s 的 /api/k8s/shutdown）。
wait_healthy：轮询 /api/health/ready 直到应用自报可以接流量。

两者都不知道"端口怎么来的"——调用方（core/handlers.py）负责先用
compose.resolve_container_port_mapping 解出端口，解不出直接失败，不传到这里。

host 用 config.APP_HOST（默认 host.docker.internal）而不是 127.0.0.1：agent 自己也是容器，
跟被管的业务容器各在独立的 bridge 网络命名空间里，127.0.0.1 只是各自的回环、互相连不到。
"""
import time

import requests
from urllib3.exceptions import NewConnectionError

from config import APP_HOST

DEFAULT_DRAIN_TIMEOUT_SEC = 60
DEFAULT_HEALTHCHECK_TIMEOUT_SEC = 120
DEFAULT_HEALTHCHECK_INTERVAL_SEC = 2
READINESS_PROBE_TIMEOUT_SEC = 5

# drain_outcome 的三种结果
DRAINED = 'drained'      # 应用确认已下线
DRAIN_FAILED = 'failed'  # 请求肯定没有生效：连不上，或应用在动手之前就拒绝了（4xx）
DRAIN_UNKNOWN = 'unknown'  # 请求可能已送达，但没拿到明确结果


def _never_connected(exc):
    """连接都没建立（拒绝、域名解析失败、连接超时），请求肯定没送到应用。"""
    if isinstance(exc, requests.ConnectTimeout):
        return True
    reason = exc.args[0] if exc.args else None
    return isinstance(getattr(reason, 'reason', reason), NewConnectionError)


def drain_outcome(port, token=None, timeout=DEFAULT_DRAIN_TIMEOUT_SEC):
    """POST /api/k8s/shutdown 到本机指定端口，返回 (outcome, message)，不抛异常。

    下线不可撤销：plugin-service-k8s 收到请求先打关闭标志（ready 立即 503），再注销 Nacos、停定时任务、
    等待在途请求，全部做完才回 200。所以读超时、回包中断、5xx、应用回 success=false 时，实例可能已经
    不接流量，只能算 unknown，不能说「实例保持原状」。"""
    headers = {'X-Shutdown-Token': token} if token else {}
    try:
        resp = requests.post(f'http://{APP_HOST}:{port}/api/k8s/shutdown', headers=headers, timeout=timeout)
    except requests.RequestException as e:
        if isinstance(e, requests.ConnectionError) and _never_connected(e):
            return DRAIN_FAILED, str(e)
        if isinstance(e, (requests.Timeout, requests.ConnectionError)):
            return DRAIN_UNKNOWN, str(e)
        return DRAIN_FAILED, str(e)
    except Exception as e:  # 防御：任何意外都不能让命令线程无声退出
        return DRAIN_UNKNOWN, str(e)

    if resp.status_code >= 500:
        return DRAIN_UNKNOWN, f'unexpected status {resp.status_code}: {resp.text}'
    if resp.status_code != 200:
        return DRAIN_FAILED, f'unexpected status {resp.status_code}: {resp.text}'

    try:
        body = resp.json()
    except ValueError:
        return DRAINED, 'drained (non-JSON response)'
    if not isinstance(body, dict):
        return DRAINED, 'drained'
    if body.get('success') is False:
        # 应用在下线过程中出错：关闭标志通常已经打上
        return DRAIN_UNKNOWN, body.get('message') or 'app reported failure'
    return DRAINED, body.get('message') or 'drained'


def drain(port, token=None, timeout=DEFAULT_DRAIN_TIMEOUT_SEC):
    """POST /api/k8s/shutdown 到本机指定端口。返回 (ok, message)，ok 只在应用确认已下线时为 True。"""
    outcome, message = drain_outcome(port, token=token, timeout=timeout)
    return outcome == DRAINED, message


def readiness_state(port, timeout=READINESS_PROBE_TIMEOUT_SEC):
    """单次探测 /api/health/ready：200 → ready，503 → not_ready（关闭标志已打上或应用不健康），
    连不上或其它状态码 → unknown。"""
    try:
        resp = requests.get(f'http://{APP_HOST}:{port}/api/health/ready', timeout=timeout)
    except requests.RequestException:
        return 'unknown'
    if resp.status_code == 200:
        return 'ready'
    if resp.status_code == 503:
        return 'not_ready'
    return 'unknown'


def wait_healthy(port, timeout=DEFAULT_HEALTHCHECK_TIMEOUT_SEC, interval=DEFAULT_HEALTHCHECK_INTERVAL_SEC):
    """轮询 /api/health/ready 直到 200 或超时。返回 (ok, message)。"""
    deadline = time.monotonic() + timeout
    last_error = 'no attempt made'
    while time.monotonic() < deadline:
        try:
            resp = requests.get(f'http://{APP_HOST}:{port}/api/health/ready', timeout=5)
            if resp.status_code == 200:
                return True, 'healthy'
            last_error = f'status {resp.status_code}'
        except requests.RequestException as e:
            last_error = str(e)
        time.sleep(interval)
    return False, f'healthcheck timed out after {timeout}s: {last_error}'
