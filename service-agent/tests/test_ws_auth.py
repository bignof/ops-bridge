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


def test_hub_rolled_back_after_header_was_accepted_still_falls_back(monkeypatch):
    """新中枢接受过请求头之后被回滚到只认 ?key= 的旧版：死守请求头会让全部 Agent 永久连不上。"""
    module, created = _load(monkeypatch)
    module._on_message(None, '{"type": "hub_hello", "features": ["header_auth"]}')
    module._on_close(None, 1008, "auth failed")  # 旧语义：中枢根本没读请求头
    assert "?key=secret-key" in _connected_with(module, created)["url"]


def test_new_hub_rejecting_header_key_keeps_key_out_of_url(monkeypatch):
    """新中枢认识请求头，拒绝说明 AGENT_ID / AGENT_KEY 不对：回退只会把有效 key 写进访问日志。"""
    module, created = _load(monkeypatch)
    module._on_close(None, 1008, "auth failed: header")
    conn = _connected_with(module, created)
    assert conn == {"url": "ws://hub.example/ws/agent/agent-7", "header": ["X-Agent-Key: secret-key"]}


def test_query_rejected_by_new_hub_switches_back_to_header(monkeypatch):
    module, created = _load(monkeypatch)
    module._on_close(None, 1008, "auth failed")
    module._on_close(None, 1008, "auth failed: header")  # 极端情况：URL 参数也被以新语义拒绝
    assert _connected_with(module, created)["header"] == ["X-Agent-Key: secret-key"]


def test_hub_hello_restores_header_after_fallback(monkeypatch):
    """退回 URL 参数后中枢升级：中枢不会拒绝 ?key=，只能靠 hub_hello 声明的能力切回请求头。"""
    module, created = _load(monkeypatch)
    module._on_close(None, 1008, "auth failed")
    assert "?key=" in _connected_with(module, created)["url"]
    module._on_message(None, '{"type": "hub_hello", "features": ["header_auth"]}')
    assert _connected_with(module, created)["header"] == ["X-Agent-Key: secret-key"]


@pytest.mark.parametrize("hello", ['{"type": "hub_hello"}', '{"type": "hub_hello", "features": ["other"]}'])
def test_hub_hello_without_header_feature_keeps_query(monkeypatch, hello):
    module, created = _load(monkeypatch)
    module._on_close(None, 1008, "auth failed")
    module._on_message(None, hello)
    assert "?key=secret-key" in _connected_with(module, created)["url"]


def test_fixed_query_mode_ignores_hub_hello(monkeypatch):
    module, created = _load(monkeypatch, AGENT_AUTH_MODE="query")
    module._on_message(None, '{"type": "hub_hello", "features": ["header_auth"]}')
    assert _connected_with(module, created)["header"] is None


@pytest.mark.parametrize("code,reason", [(1008, "key rotated"), (1000, "replaced by new connection"), (None, None)])
def test_other_close_reasons_do_not_switch(monkeypatch, code, reason):
    module, created = _load(monkeypatch)
    module._on_close(None, code, reason)
    assert _connected_with(module, created)["header"] == ["X-Agent-Key: secret-key"]


def test_fixed_header_mode_never_switches(monkeypatch):
    module, created = _load(monkeypatch, AGENT_AUTH_MODE="header")
    module._on_close(None, 1008, "auth failed")
    assert _connected_with(module, created)["header"] == ["X-Agent-Key: secret-key"]
