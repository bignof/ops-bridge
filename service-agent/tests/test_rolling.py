import os
os.environ.setdefault("WS_URL", "ws://test")
os.environ.setdefault("AGENT_KEY", "test-key")

import json
import time

import config
from core import handlers, rolling
from services import nacos_client, docker_cli, http_client


class FakeWS:
    def __init__(self):
        self.sent = []

    def send(self, payload):
        self.sent.append(json.loads(payload))


def _container(cid, host_port, project=None):
    """构造一个带宿主端口、可选 compose project label 的 docker inspect 容器。"""
    c = {"Id": cid, "NetworkSettings":
         {"Ports": {"80/tcp": [{"HostPort": str(host_port)}]}, "Networks": {}}}
    if project is not None:
        c["Config"] = {"Labels": {"com.docker.compose.project": project}}
    return c


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
    # 未传 expectedComposeProject：向后兼容；容器无 label → composeProject=None，matched 仍按端口
    assert msg["instances"] == [{"address": "192.168.0.30:18029",
                                 "containerId": "abcdef123456", "healthy": True,
                                 "matched": True, "composeProject": None}]


def test_list_instances_includes_compose_project(monkeypatch):
    # 不传 expected：每实例带回容器的 compose project label，matched 仍按端口/IP（向后兼容）
    monkeypatch.setattr(nacos_client, "list_healthy_instances",
                        lambda s: [{"ip": "192.168.0.30", "port": 18029}])
    monkeypatch.setattr(docker_cli, "list_running_containers",
                        lambda: [_container("abcdef1234567890", 18029, project="memory-share-1")])
    ws = FakeWS()
    rolling.handle_list_instances(ws, {"requestId": "r1", "serviceName": "memory-share"})
    inst = ws.sent[-1]["instances"][0]
    assert inst["composeProject"] == "memory-share-1"
    assert inst["matched"] is True


def test_list_instances_expected_project_mismatch_unmatched(monkeypatch):
    # 传 expectedComposeProject 且容器 project 不符 → 寻址漂移，matched=False（composeProject 仍如实回传）
    monkeypatch.setattr(nacos_client, "list_healthy_instances",
                        lambda s: [{"ip": "192.168.0.30", "port": 18029}])
    monkeypatch.setattr(docker_cli, "list_running_containers",
                        lambda: [_container("abcdef1234567890", 18029, project="other-project")])
    ws = FakeWS()
    rolling.handle_list_instances(ws, {"requestId": "r1", "serviceName": "memory-share",
                                       "expectedComposeProject": "memory-share-1"})
    inst = ws.sent[-1]["instances"][0]
    assert inst["matched"] is False
    assert inst["composeProject"] == "other-project"
    # 端口命中，容器仍可寻址，故 containerId 照常带回（供上层定位漂移容器）
    assert inst["containerId"] == "abcdef123456"


def test_list_instances_expected_project_match(monkeypatch):
    # 传 expectedComposeProject 且相符 → matched=True
    monkeypatch.setattr(nacos_client, "list_healthy_instances",
                        lambda s: [{"ip": "192.168.0.30", "port": 18029}])
    monkeypatch.setattr(docker_cli, "list_running_containers",
                        lambda: [_container("abcdef1234567890", 18029, project="memory-share-1")])
    ws = FakeWS()
    rolling.handle_list_instances(ws, {"requestId": "r1", "serviceName": "memory-share",
                                       "expectedComposeProject": "memory-share-1"})
    inst = ws.sent[-1]["instances"][0]
    assert inst["matched"] is True
    assert inst["composeProject"] == "memory-share-1"


def test_list_instances_unmatched_flagged(monkeypatch):
    monkeypatch.setattr(nacos_client, "list_healthy_instances",
                        lambda s: [{"ip": "10.9.9.9", "port": 9999}])
    monkeypatch.setattr(docker_cli, "list_running_containers", lambda: [])
    ws = FakeWS()
    rolling.handle_list_instances(ws, {"requestId": "r1", "serviceName": "svc"})
    inst = ws.sent[-1]["instances"][0]
    assert inst["matched"] is False and inst["containerId"] is None
    assert inst["composeProject"] is None


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


