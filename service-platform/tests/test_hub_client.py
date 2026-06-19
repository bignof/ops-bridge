"""hub_client 单测(Task 4)。

平台用 admin token 调 service-hub 的命名空间(provision/rotate agent)端点:
- `provision_agent` → POST `{hub}/api/agents`,body `{agentId}`,header `X-Admin-Token`,读返回 `agentKey`。
- `rotate_agent_key` → POST `{hub}/api/agents/{id}/credentials/rotate`,读返回 `agentKey`。

测试纪律(评审 H8,权威范式 = `service-hub/tests/test_api.py:36`):`settings` 是
`@dataclass(frozen=True)`,**禁** `monkeypatch.setattr(<frozen 实例>, attr, val, raising=False)`
(底层仍 `FrozenInstanceError`)。本文件统一用 `monkeypatch.setattr(hc, "settings", SimpleNamespace(...))`
整体替换模块级引用(`hub_client.py` 是 `from app.config import settings`,可行,且无需 teardown)。

httpx 不真发请求:monkeypatch `hc.httpx.post` 为 fake,断言 URL/header/body/返回解析。
绑定约束:`X-Admin-Token` 仅服务端持有,断言其透传;敏感串(token / agentKey)不入日志。
"""

from __future__ import annotations

import types

import pytest

import app.hub_client as hc


class _Resp:
    """最小 httpx.Response 替身:只实现 hub_client 用到的 raise_for_status / json。"""

    def __init__(self, data: dict, *, status_ok: bool = True) -> None:
        self._d = data
        self._status_ok = status_ok

    def raise_for_status(self) -> None:
        if not self._status_ok:
            raise hc.httpx.HTTPStatusError("boom", request=None, response=None)  # type: ignore[arg-type]

    def json(self) -> dict:
        return self._d


def _fake_settings(*, url: str = "http://hub:8080", token: str = "T") -> types.SimpleNamespace:
    return types.SimpleNamespace(service_hub_url=url, hub_admin_token=token)


def test_provision_agent(monkeypatch) -> None:
    calls: dict = {}

    def fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002 (对齐 httpx.post 签名)
        calls["url"] = url
        calls["headers"] = headers
        calls["json"] = json
        calls["timeout"] = timeout
        return _Resp({"agentKey": "k-123"})

    monkeypatch.setattr(hc, "settings", _fake_settings())
    monkeypatch.setattr(hc.httpx, "post", fake_post)

    assert hc.provision_agent("ns1") == "k-123"
    assert calls["url"].endswith("/api/agents")
    assert calls["url"] == "http://hub:8080/api/agents"
    assert calls["headers"]["X-Admin-Token"] == "T"
    assert calls["headers"]["Content-Type"] == "application/json"
    assert calls["json"] == {"agentId": "ns1"}
    assert calls["timeout"] == 15


def test_rotate_agent_key(monkeypatch) -> None:
    calls: dict = {}

    def fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        calls["url"] = url
        calls["headers"] = headers
        calls["json"] = json
        return _Resp({"agentKey": "k-rot"})

    monkeypatch.setattr(hc, "settings", _fake_settings())
    monkeypatch.setattr(hc.httpx, "post", fake_post)

    assert hc.rotate_agent_key("ns2") == "k-rot"
    assert calls["url"] == "http://hub:8080/api/agents/ns2/credentials/rotate"
    assert calls["headers"]["X-Admin-Token"] == "T"


def test_provision_agent_missing_hub_url_raises(monkeypatch) -> None:
    called = {"hit": False}

    def fake_post(*a, **k):
        called["hit"] = True
        return _Resp({"agentKey": "should-not-happen"})

    monkeypatch.setattr(hc, "settings", _fake_settings(url=""))
    monkeypatch.setattr(hc.httpx, "post", fake_post)

    with pytest.raises(hc.HubError):
        hc.provision_agent("ns1")
    assert called["hit"] is False  # 未配置 hub 时不应发起请求


def test_rotate_agent_key_missing_hub_url_raises(monkeypatch) -> None:
    called = {"hit": False}

    def fake_post(*a, **k):
        called["hit"] = True
        return _Resp({"agentKey": "x"})

    monkeypatch.setattr(hc, "settings", _fake_settings(url=""))
    monkeypatch.setattr(hc.httpx, "post", fake_post)

    with pytest.raises(hc.HubError):
        hc.rotate_agent_key("ns2")
    assert called["hit"] is False


def test_provision_agent_no_key_in_response_raises(monkeypatch) -> None:
    monkeypatch.setattr(hc, "settings", _fake_settings())
    monkeypatch.setattr(hc.httpx, "post", lambda *a, **k: _Resp({}))

    with pytest.raises(hc.HubError):
        hc.provision_agent("ns1")


def test_rotate_agent_key_no_key_in_response_raises(monkeypatch) -> None:
    monkeypatch.setattr(hc, "settings", _fake_settings())
    monkeypatch.setattr(hc.httpx, "post", lambda *a, **k: _Resp({}))

    with pytest.raises(hc.HubError):
        hc.rotate_agent_key("ns2")
