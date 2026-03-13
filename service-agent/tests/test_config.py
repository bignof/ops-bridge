import importlib
import sys
from datetime import datetime

import pytest


def _import_config(monkeypatch: pytest.MonkeyPatch, **overrides: str):
    defaults = {
        "WS_URL": "ws://localhost:8080/ws/agent",
        "AGENT_KEY": "agent-key-1",
        "AGENT_ID": "agent-test",
        "RECONNECT_DELAY": "7",
        "HEARTBEAT_INTERVAL": "11",
        "HEALTH_HOST": "127.0.0.1",
        "HEALTH_PORT": "18081",
    }
    for key, value in defaults.items():
        monkeypatch.setenv(key, value)
    for key, value in overrides.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    sys.modules.pop("config", None)
    return importlib.import_module("config")


def test_config_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _import_config(monkeypatch)

    assert config.WS_URL == "ws://localhost:8080/ws/agent"
    assert config.AGENT_KEY == "agent-key-1"
    assert config.AGENT_ID == "agent-test"
    assert config.RECONNECT_DELAY == 7
    assert config.HEARTBEAT_INTERVAL == 11
    assert config.HEALTH_HOST == "127.0.0.1"
    assert config.HEALTH_PORT == 18081


def test_config_requires_ws_url(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit, match="WS_URL is not set"):
        _import_config(monkeypatch, WS_URL="")


def test_config_requires_agent_key(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit, match="AGENT_KEY is not set"):
        _import_config(monkeypatch, AGENT_KEY="")


def test_china_time_formatter_respects_datefmt(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _import_config(monkeypatch)
    formatter = config.ChinaTimeFormatter("%(asctime)s", "%Y-%m-%d %H:%M:%S")
    record = pytest.importorskip("logging").LogRecord(
        name="test",
        level=20,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )
    record.created = datetime(2026, 3, 13, 7, 30, 0, tzinfo=config.timezone.utc).timestamp()

    assert formatter.formatTime(record, "%Y-%m-%d %H:%M:%S") == "2026-03-13 15:30:00"