def test_graceful_restart_sends_shutdown_token_when_configured(monkeypatch):
    # T4a：配 K8S_SHUTDOWN_TOKEN 时，graceful-restart 调 /api/k8s/shutdown 带 X-Shutdown-Token
    monkeypatch.setattr(config, "K8S_SHUTDOWN_TOKEN", "secret")
    captured = {}

    def rec_post(url, timeout=60, headers=None):
        captured["url"] = url
        captured["headers"] = headers
        return (200, "ok")

    monkeypatch.setattr(http_client, "post", rec_post)
    monkeypatch.setattr(docker_cli, "restart_container", lambda cid, timeout=120: (True, "ok"))
    monkeypatch.setattr(http_client, "get_status", lambda url, timeout=5: 200)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    ws = FakeWS()
    rolling.handle_graceful_restart(ws, {"requestId": "g1", "containerId": "abc",
        "healthBaseUrl": "http://192.168.0.30:18029", "settleSec": 0,
        "shutdownTimeoutSec": 60, "readyTimeoutSec": 10})
    assert ws.sent[-1]["status"] == "success"
    assert captured["url"] == "http://192.168.0.30:18029/api/k8s/shutdown"
    assert captured["headers"] == {"X-Shutdown-Token": "secret"}


def test_graceful_restart_omits_token_header_when_unset(monkeypatch):
    # T4a 向后兼容：未配 token 时不带 headers 关键字（桩仅接受 url/timeout，误带则 TypeError）
    monkeypatch.setattr(config, "K8S_SHUTDOWN_TOKEN", "")
    captured = {}

    def rec_post(url, timeout=60):
        captured["url"] = url
        captured["timeout"] = timeout
        return (200, "ok")

    monkeypatch.setattr(http_client, "post", rec_post)
    monkeypatch.setattr(docker_cli, "restart_container", lambda cid, timeout=120: (True, "ok"))
    monkeypatch.setattr(http_client, "get_status", lambda url, timeout=5: 200)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    ws = FakeWS()
    rolling.handle_graceful_restart(ws, {"requestId": "g1", "containerId": "abc",
        "healthBaseUrl": "http://192.168.0.30:18029", "settleSec": 0,
        "shutdownTimeoutSec": 60, "readyTimeoutSec": 10})
    assert ws.sent[-1]["status"] == "success"
    assert captured == {"url": "http://192.168.0.30:18029/api/k8s/shutdown", "timeout": 60}


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


# ─────────────────────────────────────────────
# handle_graceful_redeploy — drain → 拉镜像/重建 → wait-ready → 回 redeploy-result
# （graceful-restart 的镜像版，与之对称）
# ─────────────────────────────────────────────

def _redeploy_msg(**overrides):
    """构造一条 graceful-redeploy 消息（带协调器约定的全部字段），允许覆盖。"""
    msg = {"requestId": "d1", "containerId": "abc", "image": "registry.example.com/app:1",
           "dir": "/data/biz-app", "healthBaseUrl": "http://192.168.0.30:18029",
           "settleSec": 7, "shutdownTimeoutSec": 60, "readyTimeoutSec": 10}
    msg.update(overrides)
    return msg


def test_graceful_redeploy_success(monkeypatch):
    # 成功路径：drain ok → 重建 ok → wait-ready ok → 回 success；并断言 settle 步骤确实跑了
    order = []
    monkeypatch.setattr(rolling, "drain", lambda base, timeout=60: order.append(f"drain:{base}:{timeout}"))
    monkeypatch.setattr(rolling, "redeploy_compose_image",
                        lambda data, rid, pd: order.append(f"redeploy:{data.get('image')}:{pd}") or
                        {"ok": True, "output": "up ok", "error": None})
    monkeypatch.setattr(http_client, "get_status", lambda url, timeout=5: 200)
    recorded = []
    monkeypatch.setattr(time, "sleep", lambda s: recorded.append(s))
    ws = FakeWS()
    rolling.handle_graceful_redeploy(ws, _redeploy_msg())
    assert ws.sent == [{"type": "graceful-redeploy-result", "requestId": "d1", "status": "success"}]
    # 次序固定：drain 必须先于重建
    assert order == ["drain:http://192.168.0.30:18029:60", "redeploy:registry.example.com/app:1:/data/biz-app"]
    assert 7 in recorded  # settle 承重步骤被执行


