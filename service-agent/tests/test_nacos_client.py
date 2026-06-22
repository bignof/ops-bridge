import os
os.environ.setdefault("WS_URL", "ws://test")
os.environ.setdefault("AGENT_KEY", "test-key")

import pytest
import requests
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


class FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)


def test_login_real_body(monkeypatch):
    # M2：不打桩 _login 真身，验证 url/form/取 accessToken
    monkeypatch.setattr(config, "NACOS_SERVER", "1.2.3.4:8848")
    monkeypatch.setattr(config, "NACOS_CONTEXT_PATH", "/nacos")
    monkeypatch.setattr(config, "NACOS_USERNAME", "nacos")
    monkeypatch.setattr(config, "NACOS_PASSWORD", "pw")
    captured = {}
    def fake_post(url, data=None, timeout=10):
        captured["url"] = url
        captured["data"] = data
        return FakeResp(payload={"accessToken": "tok"})
    monkeypatch.setattr(nacos_client.requests, "post", fake_post)
    assert nacos_client._login() == "tok"
    assert captured["url"].endswith("/v1/auth/login")
    assert captured["data"]["username"] == "nacos"
    assert captured["data"]["password"] == "pw"


def test_login_raises_on_error(monkeypatch):
    # M2：登录返回非 2xx 时 raise_for_status 抛错
    monkeypatch.setattr(config, "NACOS_SERVER", "1.2.3.4:8848")
    monkeypatch.setattr(config, "NACOS_CONTEXT_PATH", "/nacos")
    monkeypatch.setattr(config, "NACOS_USERNAME", "nacos")
    monkeypatch.setattr(config, "NACOS_PASSWORD", "bad")
    monkeypatch.setattr(nacos_client.requests, "post",
                        lambda url, data=None, timeout=10: FakeResp(status=403))
    with pytest.raises(requests.HTTPError):
        nacos_client._login()


def test_list_instances_http_error_strips_token(monkeypatch):
    # H2：get_json 抛 HTTPError（其 str 含 accessToken=SECRET），
    # list_healthy_instances 必须抛 RuntimeError 且不含 SECRET
    monkeypatch.setattr(config, "NACOS_SERVER", "1.2.3.4:8848")
    monkeypatch.setattr(config, "NACOS_CONTEXT_PATH", "/nacos")
    monkeypatch.setattr(config, "NACOS_NAMESPACE", "")
    monkeypatch.setattr(config, "NACOS_GROUP", "DEFAULT_GROUP")
    monkeypatch.setattr(config, "NACOS_USERNAME", "")
    err_resp = FakeResp(status=403)
    def boom(url, params=None, timeout=10):
        raise requests.HTTPError(
            "403 Client Error for url: http://h/list?accessToken=SECRET&x=1",
            response=err_resp,
        )
    monkeypatch.setattr(http_client, "get_json", boom)
    with pytest.raises(RuntimeError) as ei:
        nacos_client.list_healthy_instances("svc")
    assert "SECRET" not in str(ei.value)
    assert "403" in str(ei.value)


def test_list_instances_http_error_no_response(monkeypatch):
    # H2：response 为 None 时降级为 "?"，仍不冒泡含 token 的原异常
    monkeypatch.setattr(config, "NACOS_SERVER", "1.2.3.4:8848")
    monkeypatch.setattr(config, "NACOS_CONTEXT_PATH", "/nacos")
    monkeypatch.setattr(config, "NACOS_NAMESPACE", "")
    monkeypatch.setattr(config, "NACOS_GROUP", "DEFAULT_GROUP")
    monkeypatch.setattr(config, "NACOS_USERNAME", "")
    def boom(url, params=None, timeout=10):
        raise requests.HTTPError("boom accessToken=SECRET")
    monkeypatch.setattr(http_client, "get_json", boom)
    with pytest.raises(RuntimeError) as ei:
        nacos_client.list_healthy_instances("svc")
    assert "SECRET" not in str(ei.value)


# --- list_all_instances(P3 发现:全服务全实例,含不健康)---


def test_list_all_instances_requires_server(monkeypatch):
    monkeypatch.setattr(config, "NACOS_SERVER", "")
    with pytest.raises(RuntimeError):
        nacos_client.list_all_instances()


