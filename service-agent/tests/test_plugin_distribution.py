import importlib
import sys

import pytest


def _imp(monkeypatch, *, platform="http://console", token="tok", ns="ns-a"):
    # config.py 在 import 时校验 WS_URL/AGENT_KEY 否则 sys.exit;先置环境再 reimport（同 test_health_server）。
    monkeypatch.setenv("WS_URL", "ws://localhost:8080/ws/agent")
    monkeypatch.setenv("AGENT_KEY", "token")
    monkeypatch.setenv("PLATFORM_URL", platform)
    monkeypatch.setenv("PULL_TOKEN", token)
    monkeypatch.setenv("PLUGIN_NAMESPACE", ns)
    for name in ["config", "services.http_client", "services.plugin_distribution"]:
        sys.modules.pop(name, None)
    config = importlib.import_module("config")
    http_client = importlib.import_module("services.http_client")
    pd = importlib.import_module("services.plugin_distribution")
    return config, http_client, pd


def test_is_configured_true(monkeypatch):
    _config, _hc, pd = _imp(monkeypatch)
    assert pd.is_configured() is True


@pytest.mark.parametrize(
    "platform,token,ns",
    [("", "tok", "ns"), ("http://c", "", "ns"), ("http://c", "tok", "")],
)
def test_is_configured_false_when_any_missing(monkeypatch, platform, token, ns):
    _config, _hc, pd = _imp(monkeypatch, platform=platform, token=token, ns=ns)
    assert pd.is_configured() is False


def test_fetch_manifest_not_configured_raises(monkeypatch):
    _config, _hc, pd = _imp(monkeypatch, platform="")
    with pytest.raises(pd.DistributionNotConfigured):
        pd.fetch_manifest("svc-a")


def test_fetch_manifest_uses_own_ns_bearer_and_strips_base(monkeypatch):
    _config, hc, pd = _imp(monkeypatch, platform="http://console/", token="tok", ns="ns-a")
    captured = {}

    def fake_get_json(url, params=None, headers=None, timeout=10):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        return [{"pluginName": "p", "version": "1", "url": "http://console/api/distribution/download/7"}]

    monkeypatch.setattr(hc, "get_json", fake_get_json)
    out = pd.fetch_manifest("svc-a")

    assert out[0]["pluginName"] == "p"
    assert captured["url"] == "http://console/api/distribution/plugins"  # base 末尾 / 被 strip
    assert captured["params"] == {"namespace": "ns-a", "service": "svc-a"}  # 恒用本 ns,不取调用方传入
    assert captured["headers"] == {"Authorization": "Bearer tok"}


def test_fetch_manifest_non_list_raises(monkeypatch):
    _config, hc, pd = _imp(monkeypatch)
    monkeypatch.setattr(hc, "get_json", lambda *a, **k: {"not": "list"})
    with pytest.raises(RuntimeError):
        pd.fetch_manifest("svc-a")


@pytest.mark.parametrize(
    "url,expected",
    [
        ("http://console/api/distribution/download/42", "42"),
        ("http://console/api/distribution/download/42/", "42"),
        ("http://console/api/distribution/download/abc-1.0?x=1", "abc-1.0"),
    ],
)
def test_attachment_id_from_url_ok(monkeypatch, url, expected):
    _config, _hc, pd = _imp(monkeypatch)
    assert pd.attachment_id_from_url(url) == expected


@pytest.mark.parametrize("url", ["", "http://console/", None])
def test_attachment_id_from_url_empty_raises(monkeypatch, url):
    _config, _hc, pd = _imp(monkeypatch)
    with pytest.raises(ValueError):
        pd.attachment_id_from_url(url)


def test_download_to_not_configured_raises(monkeypatch):
    _config, _hc, pd = _imp(monkeypatch, token="")
    with pytest.raises(pd.DistributionNotConfigured):
        pd.download_to("7", "/tmp/x.tgz")


def test_download_to_calls_http_client_with_url_and_bearer(monkeypatch):
    _config, hc, pd = _imp(monkeypatch, platform="http://console", token="tok", ns="ns-a")
    captured = {}

    def fake_download(url, dest_path, headers=None, timeout=60):
        captured["url"] = url
        captured["dest"] = dest_path
        captured["headers"] = headers

    monkeypatch.setattr(hc, "download", fake_download)
    pd.download_to("7", "/tmp/pkg.tgz")

    assert captured["url"] == "http://console/api/distribution/download/7"
    assert captured["dest"] == "/tmp/pkg.tgz"
    assert captured["headers"] == {"Authorization": "Bearer tok"}
