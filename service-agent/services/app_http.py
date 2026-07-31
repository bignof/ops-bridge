"""
app_http.py — 与本机 NocoBase 应用（同机容器，经宿主机端口回环访问）对话的两个 HTTP 原语。

drain：优雅停机（调用 @orchisky/plugin-service-k8s 的 /api/k8s/shutdown）。
wait_healthy：轮询 /api/health/ready 直到应用自报可以接流量。

两者都不知道"端口怎么来的"——调用方（core/handlers.py）负责先用
compose.resolve_container_port_mapping 解出端口，解不出直接失败，不传到这里。
"""
import time

import requests

DEFAULT_DRAIN_TIMEOUT_SEC = 60
DEFAULT_HEALTHCHECK_TIMEOUT_SEC = 120
DEFAULT_HEALTHCHECK_INTERVAL_SEC = 2


def drain(port, token=None, timeout=DEFAULT_DRAIN_TIMEOUT_SEC):
    """POST /api/k8s/shutdown 到本机指定端口。返回 (ok, message)。"""
    headers = {'X-Shutdown-Token': token} if token else {}
    try:
        resp = requests.post(f'http://127.0.0.1:{port}/api/k8s/shutdown', headers=headers, timeout=timeout)
    except requests.RequestException as e:
        return False, str(e)

    if resp.status_code != 200:
        return False, f'unexpected status {resp.status_code}: {resp.text}'

    try:
        body = resp.json()
    except ValueError:
        return True, 'drained (non-JSON response)'

    if body.get('success') is False:
        return False, body.get('message') or 'app reported failure'
    return True, body.get('message') or 'drained'


def wait_healthy(port, timeout=DEFAULT_HEALTHCHECK_TIMEOUT_SEC, interval=DEFAULT_HEALTHCHECK_INTERVAL_SEC):
    """轮询 /api/health/ready 直到 200 或超时。返回 (ok, message)。"""
    deadline = time.monotonic() + timeout
    last_error = 'no attempt made'
    while time.monotonic() < deadline:
        try:
            resp = requests.get(f'http://127.0.0.1:{port}/api/health/ready', timeout=5)
            if resp.status_code == 200:
                return True, 'healthy'
            last_error = f'status {resp.status_code}'
        except requests.RequestException as e:
            last_error = str(e)
        time.sleep(interval)
    return False, f'healthcheck timed out after {timeout}s: {last_error}'
