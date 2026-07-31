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
