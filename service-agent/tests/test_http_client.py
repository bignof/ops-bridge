import pytest
import requests
from services import http_client

class FakeResp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text
    def json(self):
        return self._payload
    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

def test_get_json_ok(monkeypatch):
    captured = {}
    def fake_get(url, params=None, timeout=10, **kwargs):
        captured.update(kwargs)
        return FakeResp(payload={"a": 1})
    monkeypatch.setattr(requests, "get", fake_get)
    assert http_client.get_json("http://x", {"k": "v"}) == {"a": 1}
    # H1：必须禁止跟随重定向
    assert captured.get("allow_redirects") is False

def test_get_json_raises_on_4xx(monkeypatch):
    # L4/L7：4xx/5xx 应经 raise_for_status 冒泡成 HTTPError
    monkeypatch.setattr(requests, "get",
                        lambda url, params=None, timeout=10, **k: FakeResp(status=500))
    with pytest.raises(requests.HTTPError):
        http_client.get_json("http://x")

def test_get_status_handles_error(monkeypatch):
    def boom(*a, **k):
        raise requests.ConnectionError("refused")
    monkeypatch.setattr(requests, "get", boom)
    assert http_client.get_status("http://x") == 0

def test_get_status_returns_code(monkeypatch):
    # L4/L7：get_status 成功路径（非异常），原样返回状态码
    captured = {}
    def fake_get(url, timeout=5, **kwargs):
        captured.update(kwargs)
        return FakeResp(status=503)
    monkeypatch.setattr(requests, "get", fake_get)
    assert http_client.get_status("http://x") == 503
    assert captured.get("allow_redirects") is False

def test_post_returns_code_and_text(monkeypatch):
    captured = {}
    def fake_post(url, timeout=60, **kwargs):
        captured.update(kwargs)
        return FakeResp(status=200, text="ok")
    monkeypatch.setattr(requests, "post", fake_post)
    assert http_client.post("http://x") == (200, "ok")
    # H1：post 同样禁止跟随重定向
    assert captured.get("allow_redirects") is False


def test_post_forwards_headers_to_requests(monkeypatch):
    # T4a：post 须把传入的 headers 透传给 requests.post（供 /api/k8s/shutdown 带凭据）
    captured = {}
    def fake_post(url, timeout=60, **kwargs):
        captured.update(kwargs)
        return FakeResp(status=200, text="ok")
    monkeypatch.setattr(requests, "post", fake_post)
    code, text = http_client.post("http://x", headers={"X-Shutdown-Token": "secret"})
    assert (code, text) == (200, "ok")
    assert captured.get("headers") == {"X-Shutdown-Token": "secret"}
    # 透传 headers 不应破坏 H1 禁跳转
    assert captured.get("allow_redirects") is False


def test_post_headers_default_none_passthrough(monkeypatch):
    # 不传 / 传 None headers 时行为不变（requests 对 headers=None 等同未设）
    captured = {}
    def fake_post(url, timeout=60, **kwargs):
        captured.update(kwargs)
        return FakeResp(status=200, text="ok")
    monkeypatch.setattr(requests, "post", fake_post)
    assert http_client.post("http://x", headers=None) == (200, "ok")
    assert captured.get("headers") is None
    assert captured.get("allow_redirects") is False