def test_graceful_redeploy_drain_fail_skips_rebuild(monkeypatch):
    # drain 失败 → 不重建、回 failed（redeploy_compose_image 绝不被调用）
    rebuilt = {"n": 0}
    monkeypatch.setattr(rolling, "drain",
                        lambda base, timeout=60: (_ for _ in ()).throw(RuntimeError("shutdown 返回 503")))
    monkeypatch.setattr(rolling, "redeploy_compose_image",
                        lambda data, rid, pd: rebuilt.__setitem__("n", rebuilt["n"] + 1) or
                        {"ok": True, "output": "", "error": None})
    monkeypatch.setattr(http_client, "get_status", lambda url, timeout=5: 200)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    ws = FakeWS()
    rolling.handle_graceful_redeploy(ws, _redeploy_msg())
    assert ws.sent[-1]["type"] == "graceful-redeploy-result"
    assert ws.sent[-1]["status"] == "failed" and "shutdown" in ws.sent[-1]["error"]
    assert rebuilt["n"] == 0


def test_graceful_redeploy_rejects_non_allowlisted_image_before_pull(monkeypatch):
    # 非白名单 image → 重建核心在 pull 之前就拦下、回 failed，且绝不真正 pull/down/up。
    # 用真实 redeploy_compose_image（仅桩 handlers 的 compose 原语），验证拦截点确在 pull 之前。
    monkeypatch.setattr(rolling, "drain", lambda base, timeout=60: None)
    monkeypatch.setattr(http_client, "get_status", lambda url, timeout=5: 200)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(handlers.config, "IMAGE_REGISTRY_ALLOWLIST", ["registry.example.com"])
    compose_calls = []
    monkeypatch.setattr(handlers, "find_compose_file", lambda project_dir: "compose.yml")
    monkeypatch.setattr(handlers, "read_compose_file", lambda compose_file: "services: {}\n")
    monkeypatch.setattr(handlers, "update_image_in_compose", lambda *a: ["api"])
    monkeypatch.setattr(handlers, "run_compose",
                        lambda project_dir, args: (compose_calls.append(args) or (True, "ok")))
    ws = FakeWS()
    rolling.handle_graceful_redeploy(ws, _redeploy_msg(image="evil.com/x:1"))
    assert ws.sent[-1]["status"] == "failed"
    assert "白名单" in ws.sent[-1]["error"]
    # 关键：拦在 pull 之前，没有任何 compose 子命令被执行
    assert compose_calls == []


def test_graceful_redeploy_rebuild_fail_skips_wait_ready(monkeypatch):
    # 重建失败（compose 步骤失败，error=None）→ 回 failed（带 output），且不再 wait-ready
    monkeypatch.setattr(rolling, "drain", lambda base, timeout=60: None)
    monkeypatch.setattr(rolling, "redeploy_compose_image",
                        lambda data, rid, pd: {"ok": False, "output": "pull 失败详情", "error": None})
    ready_calls = {"n": 0}
    monkeypatch.setattr(http_client, "get_status",
                        lambda url, timeout=5: ready_calls.__setitem__("n", ready_calls["n"] + 1) or 200)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    ws = FakeWS()
    rolling.handle_graceful_redeploy(ws, _redeploy_msg())
    assert ws.sent[-1]["status"] == "failed"
    assert "pull 失败详情" in ws.sent[-1]["error"]
    # 重建已失败，绝不进入 wait-ready 探测
    assert ready_calls["n"] == 0


