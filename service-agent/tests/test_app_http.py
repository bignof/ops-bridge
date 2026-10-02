from types import SimpleNamespace

import pytest
import requests
from urllib3.exceptions import MaxRetryError, NewConnectionError, ProtocolError

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
    assert captured["url"] == "http://host.docker.internal:13099/api/k8s/shutdown"
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


def _raise(exc):
    def fake_post(url, headers, timeout):
        raise exc

    return fake_post


def _refused():
    """requests 对「连接被拒」的真实形态：ConnectionError(MaxRetryError(reason=NewConnectionError))。"""
    reason = NewConnectionError(None, "Failed to establish a new connection: [Errno 111] Connection refused")
    return requests.ConnectionError(MaxRetryError(None, "/api/k8s/shutdown", reason=reason))


@pytest.mark.parametrize(
    "exc,expected",
    [
        (_refused(), app_http.DRAIN_FAILED),  # 连不上：请求肯定没送到
        (requests.ConnectTimeout("connect timed out"), app_http.DRAIN_FAILED),
        (requests.ReadTimeout("Read timed out."), app_http.DRAIN_UNKNOWN),  # 应用可能还在排空
        (requests.ConnectionError(ProtocolError("Connection aborted.", ConnectionResetError())), app_http.DRAIN_UNKNOWN),
        (requests.exceptions.InvalidURL("bad url"), app_http.DRAIN_FAILED),
        (KeyError("unexpected"), app_http.DRAIN_UNKNOWN),
    ],
)
def test_drain_outcome_classifies_transport_errors(monkeypatch: pytest.MonkeyPatch, exc, expected) -> None:
    monkeypatch.setattr(app_http.requests, "post", _raise(exc))

    outcome, message = app_http.drain_outcome(13099)

    assert outcome == expected
    assert message


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (200, {"success": True}, app_http.DRAINED),
        (200, ["not", "an", "object"], app_http.DRAINED),
        (200, {"success": False, "message": "boom"}, app_http.DRAIN_UNKNOWN),  # 关闭标志可能已打上
        (403, None, app_http.DRAIN_FAILED),  # token 不对，应用没动手
        (404, None, app_http.DRAIN_FAILED),
        (500, None, app_http.DRAIN_UNKNOWN),
    ],
)
def test_drain_outcome_classifies_responses(monkeypatch: pytest.MonkeyPatch, status, body, expected) -> None:
    monkeypatch.setattr(
        app_http.requests,
        "post",
        lambda url, headers, timeout: SimpleNamespace(status_code=status, text=str(body), json=lambda: body),
    )

    assert app_http.drain_outcome(13099)[0] == expected


def test_drain_outcome_non_json_200_counts_as_drained(monkeypatch: pytest.MonkeyPatch) -> None:
    def bad_json():
        raise ValueError("not json")

    monkeypatch.setattr(
        app_http.requests, "post", lambda url, headers, timeout: SimpleNamespace(status_code=200, text="ok", json=bad_json)
    )

    assert app_http.drain_outcome(13099) == (app_http.DRAINED, "drained (non-JSON response)")


@pytest.mark.parametrize("status,expected", [(200, "ready"), (503, "not_ready"), (404, "unknown")])
def test_readiness_state_maps_status(monkeypatch: pytest.MonkeyPatch, status, expected) -> None:
    monkeypatch.setattr(app_http.requests, "get", lambda url, timeout: SimpleNamespace(status_code=status))

    assert app_http.readiness_state(13099) == expected


def test_readiness_state_unreachable_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url, timeout):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(app_http.requests, "get", fake_get)

    assert app_http.readiness_state(13099) == "unknown"


def test_wait_healthy_succeeds_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_http.requests, "get", lambda url, timeout: SimpleNamespace(status_code=200))

    ok, message = app_http.wait_healthy(13099, timeout=1, interval=0.01)

    assert ok is True


def test_wait_healthy_uses_docker_internal_host(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_get(url, timeout):
        captured["url"] = url
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(app_http.requests, "get", fake_get)

    app_http.wait_healthy(13099, timeout=1, interval=0.01)

    assert captured["url"] == "http://host.docker.internal:13099/api/health/ready"


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
