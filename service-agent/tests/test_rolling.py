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
    # L6：用记录型 sleep 桩，断言 settle 步骤确实跑了（settle 是零中断承重步骤，删了必须被测到）
    recorded = []
    monkeypatch.setattr(time, "sleep", lambda s: recorded.append(s))
    ws = FakeWS()
    rolling.handle_graceful_restart(ws, {"requestId": "g1", "containerId": "abc",
        "healthBaseUrl": "http://192.168.0.30:18029", "settleSec": 7,
        "shutdownTimeoutSec": 60, "readyTimeoutSec": 10})
    assert ws.sent == [{"type": "graceful-restart-result", "requestId": "g1", "status": "success"}]
    assert 7 in recorded


def test_graceful_restart_shutdown_fail(monkeypatch):
    monkeypatch.setattr(http_client, "post", lambda url, timeout=60: (500, "err"))
    ws = FakeWS()
    rolling.handle_graceful_restart(ws, {"requestId": "g1", "containerId": "abc",
        "healthBaseUrl": "http://192.168.0.30:18029", "settleSec": 0,
        "shutdownTimeoutSec": 60, "readyTimeoutSec": 10})
    assert ws.sent[-1]["status"] == "failed" and "shutdown" in ws.sent[-1]["error"]


def test_graceful_restart_restart_fail(monkeypatch):
    # M1：docker restart 失败分支（rolling.py:56），shutdown 成功但 restart 返回失败
    monkeypatch.setattr(http_client, "post", lambda url, timeout=60: (200, "ok"))
    monkeypatch.setattr(docker_cli, "restart_container",
                        lambda cid, timeout=120: (False, "no such container"))
    monkeypatch.setattr(http_client, "get_status", lambda url, timeout=5: 200)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    ws = FakeWS()
    rolling.handle_graceful_restart(ws, {"requestId": "g1", "containerId": "abc",
        "healthBaseUrl": "http://192.168.0.30:18029", "settleSec": 0,
        "shutdownTimeoutSec": 60, "readyTimeoutSec": 10})
    assert ws.sent[-1]["status"] == "failed" and "docker restart" in ws.sent[-1]["error"]


def test_graceful_restart_not_ready(monkeypatch):
    monkeypatch.setattr(http_client, "post", lambda url, timeout=60: (200, "ok"))
    monkeypatch.setattr(docker_cli, "restart_container", lambda cid, timeout=120: (True, "ok"))
    monkeypatch.setattr(http_client, "get_status", lambda url, timeout=5: 503)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    # L5：用单调递增 clock，不耦合 logging 内部对 time.time 的调用次数；
    # 每次 +1000 会很快越过 deadline，_wait_ready 在有限步内超时返回 False
    t = {"v": 1000.0}
    def clock():
        t["v"] += 1000
        return t["v"]
    monkeypatch.setattr(time, "time", clock)
    ws = FakeWS()
    rolling.handle_graceful_restart(ws, {"requestId": "g1", "containerId": "abc",
        "healthBaseUrl": "http://192.168.0.30:18029", "settleSec": 0,
        "shutdownTimeoutSec": 60, "readyTimeoutSec": 1})
    assert ws.sent[-1]["status"] == "failed" and "ready" in ws.sent[-1]["error"]


def test_graceful_restart_ready_timeout_zero_probes_once(monkeypatch):
    # L1：readyTimeoutSec=0 但节点已 ready（get_status→200），应至少探一次并成功
    monkeypatch.setattr(http_client, "post", lambda url, timeout=60: (200, "ok"))
    monkeypatch.setattr(docker_cli, "restart_container", lambda cid, timeout=120: (True, "ok"))
    monkeypatch.setattr(http_client, "get_status", lambda url, timeout=5: 200)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    ws = FakeWS()
    rolling.handle_graceful_restart(ws, {"requestId": "g1", "containerId": "abc",
        "healthBaseUrl": "http://192.168.0.30:18029", "settleSec": 0,
        "shutdownTimeoutSec": 60, "readyTimeoutSec": 0})
    assert ws.sent[-1]["status"] == "success"


def _recording_stubs(monkeypatch):
    """装一组记录型桩：若 H1 校验未拦截，post/restart 会被记录，便于断言『未触发』。"""
    calls = {"post": 0, "restart": 0}
    def rec_post(url, timeout=60):
        calls["post"] += 1
        return (200, "ok")
    def rec_restart(cid, timeout=120):
        calls["restart"] += 1
        return (True, "ok")
    monkeypatch.setattr(http_client, "post", rec_post)
    monkeypatch.setattr(docker_cli, "restart_container", rec_restart)
    monkeypatch.setattr(http_client, "get_status", lambda url, timeout=5: 200)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return calls


def test_graceful_restart_rejects_public_ip(monkeypatch):
    # H1：公网 IP 必须被拒，且不触发 shutdown(post) / restart
    calls = _recording_stubs(monkeypatch)
    ws = FakeWS()
    rolling.handle_graceful_restart(ws, {"requestId": "g1", "containerId": "abc",
        "healthBaseUrl": "http://8.8.8.8:18029", "settleSec": 0,
        "shutdownTimeoutSec": 60, "readyTimeoutSec": 10})
    assert ws.sent[-1]["status"] == "failed"
    assert "内网" in ws.sent[-1]["error"] or "公网" in ws.sent[-1]["error"]
    assert calls["post"] == 0 and calls["restart"] == 0