def test_graceful_redeploy_wait_ready_timeout(monkeypatch):
    # 重建成功但新容器始终非 200 → wait-ready 超时 → 回 failed
    monkeypatch.setattr(rolling, "drain", lambda base, timeout=60: None)
    monkeypatch.setattr(rolling, "redeploy_compose_image",
                        lambda data, rid, pd: {"ok": True, "output": "up ok", "error": None})
    monkeypatch.setattr(http_client, "get_status", lambda url, timeout=5: 503)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    # 单调递增 clock，快速越过 deadline，_wait_ready 有限步内超时返回 False（同 graceful-restart 套路）
    t = {"v": 1000.0}
    def clock():
        t["v"] += 1000
        return t["v"]
    monkeypatch.setattr(time, "time", clock)
    ws = FakeWS()
    rolling.handle_graceful_redeploy(ws, _redeploy_msg(readyTimeoutSec=1))
    assert ws.sent[-1]["status"] == "failed" and "ready" in ws.sent[-1]["error"]


def test_graceful_redeploy_rejects_public_ip_before_drain(monkeypatch):
    # H1：公网 healthBaseUrl 必须被拒，且不 drain、不重建
    calls = {"drain": 0, "rebuild": 0}
    monkeypatch.setattr(rolling, "drain", lambda base, timeout=60: calls.__setitem__("drain", calls["drain"] + 1))
    monkeypatch.setattr(rolling, "redeploy_compose_image",
                        lambda data, rid, pd: calls.__setitem__("rebuild", calls["rebuild"] + 1) or
                        {"ok": True, "output": "", "error": None})
    monkeypatch.setattr(http_client, "get_status", lambda url, timeout=5: 200)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    ws = FakeWS()
    rolling.handle_graceful_redeploy(ws, _redeploy_msg(healthBaseUrl="http://8.8.8.8:18029"))
    assert ws.sent[-1]["status"] == "failed"
    assert "内网" in ws.sent[-1]["error"] or "公网" in ws.sent[-1]["error"]
    assert calls["drain"] == 0 and calls["rebuild"] == 0


def test_graceful_redeploy_rejects_empty_url_before_drain(monkeypatch):
    # H1：healthBaseUrl 缺失/为空必须被拒，不 drain、不重建
    calls = {"drain": 0, "rebuild": 0}
    monkeypatch.setattr(rolling, "drain", lambda base, timeout=60: calls.__setitem__("drain", calls["drain"] + 1))
    monkeypatch.setattr(rolling, "redeploy_compose_image",
                        lambda data, rid, pd: calls.__setitem__("rebuild", calls["rebuild"] + 1) or
                        {"ok": True, "output": "", "error": None})
    monkeypatch.setattr(time, "sleep", lambda s: None)
    ws = FakeWS()
    msg = _redeploy_msg()
    del msg["healthBaseUrl"]
    rolling.handle_graceful_redeploy(ws, msg)
    assert ws.sent[-1]["status"] == "failed"
    assert calls["drain"] == 0 and calls["rebuild"] == 0


def test_graceful_redeploy_error_redacts_token(monkeypatch):
    # H2 纵深防御：即便底层异常串里带 accessToken，回 hub 的 error 也必须脱敏
    monkeypatch.setattr(rolling, "drain", lambda base, timeout=60: None)
    def boom(data, rid, pd):
        raise RuntimeError("失败 url=http://x/list?accessToken=SECRET&k=1")
    monkeypatch.setattr(rolling, "redeploy_compose_image", boom)
    monkeypatch.setattr(http_client, "get_status", lambda url, timeout=5: 200)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    ws = FakeWS()
    rolling.handle_graceful_redeploy(ws, _redeploy_msg())
    assert ws.sent[-1]["status"] == "failed"
    assert "SECRET" not in ws.sent[-1]["error"]
    assert "accessToken=***" in ws.sent[-1]["error"]


def test_graceful_redeploy_ready_timeout_zero_probes_once(monkeypatch):
    # L1：readyTimeoutSec=0 但节点已 ready（get_status→200），应至少探一次并成功
    monkeypatch.setattr(rolling, "drain", lambda base, timeout=60: None)
    monkeypatch.setattr(rolling, "redeploy_compose_image",
                        lambda data, rid, pd: {"ok": True, "output": "up ok", "error": None})
    monkeypatch.setattr(http_client, "get_status", lambda url, timeout=5: 200)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    ws = FakeWS()
    rolling.handle_graceful_redeploy(ws, _redeploy_msg(readyTimeoutSec=0))
    assert ws.sent[-1]["status"] == "success"
