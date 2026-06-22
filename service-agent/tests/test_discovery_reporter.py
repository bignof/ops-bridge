import os

os.environ.setdefault("WS_URL", "ws://test")
os.environ.setdefault("AGENT_KEY", "test-key")

import config
from core import discovery_reporter
from services import discovery, docker_cli, nacos_client


class _Stop(Exception):
    pass


def test_build_report_assembles_envelope_and_reuses_containers(monkeypatch):
    monkeypatch.setattr(config, "AGENT_ID", "agent-x")
    monkeypatch.setattr(config, "MANAGED_PROJECTS_ROOT", "/data")
    monkeypatch.setattr(config, "NACOS_SERVER", "1.2.3.4:8848")
    raw = [{"Id": "c1"}]
    monkeypatch.setattr(docker_cli, "list_all_containers", lambda: raw)
    seen = {}

    def fake_collect(managed_root="", containers=None):
        seen["managed_root"] = managed_root
        seen["containers"] = containers
        return [{"containerId": "c1", "composeProject": "p"}]

    monkeypatch.setattr(discovery, "collect_local_containers", fake_collect)
    monkeypatch.setattr(
        nacos_client, "list_all_instances", lambda: [{"serviceName": "wms", "ip": "x", "port": 1, "healthy": True}]
    )

    def fake_enrich(records, raw_c, instances):
        seen["instances"] = instances
        return [{"containerId": "c1", "nacosService": "wms", "healthy": True}], [{"type": "w"}]

    monkeypatch.setattr(discovery, "enrich_with_nacos", fake_enrich)
    monkeypatch.setattr(discovery_reporter.time, "time", lambda: 99.0)

    rpt = discovery_reporter.build_report()
    assert rpt == {
        "type": "discovery-report",
        "agentId": "agent-x",
        "nodes": [{"containerId": "c1", "nacosService": "wms", "healthy": True}],
        "warnings": [{"type": "w"}],
        "ts": 99.0,
    }
    assert seen["managed_root"] == "/data"
    assert seen["containers"] is raw  # 复用同一批 inspect,避免二次 docker 调用
    assert seen["instances"] == [{"serviceName": "wms", "ip": "x", "port": 1, "healthy": True}]


def test_build_report_without_nacos_passes_empty_instances(monkeypatch):
    monkeypatch.setattr(config, "AGENT_ID", "a")
    monkeypatch.setattr(config, "MANAGED_PROJECTS_ROOT", "")
    monkeypatch.setattr(config, "NACOS_SERVER", "")  # 未配 nacos
    monkeypatch.setattr(docker_cli, "list_all_containers", lambda: [])
    monkeypatch.setattr(discovery, "collect_local_containers", lambda managed_root="", containers=None: [])
    monkeypatch.setattr(
        nacos_client, "list_all_instances", lambda: (_ for _ in ()).throw(AssertionError("未配 nacos 不该查"))
    )
    captured = {}

    def fake_enrich(records, raw_c, instances):
        captured["instances"] = instances
        return [], []

    monkeypatch.setattr(discovery, "enrich_with_nacos", fake_enrich)
    discovery_reporter.build_report()
    assert captured["instances"] == []


def test_nacos_instances_degrades_to_empty_on_failure(monkeypatch):
    monkeypatch.setattr(config, "NACOS_SERVER", "1.2.3.4:8848")
    monkeypatch.setattr(
        nacos_client, "list_all_instances", lambda: (_ for _ in ()).throw(RuntimeError("nacos down"))
    )
    assert discovery_reporter._nacos_instances() == []


def test_report_once_sends_built_report(monkeypatch):
    sent = []
    monkeypatch.setattr(discovery_reporter, "build_report", lambda: {"type": "discovery-report"})
    monkeypatch.setattr(discovery_reporter, "send_message", lambda ws, msg: sent.append((ws, msg)))
    discovery_reporter.report_once("WS")
    assert sent == [("WS", {"type": "discovery-report"})]


def test_report_once_swallows_errors(monkeypatch):
    monkeypatch.setattr(discovery_reporter, "build_report", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    discovery_reporter.report_once("WS")  # 不抛即通过


def test_start_disabled_when_interval_non_positive(monkeypatch):
    monkeypatch.setattr(config, "DISCOVERY_INTERVAL", 0)
    assert discovery_reporter.start_discovery_reporter(object()) is None


def test_start_creates_daemon_thread_and_reports(monkeypatch):
    monkeypatch.setattr(config, "DISCOVERY_INTERVAL", 30)
    monkeypatch.setattr(discovery_reporter.time, "sleep", lambda s: None)
    reports = []

    def fake_report_once(ws):
        reports.append(ws)
        raise _Stop()  # 第一轮后中断,避免无限循环

    monkeypatch.setattr(discovery_reporter, "report_once", fake_report_once)

    class FakeThread:
        def __init__(self, target, daemon):
            self.target = target
            self.daemon = daemon

        def start(self):
            try:
                self.target()
            except _Stop:
                pass

    monkeypatch.setattr(discovery_reporter.threading, "Thread", FakeThread)

    class WS:
        keep_running = True

    ws = WS()
    thread = discovery_reporter.start_discovery_reporter(ws)
    assert isinstance(thread, FakeThread)
    assert thread.daemon is True
    assert reports == [ws]
