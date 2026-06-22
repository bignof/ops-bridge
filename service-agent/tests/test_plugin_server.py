import importlib
import io
import json
import sys

import pytest


def _imp(monkeypatch):
    monkeypatch.setenv("WS_URL", "ws://localhost:8080/ws/agent")
    monkeypatch.setenv("AGENT_KEY", "token")
    monkeypatch.setenv("PLUGIN_SERVE_HOST", "127.0.0.1")
    monkeypatch.setenv("PLUGIN_SERVE_PORT", "18082")
    for name in [
        "config",
        "services.http_client",
        "services.plugin_cache",
        "services.plugin_distribution",
        "core.plugin_server",
    ]:
        sys.modules.pop(name, None)
    return importlib.import_module("core.plugin_server")


def _handler(module, path, headers=None):
    h = module._PluginHandler.__new__(module._PluginHandler)
    h.path = path
    h.headers = headers if headers is not None else {}
    h.wfile = io.BytesIO()
    responses: list[int] = []
    sent: list[tuple[str, str]] = []
    h.send_response = lambda code: responses.append(code)
    h.send_header = lambda k, v: sent.append((k, v))
    h.end_headers = lambda: None
    return h, responses, sent


def _json_body(h):
    return json.loads(h.wfile.getvalue().decode("utf-8"))


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #


def test_unknown_path_returns_404(monkeypatch):
    m = _imp(monkeypatch)
    h, responses, _ = _handler(m, "/nope")
    h.do_GET()
    assert responses == [404]
    assert _json_body(h) == {"error": "not found"}


def test_log_message_is_silent(monkeypatch):
    m = _imp(monkeypatch)
    h, _, _ = _handler(m, "/plugins")
    assert m._PluginHandler.log_message(h, "%s") is None


# --------------------------------------------------------------------------- #
# /plugins
# --------------------------------------------------------------------------- #


def test_plugins_missing_service_400(monkeypatch):
    m = _imp(monkeypatch)
    h, responses, _ = _handler(m, "/plugins")
    h.do_GET()
    assert responses == [400]


def test_plugins_not_configured_503(monkeypatch):
    m = _imp(monkeypatch)

    def boom(service):
        raise m.plugin_distribution.DistributionNotConfigured("x")

    monkeypatch.setattr(m.plugin_distribution, "fetch_manifest", boom)
    h, responses, _ = _handler(m, "/plugins?service=svc")
    h.do_GET()
    assert responses == [503]


def test_plugins_upstream_failure_502(monkeypatch):
    m = _imp(monkeypatch)
    monkeypatch.setattr(
        m.plugin_distribution, "fetch_manifest", lambda service: (_ for _ in ()).throw(RuntimeError("net"))
    )
    h, responses, _ = _handler(m, "/plugins?service=svc")
    h.do_GET()
    assert responses == [502]


def test_plugins_success_rewrites_url_with_host_and_ignores_ns_and_skips_bad(monkeypatch):
    m = _imp(monkeypatch)
    manifest = [
        {"pluginName": "p1", "version": "1.0", "url": "http://console/api/distribution/download/7"},
        {"pluginName": "bad", "version": "9", "url": ""},  # 无法解析 attachmentId → 跳过
        {"pluginName": "p2", "version": "2.0", "url": "http://console/api/distribution/download/8"},
    ]
    monkeypatch.setattr(m.plugin_distribution, "fetch_manifest", lambda service: manifest)
    # worker 传了 namespace=ignored,应被忽略(只用 service);Host 决定改写后的 base。
    h, responses, _ = _handler(m, "/plugins?service=svc&namespace=ignored", headers={"Host": "10.0.0.1:18082"})
    h.do_GET()
    assert responses == [200]
    assert _json_body(h) == [
        {"pluginName": "p1", "version": "1.0", "url": "http://10.0.0.1:18082/download/7"},
        {"pluginName": "p2", "version": "2.0", "url": "http://10.0.0.1:18082/download/8"},
    ]


def test_plugins_fallback_base_without_host_header(monkeypatch):
    m = _imp(monkeypatch)
    monkeypatch.setattr(
        m.plugin_distribution,
        "fetch_manifest",
        lambda service: [{"pluginName": "p", "version": "1", "url": "http://c/api/distribution/download/3"}],
    )
    h, responses, _ = _handler(m, "/plugins?service=svc", headers={})  # 无 Host → 回退配置地址
    h.do_GET()
    assert responses == [200]
    assert _json_body(h)[0]["url"] == "http://127.0.0.1:18082/download/3"


# --------------------------------------------------------------------------- #
# /download
# --------------------------------------------------------------------------- #


