import importlib
import sys

import pytest


def _imp(monkeypatch, *, platform="http://console", token="tok", ns="ns-a"):
    # core.prewarm → core.handlers / services.* → config，config 在缺 WS_URL/AGENT_KEY 时 sys.exit；
    # 先置环境再 reimport（同 test_plugin_server 套路）。
    monkeypatch.setenv("WS_URL", "ws://localhost:8080/ws/agent")
    monkeypatch.setenv("AGENT_KEY", "token")
    monkeypatch.setenv("PLATFORM_URL", platform)
    monkeypatch.setenv("PULL_TOKEN", token)
    monkeypatch.setenv("PLUGIN_NAMESPACE", ns)
    for name in [
        "config",
        "services.http_client",
        "services.plugin_cache",
        "services.plugin_distribution",
        "core.handlers",
        "core.prewarm",
    ]:
        sys.modules.pop(name, None)
    return importlib.import_module("core.prewarm")


def _capture_send(m):
    """把 prewarm 模块里绑定的 send_message 换成捕获器，返回收集列表。"""
    sent: list[dict] = []
    m.send_message = lambda ws, payload: sent.append(payload)
    return sent


def _set_configured(monkeypatch, m, value: bool):
    """桩 is_configured，表达「是否配齐回源」的行为契约（monkeypatch 自动还原，避免污染后续用例）。

    不靠 config.PLATFORM_URL 真实值——prewarm 内部 `from services import plugin_distribution`，
    跨用例 reimport 时该子模块属性可能被前序测试的 monkeypatch.setenv 污染（拿到陈旧 config），
    故被测点（配/未配的回包语义）一律用桩控制，稳定且精准。
    """
    monkeypatch.setattr(m.plugin_distribution, "is_configured", lambda: value)


def _url(aid):
    return f"http://console/api/distribution/download/{aid}"


# --------------------------------------------------------------------------- #
# 未配置
# --------------------------------------------------------------------------- #


def test_prewarm_not_configured_returns_failed(monkeypatch):
    m = _imp(monkeypatch)
    _set_configured(monkeypatch, m, False)  # 未配齐回源
    sent = _capture_send(m)

    # 未配置应直接回 failed，绝不触碰 fetch_manifest（哨兵：被调到就炸）。
    monkeypatch.setattr(
        m.plugin_distribution,
        "fetch_manifest",
        lambda service: (_ for _ in ()).throw(AssertionError("未配置时不应回源 fetch_manifest")),
    )

    m.handle_prewarm(None, {"requestId": "r1", "services": ["svc-a"]})

    assert len(sent) == 1
    msg = sent[0]
    assert msg["type"] == "prewarm-result"
    assert msg["requestId"] == "r1"
    assert msg["status"] == "failed"
    assert msg["warmed"] == 0
    assert "未配置" in msg["error"]


# --------------------------------------------------------------------------- #
# 成功路径
# --------------------------------------------------------------------------- #


def test_prewarm_multi_service_success_counts_warmed(monkeypatch):
    m = _imp(monkeypatch)
    _set_configured(monkeypatch, m, True)
    sent = _capture_send(m)

    manifests = {
        "svc-a": [
            {"pluginName": "p1", "version": "1", "url": _url("7")},
            {"pluginName": "p2", "version": "2", "url": _url("8")},
        ],
        "svc-b": [
            {"pluginName": "p3", "version": "3", "url": _url("9")},
        ],
    }
    monkeypatch.setattr(m.plugin_distribution, "fetch_manifest", lambda service: manifests[service])

    fetched: list[str] = []

    def fake_get_or_fetch(aid, fetcher):
        # 真正调 fetcher 以覆盖闭包里 download_to 的那行
        fetcher("/tmp/ignored")
        return f"/cache/{aid}.tgz"

    def fake_download_to(aid, dest):
        fetched.append(aid)

    monkeypatch.setattr(m.plugin_cache, "get_or_fetch", fake_get_or_fetch)
    monkeypatch.setattr(m.plugin_distribution, "download_to", fake_download_to)

    m.handle_prewarm("WS", {"requestId": "r2", "services": ["svc-a", "svc-b"]})

    assert len(sent) == 1
    msg = sent[0]
    assert msg["type"] == "prewarm-result"
    assert msg["requestId"] == "r2"
    assert msg["status"] == "success"
    assert msg["warmed"] == 3  # 2 + 1
    assert "error" not in msg
    # 闭包确实把正确 attachmentId 传给了 download_to（默认参数绑定，无延迟绑定 bug）
    assert fetched == ["7", "8", "9"]


