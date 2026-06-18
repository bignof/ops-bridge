import os
os.environ.setdefault("WS_URL", "ws://test")
os.environ.setdefault("AGENT_KEY", "test-key")

import json

from core import rolling
from services import nacos_client, docker_cli


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