def test_download_empty_id_404(monkeypatch):
    m = _imp(monkeypatch)
    h, responses, _ = _handler(m, "/download/")
    h.do_GET()
    assert responses == [404]


def test_download_bad_id_404(monkeypatch):
    m = _imp(monkeypatch)

    def boom(aid, fetcher):
        raise ValueError("非法 attachmentId")

    monkeypatch.setattr(m.plugin_cache, "get_or_fetch", boom)
    h, responses, _ = _handler(m, "/download/..bad")
    h.do_GET()
    assert responses == [404]


def test_download_not_configured_503(monkeypatch):
    m = _imp(monkeypatch)

    def boom(aid, fetcher):
        raise m.plugin_distribution.DistributionNotConfigured("x")

    monkeypatch.setattr(m.plugin_cache, "get_or_fetch", boom)
    h, responses, _ = _handler(m, "/download/7")
    h.do_GET()
    assert responses == [503]


def test_download_upstream_failure_502(monkeypatch):
    m = _imp(monkeypatch)

    def boom(aid, fetcher):
        raise RuntimeError("net down")

    monkeypatch.setattr(m.plugin_cache, "get_or_fetch", boom)
    h, responses, _ = _handler(m, "/download/7")
    h.do_GET()
    assert responses == [502]


def test_download_success_streams_cached_file(monkeypatch, tmp_path):
    m = _imp(monkeypatch)
    pkg = tmp_path / "7.tgz"
    pkg.write_bytes(b"PKGDATA")
    monkeypatch.setattr(m.plugin_cache, "get_or_fetch", lambda aid, fetcher: str(pkg))
    h, responses, sent = _handler(m, "/download/7")
    h.do_GET()
    assert responses == [200]
    assert ("Content-Type", "application/gzip") in sent
    assert ("Content-Length", "7") in sent
    assert h.wfile.getvalue() == b"PKGDATA"


def test_download_miss_invokes_fetcher_then_serves(monkeypatch, tmp_path):
    # 覆盖 get_or_fetch 的 fetcher 闭包真正回源(download_to)的路径。
    m = _imp(monkeypatch)
    pkg = tmp_path / "9.tgz"
    calls = {}

    def fake_get_or_fetch(aid, fetcher):
        dest = str(pkg)
        fetcher(dest)  # 模拟未命中:真正调 fetcher 落盘
        return dest

    def fake_download_to(attachment_id, dest_path):
        calls["aid"] = attachment_id
        calls["dest"] = dest_path
        with open(dest_path, "wb") as f:
            f.write(b"FETCHED")

    monkeypatch.setattr(m.plugin_cache, "get_or_fetch", fake_get_or_fetch)
    monkeypatch.setattr(m.plugin_distribution, "download_to", fake_download_to)
    h, responses, _ = _handler(m, "/download/9")
    h.do_GET()
    assert responses == [200]
    assert calls == {"aid": "9", "dest": str(pkg)}
    assert h.wfile.getvalue() == b"FETCHED"


# --------------------------------------------------------------------------- #
# server 启停
# --------------------------------------------------------------------------- #


def test_start_plugin_server_creates_background_thread(monkeypatch):
    m = _imp(monkeypatch)
    server_calls: list = []
    thread_calls: list = []

    class FakeServer:
        def __init__(self, address, handler):
            server_calls.append((address, handler))

        def serve_forever(self):
            return None

    class FakeThread:
        def __init__(self, target, daemon):
            self.target = target
            thread_calls.append((target, daemon))

        def start(self):
            self.target()

    monkeypatch.setattr(m, "ThreadingHTTPServer", FakeServer)
    monkeypatch.setattr(m.threading, "Thread", FakeThread)

    server = m.start_plugin_server()

    assert isinstance(server, FakeServer)
    assert server_calls == [(("127.0.0.1", 18082), m._PluginHandler)]
    assert thread_calls[0][1] is True


def test_maybe_start_starts_when_configured(monkeypatch):
    m = _imp(monkeypatch)
    monkeypatch.setattr(m.plugin_distribution, "is_configured", lambda: True)
    monkeypatch.setattr(m, "start_plugin_server", lambda: "SERVER")
    assert m.maybe_start_plugin_server() == "SERVER"


def test_maybe_start_skips_when_not_configured(monkeypatch):
    m = _imp(monkeypatch)
    monkeypatch.setattr(m.plugin_distribution, "is_configured", lambda: False)

    def must_not_call():
        raise AssertionError("未配置时不应启动 server")

    monkeypatch.setattr(m, "start_plugin_server", must_not_call)
    assert m.maybe_start_plugin_server() is None
