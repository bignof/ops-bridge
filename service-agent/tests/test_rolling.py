import os
os.environ.setdefault("WS_URL", "ws://test")
os.environ.setdefault("AGENT_KEY", "test-key")

import json
import time

from core import rolling
from services import nacos_client, docker_cli, http_client


class FakeWS:
    def __init__(self):
        self.sent = []

    def send(self, payload):
        self.sent.append(json.loads(payload))


def test_list_instances_success(monkeypatch):
    monkeypatch.setattr(nacos_client, "list_healthy_instances",
                        lambda s: [{"ip": "192.168.0.30", "port": 18029}])
    monkeypatch.setattr(docker_cli, "list_running_containers",
                        lambda: [{"Id": "abcdef1234567890", "NetworkSettings":
                                  {"Ports": {"80/tcp": [{"HostPort": "18029"}]}, "Networks": {}}}])
    ws = FakeWS()
    rolling.handle_list_instances(ws, {"requestId": "r1", "serviceName": "memory-share"})
    msg = ws.sent[-1]
    assert msg["type"] == "list-instances-result" and msg["status"] == "success"
    assert msg["instances"] == [{"address": "192.168.0.30:18029",
                                 "containerId": "abcdef123456", "healthy": True, "matched": True}]


def test_list_instances_unmatched_flagged(monkeypatch):
    monkeypatch.setattr(nacos_client, "list_healthy_instances",
                        lambda s: [{"ip": "10.9.9.9", "port": 9999}])
    monkeypatch.setattr(docker_cli, "list_running_containers", lambda: [])
    ws = FakeWS()
    rolling.handle_list_instances(ws, {"requestId": "r1", "serviceName": "svc"})
    inst = ws.sent[-1]["instances"][0]
    assert inst["matched"] is False and inst["containerId"] is None


def test_list_instances_failure(monkeypatch):
    def boom(s):
        raise RuntimeError("nacos down")
    monkeypatch.setattr(nacos_client, "list_healthy_instances", boom)
    ws = FakeWS()
    rolling.handle_list_instances(ws, {"requestId": "r1", "serviceName": "svc"})
    assert ws.sent[-1]["status"] == "failed" and "nacos down" in ws.sent[-1]["error"]


def test_graceful_restart_success(monkeypatch):
    monkeypatch.setattr(http_client, "post", lambda url, timeout=60: (200, "ok"))
    monkeypatch.setattr(docker_cli, "restart_container", lambda cid, timeout=120: (True, "ok"))
    monkeypatch.setattr(http_client, "get_status", lambda url, timeout=5: 200)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    ws = FakeWS()
    rolling.handle_graceful_restart(ws, {"requestId": "g1", "containerId": "abc",
        "healthBaseUrl": "http://192.168.0.30:18029", "settleSec": 1,
        "shutdownTimeoutSec": 60, "readyTimeoutSec": 10})
    assert ws.sent == [{"type": "graceful-restart-result", "requestId": "g1", "status": "success"}]


def test_graceful_restart_shutdown_fail(monkeypatch):
    monkeypatch.setattr(http_client, "post", lambda url, timeout=60: (500, "err"))
    ws = FakeWS()
    rolling.handle_graceful_restart(ws, {"requestId": "g1", "containerId": "abc",
        "healthBaseUrl": "http://x", "settleSec": 0, "shutdownTimeoutSec": 60, "readyTimeoutSec": 10})
    assert ws.sent[-1]["status"] == "failed" and "shutdown" in ws.sent[-1]["error"]


def test_graceful_restart_not_ready(monkeypatch):
    monkeypatch.setattr(http_client, "post", lambda url, timeout=60: (200, "ok"))
    monkeypatch.setattr(docker_cli, "restart_container", lambda cid, timeout=120: (True, "ok"))
    monkeypatch.setattr(http_client, "get_status", lambda url, timeout=5: 503)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    # 让 _wait_ready 立即超时：把 time.time 固定推进
    # 多喂几个:_wait_ready 用 3 次,失败分支 logger.error→LogRecord.__init__ 还会再调 time.time()
    seq = iter([1000.0, 1000.0, 2000.0, 2000.0, 2000.0])
    monkeypatch.setattr(time, "time", lambda: next(seq))
    ws = FakeWS()
    rolling.handle_graceful_restart(ws, {"requestId": "g1", "containerId": "abc",
        "healthBaseUrl": "http://x", "settleSec": 0, "shutdownTimeoutSec": 60, "readyTimeoutSec": 1})
    assert ws.sent[-1]["status"] == "failed" and "ready" in ws.sent[-1]["error"]