# --------------------------------------------------------------------------- #
# 部分失败（best-effort）
# --------------------------------------------------------------------------- #


def test_prewarm_skips_bad_plugin_and_counts_only_success(monkeypatch):
    m = _imp(monkeypatch)
    _set_configured(monkeypatch, m, True)
    sent = _capture_send(m)

    manifest = [
        {"pluginName": "good", "version": "1", "url": _url("7")},
        {"pluginName": "bad-url", "version": "9", "url": ""},  # attachment_id_from_url 抛 → 跳过
        {"pluginName": "dl-fail", "version": "2", "url": _url("8")},  # 下载抛 → 跳过
        {"pluginName": "good2", "version": "3", "url": _url("10")},
    ]
    monkeypatch.setattr(m.plugin_distribution, "fetch_manifest", lambda service: manifest)

    def fake_get_or_fetch(aid, fetcher):
        if aid == "8":
            raise RuntimeError("network down")
        return f"/cache/{aid}.tgz"

    monkeypatch.setattr(m.plugin_cache, "get_or_fetch", fake_get_or_fetch)

    m.handle_prewarm("WS", {"requestId": "r3", "services": ["svc-a"]})

    msg = sent[0]
    assert msg["status"] == "success"
    assert msg["warmed"] == 2  # 仅 good + good2；bad-url / dl-fail 被跳过


def test_prewarm_single_service_manifest_failure_continues_others(monkeypatch):
    m = _imp(monkeypatch)
    _set_configured(monkeypatch, m, True)
    sent = _capture_send(m)

    def fake_fetch_manifest(service):
        if service == "svc-bad":
            raise RuntimeError("manifest 502")
        return [{"pluginName": "p", "version": "1", "url": _url("7")}]

    monkeypatch.setattr(m.plugin_distribution, "fetch_manifest", fake_fetch_manifest)
    monkeypatch.setattr(m.plugin_cache, "get_or_fetch", lambda aid, fetcher: f"/cache/{aid}.tgz")

    m.handle_prewarm("WS", {"requestId": "r4", "services": ["svc-bad", "svc-ok"]})

    msg = sent[0]
    # svc-bad 整段失败被跳过，svc-ok 仍成功 → 总体 success
    assert msg["status"] == "success"
    assert msg["warmed"] == 1


# --------------------------------------------------------------------------- #
# 全失败 / 空
# --------------------------------------------------------------------------- #


def test_prewarm_all_services_fail_returns_failed_with_error(monkeypatch):
    m = _imp(monkeypatch)
    _set_configured(monkeypatch, m, True)
    sent = _capture_send(m)

    monkeypatch.setattr(
        m.plugin_distribution,
        "fetch_manifest",
        lambda service: (_ for _ in ()).throw(RuntimeError("accessToken=secret123 leaked")),
    )

    m.handle_prewarm("WS", {"requestId": "r5", "services": ["svc-a"]})

    msg = sent[0]
    assert msg["status"] == "failed"
    assert msg["warmed"] == 0
    # 脱敏：accessToken 被打码，secret 不外泄
    assert "accessToken=***" in msg["error"]
    assert "secret123" not in msg["error"]


def test_prewarm_empty_services_returns_failed_without_error(monkeypatch):
    m = _imp(monkeypatch)
    sent = _capture_send(m)

    m.handle_prewarm("WS", {"requestId": "r6", "services": []})

    msg = sent[0]
    assert msg["status"] == "failed"
    assert msg["warmed"] == 0
    # 无任何 service → last_error 为 None，回包不带 error 字段
    assert "error" not in msg


def test_prewarm_missing_services_key_defaults_empty(monkeypatch):
    m = _imp(monkeypatch)
    sent = _capture_send(m)

    m.handle_prewarm("WS", {"requestId": "r7"})  # 无 services 字段

    msg = sent[0]
    assert msg["status"] == "failed"
    assert msg["warmed"] == 0
    assert "error" not in msg