def test_graceful_restart_rejects_bad_scheme(monkeypatch):
    # H1：非法 scheme（file://）必须被拒，且不触发 shutdown / restart
    calls = _recording_stubs(monkeypatch)
    ws = FakeWS()
    rolling.handle_graceful_restart(ws, {"requestId": "g1", "containerId": "abc",
        "healthBaseUrl": "file:///etc/passwd", "settleSec": 0,
        "shutdownTimeoutSec": 60, "readyTimeoutSec": 10})
    assert ws.sent[-1]["status"] == "failed"
    assert calls["post"] == 0 and calls["restart"] == 0


def test_graceful_restart_rejects_domain(monkeypatch):
    # H1：域名（非 IP）一律拒绝，防 DNS 解析到公网；不触发 shutdown / restart
    calls = _recording_stubs(monkeypatch)
    ws = FakeWS()
    rolling.handle_graceful_restart(ws, {"requestId": "g1", "containerId": "abc",
        "healthBaseUrl": "http://evil.example.com:18029", "settleSec": 0,
        "shutdownTimeoutSec": 60, "readyTimeoutSec": 10})
    assert ws.sent[-1]["status"] == "failed"
    assert calls["post"] == 0 and calls["restart"] == 0


def test_graceful_restart_allows_loopback(monkeypatch):
    # H1：loopback 等内网地址应放行（成功路径），确认校验没误杀合法内网
    calls = _recording_stubs(monkeypatch)
    ws = FakeWS()
    rolling.handle_graceful_restart(ws, {"requestId": "g1", "containerId": "abc",
        "healthBaseUrl": "http://127.0.0.1:18029", "settleSec": 0,
        "shutdownTimeoutSec": 60, "readyTimeoutSec": 10})
    assert ws.sent[-1]["status"] == "success"
    assert calls["post"] == 1 and calls["restart"] == 1


def test_graceful_restart_error_redacts_token(monkeypatch):
    # H2 纵深防御：即便底层异常串里带 accessToken，回 hub 的 error 也必须脱敏
    monkeypatch.setattr(http_client, "post", lambda url, timeout=60: (200, "ok"))
    def boom(cid, timeout=120):
        raise RuntimeError("失败 url=http://x/list?accessToken=SECRET&k=1")
    monkeypatch.setattr(docker_cli, "restart_container", boom)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    ws = FakeWS()
    rolling.handle_graceful_restart(ws, {"requestId": "g1", "containerId": "abc",
        "healthBaseUrl": "http://192.168.0.30:18029", "settleSec": 0,
        "shutdownTimeoutSec": 60, "readyTimeoutSec": 10})
    assert ws.sent[-1]["status"] == "failed"
    assert "SECRET" not in ws.sent[-1]["error"]
    assert "accessToken=***" in ws.sent[-1]["error"]


def test_list_instances_error_redacts_token(monkeypatch):
    # H2 纵深防御：list-instances 失败分支也要脱敏 token
    def boom(s):
        raise RuntimeError("nacos 失败 http://h/list?accessToken=SECRET&x=1")
    monkeypatch.setattr(nacos_client, "list_healthy_instances", boom)
    ws = FakeWS()
    rolling.handle_list_instances(ws, {"requestId": "r1", "serviceName": "svc"})
    assert ws.sent[-1]["status"] == "failed"
    assert "SECRET" not in ws.sent[-1]["error"]
    assert "accessToken=***" in ws.sent[-1]["error"]


def test_graceful_restart_rejects_empty_url(monkeypatch):
    # H1：healthBaseUrl 缺失/为空必须被拒，不触发 shutdown / restart
    calls = _recording_stubs(monkeypatch)
    ws = FakeWS()
    rolling.handle_graceful_restart(ws, {"requestId": "g1", "containerId": "abc",
        "settleSec": 0, "shutdownTimeoutSec": 60, "readyTimeoutSec": 10})
    assert ws.sent[-1]["status"] == "failed"
    assert calls["post"] == 0 and calls["restart"] == 0


def test_graceful_restart_rejects_missing_host(monkeypatch):
    # H1：有 scheme 但无 host（http:///x）必须被拒
    calls = _recording_stubs(monkeypatch)
    ws = FakeWS()
    rolling.handle_graceful_restart(ws, {"requestId": "g1", "containerId": "abc",
        "healthBaseUrl": "http:///api", "settleSec": 0,
        "shutdownTimeoutSec": 60, "readyTimeoutSec": 10})
    assert ws.sent[-1]["status"] == "failed"
    assert calls["post"] == 0 and calls["restart"] == 0


def test_wait_ready_polls_then_succeeds(monkeypatch):
    # L1/覆盖轮询 sleep 分支：首探非 200 → sleep(3) → 再探 200 成功
    monkeypatch.setattr(http_client, "post", lambda url, timeout=60: (200, "ok"))
    monkeypatch.setattr(docker_cli, "restart_container", lambda cid, timeout=120: (True, "ok"))
    statuses = iter([503, 200])
    monkeypatch.setattr(http_client, "get_status", lambda url, timeout=5: next(statuses))
    recorded = []
    monkeypatch.setattr(time, "sleep", lambda s: recorded.append(s))
    # deadline 足够大，确保第一次非 200 后不超时、走 sleep(3) 再循环
    t = {"v": 0.0}
    def clock():
        t["v"] += 1
        return t["v"]
    monkeypatch.setattr(time, "time", clock)
    ws = FakeWS()
    rolling.handle_graceful_restart(ws, {"requestId": "g1", "containerId": "abc",
        "healthBaseUrl": "http://192.168.0.30:18029", "settleSec": 0,
        "shutdownTimeoutSec": 60, "readyTimeoutSec": 9999})
    assert ws.sent[-1]["status"] == "success"
    assert 3 in recorded  # 轮询间隔 sleep(3) 被执行
