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
    monkeypatch.setattr(requests, "get", lambda url, params=None, timeout=10: FakeResp(payload={"a": 1}))
    assert http_client.get_json("http://x", {"k": "v"}) == {"a": 1}

def test_get_status_handles_error(monkeypatch):
    def boom(*a, **k):
        raise requests.ConnectionError("refused")
    monkeypatch.setattr(requests, "get", boom)
    assert http_client.get_status("http://x") == 0

def test_post_returns_code_and_text(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda url, timeout=60: FakeResp(status=200, text="ok"))
    assert http_client.post("http://x") == (200, "ok")
