from types import SimpleNamespace

import pytest
import requests

from services import app_http


def test_drain_success(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_post(url, headers, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["timeout"] = timeout
        return SimpleNamespace(status_code=200, text='{"success": true}', json=lambda: {"success": True})

    monkeypatch.setattr(app_http.requests, "post", fake_post)

    ok, message = app_http.drain(13099, token="secret")

    assert ok is True
    assert captured["url"] == "http://127.0.0.1:13099/api/k8s/shutdown"
    assert captured["headers"] == {"X-Shutdown-Token": "secret"}


def test_drain_without_token_omits_header(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_post(url, headers, timeout):
        captured["headers"] = headers
        return SimpleNamespace(status_code=200, text='{"success": true}', json=lambda: {"success": True})

    monkeypatch.setattr(app_http.requests, "post", fake_post)

    app_http.drain(13099)

    assert captured["headers"] == {}


def test_drain_reports_body_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(url, headers, timeout):
        return SimpleNamespace(
            status_code=200, text='{"success": false}', json=lambda: {"success": False, "message": "boom"}
        )

    monkeypatch.setattr(app_http.requests, "post", fake_post)

    ok, message = app_http.drain(13099)

    assert ok is False
    assert "boom" in message


def test_drain_reports_non_200_status(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(url, headers, timeout):
        return SimpleNamespace(status_code=403, text="forbidden")

    monkeypatch.setattr(app_http.requests, "post", fake_post)

    ok, message = app_http.drain(13099)

    assert ok is False
    assert "403" in message


def test_drain_reports_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(url, headers, timeout):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(app_http.requests, "post", fake_post)

    ok, message = app_http.drain(13099)

    assert ok is False
    assert "refused" in message


def test_wait_healthy_succeeds_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_http.requests, "get", lambda url, timeout: SimpleNamespace(status_code=200))

    ok, message = app_http.wait_healthy(13099, timeout=1, interval=0.01)

    assert ok is True


def test_wait_healthy_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_http.requests, "get", lambda url, timeout: SimpleNamespace(status_code=503))

    ok, message = app_http.wait_healthy(13099, timeout=0.05, interval=0.01)

    assert ok is False
    assert "503" in message or "timed out" in message


def test_wait_healthy_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fake_get(url, timeout):
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.ConnectionError("not up yet")
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(app_http.requests, "get", fake_get)

    ok, message = app_http.wait_healthy(13099, timeout=2, interval=0.01)

    assert ok is True
    assert calls["n"] == 3
