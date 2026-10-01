"""连接鉴权 key 的传递方式：默认请求头，旧中枢拒绝后自动回退 URL 参数。"""
import importlib
import sys

import pytest


def _load(monkeypatch: pytest.MonkeyPatch, **env: str):
    defaults = {"WS_URL": "ws://hub.example/ws/agent", "AGENT_KEY": "secret-key", "AGENT_ID": "agent-7"}
    for key, value in {**defaults, **env}.items():
        monkeypatch.setenv(key, value)
    if "AGENT_AUTH_MODE" not in env:
        monkeypatch.delenv("AGENT_AUTH_MODE", raising=False)
    for name in ["config", "core.ws_client"]:
        sys.modules.pop(name, None)
    module = importlib.import_module("core.ws_client")
    created: list[dict] = []

    class FakeWebSocketApp:
        def __init__(self, url, header, **handlers):
            created.append({"url": url, "header": header})

        def run_forever(self, ping_interval, ping_timeout):
            pass

    monkeypatch.setattr(module.websocket, "WebSocketApp", FakeWebSocketApp)
    # _on_close 的其余清场与本测试无关
    for name in ["stop_status_reporting", "stop_all_log_sessions", "stop_all_follow", "abort_all_fetch"]:
        monkeypatch.setattr(module, name, lambda *a, **k: 0)
    return module, created


def _connected_with(module, created):
    module.connect()
    return created[-1]


def test_auto_mode_uses_header_and_keeps_key_out_of_url(monkeypatch):
    module, created = _load(monkeypatch)
    conn = _connected_with(module, created)
    assert conn == {"url": "ws://hub.example/ws/agent/agent-7", "header": ["X-Agent-Key: secret-key"]}


def test_query_mode_keeps_legacy_url(monkeypatch):
    module, created = _load(monkeypatch, AGENT_AUTH_MODE="query")
    conn = _connected_with(module, created)
    assert conn == {"url": "ws://hub.example/ws/agent/agent-7?key=secret-key", "header": None}


def test_unknown_mode_falls_back_to_auto(monkeypatch):
    module, created = _load(monkeypatch, AGENT_AUTH_MODE="bogus")
    assert module.AGENT_AUTH_MODE == "auto"
    assert _connected_with(module, created)["header"] == ["X-Agent-Key: secret-key"]


def test_old_hub_rejecting_header_switches_to_query_then_back_on_bad_key(monkeypatch):
    module, created = _load(monkeypatch)
    module._on_close(None, 1008, "auth failed")  # 旧中枢只认 ?key=
    assert "?key=secret-key" in _connected_with(module, created)["url"]
    module._on_close(None, 1008, b"auth failed")  # URL 参数也被拒：key 不对，回到请求头
    assert _connected_with(module, created)["header"] == ["X-Agent-Key: secret-key"]


def test_header_confirmed_by_hub_message_never_falls_back(monkeypatch):
    module, created = _load(monkeypatch)
    module._on_message(None, '{"type": "unknown"}')  # 收到中枢消息 = 请求头鉴权已通过
    module._on_close(None, 1008, "auth failed")  # 之后的拒绝是 key 被轮换，不是中枢不支持
    assert _connected_with(module, created)["header"] == ["X-Agent-Key: secret-key"]


@pytest.mark.parametrize("code,reason", [(1008, "key rotated"), (1000, "replaced by new connection"), (None, None)])
def test_other_close_reasons_do_not_switch(monkeypatch, code, reason):
    module, created = _load(monkeypatch)
    module._on_close(None, code, reason)
    assert _connected_with(module, created)["header"] == ["X-Agent-Key: secret-key"]


def test_fixed_header_mode_never_switches(monkeypatch):
    module, created = _load(monkeypatch, AGENT_AUTH_MODE="header")
    module._on_close(None, 1008, "auth failed")
    assert _connected_with(module, created)["header"] == ["X-Agent-Key: secret-key"]
