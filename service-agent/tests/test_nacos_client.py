import os
os.environ.setdefault("WS_URL", "ws://test")
os.environ.setdefault("AGENT_KEY", "test-key")

import pytest
from services import nacos_client, http_client
import config

def test_requires_server(monkeypatch):
    monkeypatch.setattr(config, "NACOS_SERVER", "")
    with pytest.raises(RuntimeError):
        nacos_client.list_healthy_instances("svc")

def test_filters_unhealthy_and_builds_url(monkeypatch):
    monkeypatch.setattr(config, "NACOS_SERVER", "1.2.3.4:8848")
    monkeypatch.setattr(config, "NACOS_CONTEXT_PATH", "/nacos")
    monkeypatch.setattr(config, "NACOS_NAMESPACE", "dev")
    monkeypatch.setattr(config, "NACOS_GROUP", "DEFAULT_GROUP")
    monkeypatch.setattr(config, "NACOS_USERNAME", "")
    captured = {}
    def fake_get_json(url, params=None, timeout=10):
        captured["url"] = url
        captured["params"] = params
        return {"hosts": [
            {"ip": "10.0.0.1", "port": 18029, "healthy": True, "enabled": True},
            {"ip": "10.0.0.2", "port": 18030, "healthy": False, "enabled": True},
            {"ip": "10.0.0.3", "port": 18031, "healthy": True, "enabled": False},
        ]}
    monkeypatch.setattr(http_client, "get_json", fake_get_json)
    out = nacos_client.list_healthy_instances("memory-share")
    assert out == [{"ip": "10.0.0.1", "port": 18029}]
    assert captured["url"] == "http://1.2.3.4:8848/nacos/v1/ns/instance/list"
    assert captured["params"]["serviceName"] == "memory-share"
    assert captured["params"]["namespaceId"] == "dev"

def test_login_when_username_set(monkeypatch):
    monkeypatch.setattr(config, "NACOS_SERVER", "1.2.3.4:8848")
    monkeypatch.setattr(config, "NACOS_CONTEXT_PATH", "/nacos")
    monkeypatch.setattr(config, "NACOS_NAMESPACE", "")
    monkeypatch.setattr(config, "NACOS_GROUP", "DEFAULT_GROUP")
    monkeypatch.setattr(config, "NACOS_USERNAME", "nacos")
    monkeypatch.setattr(config, "NACOS_PASSWORD", "pw")
    monkeypatch.setattr(nacos_client, "_login", lambda: "tok-123")
    def fake_get_json(url, params=None, timeout=10):
        assert params["accessToken"] == "tok-123"
        return {"hosts": []}
    monkeypatch.setattr(http_client, "get_json", fake_get_json)
    assert nacos_client.list_healthy_instances("svc") == []
