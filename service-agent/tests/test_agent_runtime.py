import runpy
import sys
from types import ModuleType

import pytest


class StopLoop(Exception):
    pass


def test_agent_main_starts_health_server_and_reconnects(monkeypatch: pytest.MonkeyPatch) -> None:
    health_calls: list[str] = []
    connect_calls: list[str] = []
    sleep_calls: list[int] = []

    fake_config = ModuleType("config")
    fake_config.RECONNECT_DELAY = 5

    fake_health_server = ModuleType("core.health_server")
    fake_health_server.start_health_server = lambda: health_calls.append("started")

    fake_ws_client = ModuleType("core.ws_client")
    fake_ws_client.connect = lambda: connect_calls.append("connected")

    monkeypatch.setitem(sys.modules, "config", fake_config)
    monkeypatch.setitem(sys.modules, "core.health_server", fake_health_server)
    monkeypatch.setitem(sys.modules, "core.ws_client", fake_ws_client)
    monkeypatch.setattr("time.sleep", lambda seconds: (sleep_calls.append(seconds), (_ for _ in ()).throw(StopLoop()))[1])

    with pytest.raises(StopLoop):
        runpy.run_module("agent", run_name="__main__")

    assert health_calls == ["started"]
    assert connect_calls == ["connected"]
    assert sleep_calls == [5]
