import os
os.environ.setdefault("WS_URL", "ws://test")
os.environ.setdefault("AGENT_KEY", "test-key")

import pytest

import config
from core import graceful
from services import http_client


# ─────────────────────────────────────────────
# _validate_health_base_url（从 rolling 移过来，SSRF 守卫）
# ─────────────────────────────────────────────

def test_validate_allows_private_ip():
    # 内网 IP 放行，原样返回 base
    assert graceful._validate_health_base_url("http://192.168.0.30:18029") == "http://192.168.0.30:18029"


def test_validate_allows_loopback():
    assert graceful._validate_health_base_url("http://127.0.0.1:18029") == "http://127.0.0.1:18029"


def test_validate_rejects_empty():
    with pytest.raises(ValueError):
        graceful._validate_health_base_url("")


def test_validate_rejects_bad_scheme():
    with pytest.raises(ValueError):
        graceful._validate_health_base_url("file:///etc/passwd")


def test_validate_rejects_missing_host():
    with pytest.raises(ValueError):
        graceful._validate_health_base_url("http:///api")


def test_validate_rejects_public_ip():
    with pytest.raises(ValueError):
        graceful._validate_health_base_url("http://8.8.8.8:18029")


def test_validate_rejects_domain():
    # 域名一律拒绝，防 DNS 解析到公网
    with pytest.raises(ValueError):
        graceful._validate_health_base_url("http://evil.example.com:18029")


# ─────────────────────────────────────────────
# drain（纯函数：校验 base → POST /api/k8s/shutdown）
# ─────────────────────────────────────────────

def test_drain_posts_shutdown_on_valid_base(monkeypatch):
    """合法内网 base：调 http_client.post 打 /api/k8s/shutdown，超时透传，返回 200 不抛错。"""
    calls = []

    def fake_post(url, timeout=60):
        calls.append((url, timeout))
        return 200, "ok"

    monkeypatch.setattr(http_client, "post", fake_post)
    graceful.drain("http://192.168.0.30:18029", shutdown_timeout=45)

    assert calls == [("http://192.168.0.30:18029/api/k8s/shutdown", 45)]


def test_drain_uses_default_timeout(monkeypatch):
    """不传 shutdown_timeout 时用默认 60。"""
    calls = []
    monkeypatch.setattr(http_client, "post", lambda url, timeout=60: (calls.append((url, timeout)) or (200, "ok")))
    graceful.drain("http://10.0.0.5:18029")
    assert calls == [("http://10.0.0.5:18029/api/k8s/shutdown", 60)]


def test_drain_raises_on_non_200(monkeypatch):
    """shutdown 返回非 200 → RuntimeError（携带状态码）。"""
    monkeypatch.setattr(http_client, "post", lambda url, timeout=60: (500, "err"))
    with pytest.raises(RuntimeError) as exc:
        graceful.drain("http://192.168.0.30:18029", shutdown_timeout=60)
    assert "500" in str(exc.value)


def test_drain_rejects_public_ip_without_posting(monkeypatch):
    """公网 IP base：drain 在发 shutdown 前就 ValueError，绝不调 http_client.post。"""
    posted = {"n": 0}

    def fake_post(url, timeout=60):
        posted["n"] += 1
        return 200, "ok"

    monkeypatch.setattr(http_client, "post", fake_post)
    with pytest.raises(ValueError):
        graceful.drain("http://8.8.8.8:18029", shutdown_timeout=60)
    assert posted["n"] == 0


def test_drain_rejects_domain_without_posting(monkeypatch):
    """域名 base：drain 在发 shutdown 前就 ValueError，绝不调 http_client.post。"""
    posted = {"n": 0}
    monkeypatch.setattr(http_client, "post", lambda url, timeout=60: (posted.__setitem__("n", posted["n"] + 1) or (200, "ok")))
    with pytest.raises(ValueError):
        graceful.drain("http://evil.example.com:18029", shutdown_timeout=60)
    assert posted["n"] == 0


def test_drain_rejects_empty_base_without_posting(monkeypatch):
    """空 base：drain ValueError，不调 http_client.post。"""
    posted = {"n": 0}
    monkeypatch.setattr(http_client, "post", lambda url, timeout=60: (posted.__setitem__("n", posted["n"] + 1) or (200, "ok")))
    with pytest.raises(ValueError):
        graceful.drain("", shutdown_timeout=60)
    assert posted["n"] == 0


# ─────────────────────────────────────────────
# shutdown_headers（T4a：opt-in 凭据透传）
# ─────────────────────────────────────────────

def test_shutdown_headers_none_when_token_unset(monkeypatch):
    # 未配 K8S_SHUTDOWN_TOKEN（空）→ 返回 None（不带 header，向后兼容）
    monkeypatch.setattr(config, "K8S_SHUTDOWN_TOKEN", "")
    assert graceful.shutdown_headers() is None


def test_shutdown_headers_set_when_token_present(monkeypatch):
    # 配了 token → 返回 {'X-Shutdown-Token': <token>}
    monkeypatch.setattr(config, "K8S_SHUTDOWN_TOKEN", "secret")
    assert graceful.shutdown_headers() == {"X-Shutdown-Token": "secret"}


# ─────────────────────────────────────────────
# drain 携带凭据（方案 A：配了 token 才带 headers 关键字）
# ─────────────────────────────────────────────

def test_drain_sends_token_header_when_configured(monkeypatch):
    """配 K8S_SHUTDOWN_TOKEN：drain 调 http_client.post 带 headers={'X-Shutdown-Token': ...}。"""
    monkeypatch.setattr(config, "K8S_SHUTDOWN_TOKEN", "secret")
    captured = {}

    def fake_post(url, timeout=60, headers=None):
        captured["url"] = url
        captured["timeout"] = timeout
        captured["headers"] = headers
        return 200, "ok"

    monkeypatch.setattr(http_client, "post", fake_post)
    graceful.drain("http://192.168.0.30:18029", shutdown_timeout=45)

    assert captured["url"] == "http://192.168.0.30:18029/api/k8s/shutdown"
    assert captured["timeout"] == 45
    assert captured["headers"] == {"X-Shutdown-Token": "secret"}


def test_drain_omits_headers_kwarg_when_token_unset(monkeypatch):
    """未配 token：drain 调 post 时不传 headers 关键字（逐字节兼容旧调用，桩签名仅 url/timeout）。"""
    monkeypatch.setattr(config, "K8S_SHUTDOWN_TOKEN", "")
    captured = {}

    # 桩故意只接受 (url, timeout)：若实现误带 headers= 关键字，这里会 TypeError 而失败
    def fake_post(url, timeout=60):
        captured["url"] = url
        captured["timeout"] = timeout
        return 200, "ok"

    monkeypatch.setattr(http_client, "post", fake_post)
    graceful.drain("http://192.168.0.30:18029", shutdown_timeout=60)

    assert captured == {"url": "http://192.168.0.30:18029/api/k8s/shutdown", "timeout": 60}