def test_list_all_instances_collects_all_services_unfiltered(monkeypatch):
    monkeypatch.setattr(config, "NACOS_SERVER", "1.2.3.4:8848")
    monkeypatch.setattr(config, "NACOS_CONTEXT_PATH", "/nacos")
    monkeypatch.setattr(config, "NACOS_NAMESPACE", "dev")
    monkeypatch.setattr(config, "NACOS_GROUP", "G")
    monkeypatch.setattr(config, "NACOS_USERNAME", "")
    calls = []

    def fake_get_json(url, params=None, timeout=10):
        calls.append(url)
        if url.endswith("/v1/ns/service/list"):
            assert params["namespaceId"] == "dev"
            return {"doms": ["svc-a", "svc-b"]}
        if url.endswith("/v1/ns/instance/list"):
            svc = params["serviceName"]
            return {"hosts": [{"ip": "10.0.0.1", "port": 18029, "healthy": svc == "svc-a"}]}
        raise AssertionError(url)

    monkeypatch.setattr(http_client, "get_json", fake_get_json)
    out = nacos_client.list_all_instances()
    assert out == [
        {"serviceName": "svc-a", "ip": "10.0.0.1", "port": 18029, "healthy": True},
        {"serviceName": "svc-b", "ip": "10.0.0.1", "port": 18029, "healthy": False},  # 含不健康
    ]
    assert calls[0].endswith("/v1/ns/service/list")


def test_list_all_instances_login_token_propagates(monkeypatch):
    monkeypatch.setattr(config, "NACOS_SERVER", "1.2.3.4:8848")
    monkeypatch.setattr(config, "NACOS_CONTEXT_PATH", "/nacos")
    monkeypatch.setattr(config, "NACOS_NAMESPACE", "")
    monkeypatch.setattr(config, "NACOS_GROUP", "G")
    monkeypatch.setattr(config, "NACOS_USERNAME", "nacos")
    monkeypatch.setattr(nacos_client, "_login", lambda: "tok-9")
    seen = []

    def fake_get_json(url, params=None, timeout=10):
        seen.append(params.get("accessToken"))
        if url.endswith("/service/list"):
            return {"doms": ["s"]}
        return {"hosts": []}

    monkeypatch.setattr(http_client, "get_json", fake_get_json)
    nacos_client.list_all_instances()
    assert seen == ["tok-9", "tok-9"]  # service/list + instance/list 都带同一 token(登录只一次)


def test_list_all_instances_service_list_error_strips_token(monkeypatch):
    monkeypatch.setattr(config, "NACOS_SERVER", "1.2.3.4:8848")
    monkeypatch.setattr(config, "NACOS_CONTEXT_PATH", "/nacos")
    monkeypatch.setattr(config, "NACOS_NAMESPACE", "")
    monkeypatch.setattr(config, "NACOS_GROUP", "G")
    monkeypatch.setattr(config, "NACOS_USERNAME", "")

    def boom(url, params=None, timeout=10):
        raise requests.HTTPError("403 accessToken=SECRET", response=FakeResp(status=403))

    monkeypatch.setattr(http_client, "get_json", boom)
    with pytest.raises(RuntimeError) as ei:
        nacos_client.list_all_instances()
    assert "SECRET" not in str(ei.value)
    assert "403" in str(ei.value)


def test_list_all_instances_instance_list_error_no_response_strips_token(monkeypatch):
    monkeypatch.setattr(config, "NACOS_SERVER", "1.2.3.4:8848")
    monkeypatch.setattr(config, "NACOS_CONTEXT_PATH", "/nacos")
    monkeypatch.setattr(config, "NACOS_NAMESPACE", "")
    monkeypatch.setattr(config, "NACOS_GROUP", "G")
    monkeypatch.setattr(config, "NACOS_USERNAME", "")

    def fake(url, params=None, timeout=10):
        if url.endswith("/service/list"):
            return {"doms": ["s"]}
        raise requests.HTTPError("boom accessToken=SECRET")  # response None → "?"

    monkeypatch.setattr(http_client, "get_json", fake)
    with pytest.raises(RuntimeError) as ei:
        nacos_client.list_all_instances()
    assert "SECRET" not in str(ei.value)
