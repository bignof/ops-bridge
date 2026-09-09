import gzip
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from core import log_fetch


class FakeWs:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    def send(self, payload: str) -> None:
        self.messages.append(json.loads(payload))


def _project(tmp_path: Path) -> tuple[Path, Path]:
    proj = tmp_path / "app"
    (proj / "logs" / "main").mkdir(parents=True, exist_ok=True)  # 同一用例里可能建两次
    (proj / "docker-compose.yaml").write_text("services: {}\n")
    f = proj / "logs" / "main" / "orchidea_2026-09-08.log"
    f.write_bytes("".join(f"2026-09-08 10:00:{i:02d} [info ] line {i}\n" for i in range(500)).encode("utf-8"))
    return proj, f


@pytest.fixture(autouse=True)
def _reset_fetch_state():
    log_fetch._reset_for_tests()
    yield
    log_fetch._reset_for_tests()


def test_hub_http_base_derivation_and_override() -> None:
    assert log_fetch.hub_http_base("ws://hub.example:13000/ws/agent", "") == "http://hub.example:13000"
    assert log_fetch.hub_http_base("wss://hub.example/prefix/ws/agent", "") == "https://hub.example/prefix"
    assert log_fetch.hub_http_base("ws://hub.example/ws/agent/extra", "") == "http://hub.example"
    assert log_fetch.hub_http_base("ws://hub.example/other", "") == "http://hub.example/other"
    assert log_fetch.hub_http_base("ws://hub.example/ws/agent", "https://override.example/") == "https://override.example"


def test_gzip_chunks_roundtrip_and_abort(tmp_path: Path) -> None:
    proj, f = _project(tmp_path)
    counter: dict = {}
    body = b"".join(log_fetch.gzip_chunks(str(f), threading.Event(), counter))
    assert gzip.decompress(body) == f.read_bytes()
    assert counter["sent"] == len(body)

    abort = threading.Event()
    abort.set()
    with pytest.raises(log_fetch.FetchAborted):
        list(log_fetch.gzip_chunks(str(f), abort, {}))


def _run(monkeypatch, tmp_path, post_impl, extra=None):
    proj, f = _project(tmp_path)
    monkeypatch.setattr(log_fetch.requests, "post", post_impl)
    monkeypatch.setattr(log_fetch, "HUB_HTTP_URL", "http://hub.example")
    assert log_fetch._slots.acquire(blocking=False)  # 直接跑线程体前先占槽位，与 start_fetch 的语义一致
    ws = FakeWs()
    data = {
        "requestId": "r1",
        "archiveId": 7,
        "dir": str(proj),
        "logDir": None,
        "file": "main/orchidea_2026-09-08.log",
        "uploadPath": "/api/hub:uploadLogArchive?archiveId=7",
        "uploadToken": "tok",
    }
    data.update(extra or {})
    log_fetch._run_fetch(ws, data, str(f))
    return ws, f


def test_run_fetch_success_sends_headers_and_result(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict = {}

    def fake_post(url, data=None, headers=None, timeout=None):
        seen.update(url=url, body=b"".join(data), headers=headers, timeout=timeout)
        return SimpleNamespace(status_code=200, text='{"ok":true}')

    ws, f = _run(monkeypatch, tmp_path, fake_post)
    assert seen["url"] == "http://hub.example/api/hub:uploadLogArchive?archiveId=7"
    assert seen["headers"]["Content-Type"] == "application/gzip"
    assert seen["headers"]["X-Hub-Upload-Token"] == "tok"
    assert seen["headers"]["X-Hub-Raw-Size"] == str(f.stat().st_size)
    assert len(seen["headers"]["X-Hub-File-Mtime"]) == 19
    assert seen["timeout"] == (10, 600)
    assert gzip.decompress(seen["body"]) == f.read_bytes()
    result = ws.messages[-1]
    assert result["type"] == "logfile_fetch_result" and result["ok"] is True
    assert result["requestId"] == "r1" and result["archiveId"] == 7
    assert result["sizeRaw"] == f.stat().st_size and result["sizeSent"] == len(seen["body"])


def test_run_fetch_http_error_and_exception(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ws, _ = _run(monkeypatch, tmp_path, lambda *a, **k: SimpleNamespace(status_code=413, text="too large" * 100))
    err = ws.messages[-1]
    assert err["ok"] is False and err["error"]["code"] == "upload_failed"
    assert err["error"]["message"].startswith("HTTP 413: too large") and len(err["error"]["message"]) <= 220

    def boom(*a, **k):
        raise requests.ConnectionError("refused")

    ws2, _ = _run(monkeypatch, tmp_path, boom)
    assert ws2.messages[-1]["error"]["code"] == "upload_failed" and "refused" in ws2.messages[-1]["error"]["message"]


def test_start_fetch_validates_paths_and_concurrency(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    proj, f = _project(tmp_path)
    ws = FakeWs()
    started: list[object] = []

    class FakeThread:
        def __init__(self, target=None, args=(), daemon=None, name=None) -> None:
            self.target, self.args = target, args

        def start(self) -> None:
            started.append(self)

    monkeypatch.setattr(log_fetch.threading, "Thread", FakeThread)
    base = {"requestId": "r", "archiveId": 1, "dir": str(proj), "uploadPath": "/x", "uploadToken": "t"}

    log_fetch.start_fetch(ws, {**base, "file": "main/nope.log"})
    assert ws.messages[-1]["ok"] is False and ws.messages[-1]["error"]["code"] == "not_found"

    log_fetch.start_fetch(ws, {**base, "file": "../docker-compose.yaml"})
    assert ws.messages[-1]["error"]["code"] == "forbidden"

    log_fetch.start_fetch(ws, {**base, "file": "main/orchidea_2026-09-08.log", "uploadPath": ""})
    assert ws.messages[-1]["error"]["code"] == "invalid"

    log_fetch.start_fetch(ws, {**base, "file": "main/orchidea_2026-09-08.log"})
    assert len(started) == 1  # 第一个进线程
    log_fetch.start_fetch(ws, {**base, "requestId": "r2", "file": "main/orchidea_2026-09-08.log"})
    assert ws.messages[-1]["error"]["code"] == "busy" and ws.messages[-1]["requestId"] == "r2"
    log_fetch._release()  # 模拟线程结束
    log_fetch.start_fetch(ws, {**base, "requestId": "r3", "file": "main/orchidea_2026-09-08.log"})
    assert len(started) == 2
    log_fetch._release()

    log_fetch.start_fetch(ws, {**base, "file": "main/orchidea_2026-09-08.log", "requestId": ""})  # 缺 requestId 忽略
    assert len(started) == 2


def test_abort_all_sets_event_and_run_reports_aborted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_post(url, data=None, headers=None, timeout=None):
        log_fetch.abort_all()  # 上传中途 hub 断连
        b"".join(data)
        return SimpleNamespace(status_code=200, text="")

    ws, _ = _run(monkeypatch, tmp_path, fake_post)
    assert ws.messages[-1]["ok"] is False and "aborted" in ws.messages[-1]["error"]["message"]
    assert not log_fetch._abort.is_set()  # 跑完复位，下一次 fetch 可用
