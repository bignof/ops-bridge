from types import SimpleNamespace

import pytest

from core import status_reporter


def test_set_and_get_watch_targets_round_trip() -> None:
    status_reporter.set_watch_targets([{"deploymentId": 1, "dir": "/data/a"}, {"deploymentId": 2, "dir": "/data/b"}])

    assert status_reporter.get_watch_targets() == [
        {"deploymentId": 1, "dir": "/data/a"},
        {"deploymentId": 2, "dir": "/data/b"},
    ]


def test_set_watch_targets_overwrites_not_merges() -> None:
    status_reporter.set_watch_targets([{"deploymentId": 1, "dir": "/data/a"}])
    status_reporter.set_watch_targets([{"deploymentId": 2, "dir": "/data/b"}])

    assert status_reporter.get_watch_targets() == [{"deploymentId": 2, "dir": "/data/b"}]


def test_set_watch_targets_accepts_none_as_empty() -> None:
    status_reporter.set_watch_targets([{"deploymentId": 1, "dir": "/data/a"}])
    status_reporter.set_watch_targets(None)

    assert status_reporter.get_watch_targets() == []


def test_start_status_reporting_sends_batched_report(monkeypatch: pytest.MonkeyPatch) -> None:
    class ImmediateThread:
        def __init__(self, target, daemon):
            self.target = target

        def start(self) -> None:
            self.target()

    sent_messages: list[dict] = []
    ws = SimpleNamespace(keep_running=True)

    def fake_collect(compose_dir):
        ws.keep_running = False  # 只跑一轮（collect 对每个 target 必调，用它来终止循环比用 send 更可靠——
        # send 是否被调用取决于本轮有没有采集到数据，不能拿来当循环终止信号）
        return [
            {
                "name": "app",
                "image": f"img-for-{compose_dir}",
                "state": "running",
                "startedAt": None,
                "containerName": "c",
                "containerId": "1",
                "raw": {},
            }
        ]

    status_reporter.set_watch_targets([{"deploymentId": 1, "dir": "/data/a"}])
    monkeypatch.setattr(status_reporter.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(status_reporter, "send_message", lambda target_ws, payload: sent_messages.append(payload))
    monkeypatch.setattr(status_reporter, "collect_service_statuses", fake_collect)
    monkeypatch.setattr(status_reporter.time, "sleep", lambda seconds: None)

    status_reporter.start_status_reporting(ws)

    assert sent_messages == [
        {
            "type": "status_report",
            "reports": [
                {
                    "deploymentId": 1,
                    "services": [
                        {
                            "name": "app",
                            "image": "img-for-/data/a",
                            "state": "running",
                            "startedAt": None,
                            "containerName": "c",
                            "containerId": "1",
                            "raw": {},
                        }
                    ],
                }
            ],
        }
    ]


def test_start_status_reporting_skips_targets_with_no_services(monkeypatch: pytest.MonkeyPatch) -> None:
    class ImmediateThread:
        def __init__(self, target, daemon):
            self.target = target

        def start(self) -> None:
            self.target()

    sent_messages: list[dict] = []
    ws = SimpleNamespace(keep_running=True)

    def fake_collect(compose_dir):
        ws.keep_running = False
        return []

    status_reporter.set_watch_targets([{"deploymentId": 1, "dir": "/data/gone"}])
    monkeypatch.setattr(status_reporter.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(status_reporter, "send_message", lambda target_ws, payload: sent_messages.append(payload))
    monkeypatch.setattr(status_reporter, "collect_service_statuses", fake_collect)
    monkeypatch.setattr(status_reporter.time, "sleep", lambda seconds: None)

    status_reporter.start_status_reporting(ws)

    assert sent_messages == []  # 没有任何目标采集到数据时不发消息（不是发个空 reports 数组）
