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


def test_get_json_forwards_headers(monkeypatch):
    # P1-3：回源拉清单须带 Authorization: Bearer <pull-token>
    captured = {}
    def fake_get(url, params=None, timeout=10, **kwargs):
        captured.update(kwargs)
        return FakeResp(payload={"ok": 1})
    monkeypatch.setattr(requests, "get", fake_get)
    assert http_client.get_json("http://x", headers={"Authorization": "Bearer t"}) == {"ok": 1}
    assert captured.get("headers") == {"Authorization": "Bearer t"}
    assert captured.get("allow_redirects") is False


class FakeStreamResp:
    def __init__(self, chunks=(b"a", b"b"), status=200):
        self._chunks = chunks
        self.status_code = status
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))
    def iter_content(self, chunk_size=1):
        for c in self._chunks:
            yield c


def test_download_streams_to_file_skips_empty_chunks(monkeypatch, tmp_path):
    captured = {}
    def fake_get(url, headers=None, timeout=60, allow_redirects=False, stream=False, **k):
        captured["url"] = url
        captured["headers"] = headers
        captured["stream"] = stream
        captured["allow_redirects"] = allow_redirects
        return FakeStreamResp(chunks=[b"PKG", b"", b"DATA"])  # 空块应被跳过
    monkeypatch.setattr(requests, "get", fake_get)
    dest = tmp_path / "out.tgz"
    http_client.download("http://x/d/1", str(dest), headers={"Authorization": "Bearer t"})
    assert dest.read_bytes() == b"PKGDATA"
    assert captured["stream"] is True          # 须流式,避免大包全载内存
    assert captured["allow_redirects"] is False  # H1：禁跟随重定向
    assert captured["headers"] == {"Authorization": "Bearer t"}


def test_download_raises_on_4xx_without_writing(monkeypatch, tmp_path):
    monkeypatch.setattr(requests, "get", lambda url, **k: FakeStreamResp(status=404))
    dest = tmp_path / "out.tgz"
    with pytest.raises(requests.HTTPError):
        http_client.download("http://x/d/1", str(dest))
    assert not dest.exists()  # raise_for_status 在打开文件前抛,不留半截文件
