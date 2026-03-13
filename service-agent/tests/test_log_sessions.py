import io

import pytest

from core import log_sessions


class FakeWebSocket:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, payload: str) -> None:
        self.messages.append(payload)


class FakeProcess:
    def __init__(self, output: str, returncode: int = 0) -> None:
        self.stdout = io.StringIO(output)
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode if self.terminated else None

    def wait(self, timeout=None):
        self.terminated = True
        return self.returncode

    def terminate(self):
        self.terminated = True
        if self.returncode is None:
            self.returncode = -15

    def kill(self):
        self.killed = True
        self.terminated = True
        if self.returncode is None:
            self.returncode = -9


class ImmediateThread:
    def __init__(self, target, kwargs, daemon):
        self.target = target
        self.kwargs = kwargs

    def start(self):
        self.target(**self.kwargs)


def _decode_messages(ws: FakeWebSocket) -> list[dict]:
    import json

    return [json.loads(item) for item in ws.messages]


@pytest.fixture(autouse=True)
def clear_sessions():
    log_sessions._sessions.clear()
    yield
    log_sessions._sessions.clear()


def test_start_log_session_validates_missing_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    ws = FakeWebSocket()
    monkeypatch.setattr(log_sessions.os.path, "isdir", lambda value: False)

    log_sessions.start_log_session(ws, {"sessionId": "logs-1", "dir": "/srv/a"})

    decoded = _decode_messages(ws)
    assert decoded == [
        {
            "type": "logs_error",
            "sessionId": "logs-1",
            "error": "Directory not found: /srv/a",
        }
    ]


def test_start_log_session_validates_missing_compose_and_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    ws = FakeWebSocket()
    monkeypatch.setattr(log_sessions.os.path, "isdir", lambda value: True)
    monkeypatch.setattr(log_sessions, "find_compose_file", lambda project_dir: None)

    log_sessions.start_log_session(ws, {"sessionId": "logs-2", "dir": "/srv/a"})

    decoded = _decode_messages(ws)
    assert decoded[0]["error"] == "No docker-compose.yaml/yml found in /srv/a"

    ws = FakeWebSocket()
    monkeypatch.setattr(log_sessions, "find_compose_file", lambda project_dir: "compose.yml")
    log_sessions.start_log_session(ws, {"sessionId": "logs-3", "dir": "/srv/a", "tail": 0})
    assert _decode_messages(ws)[0]["error"] == "Tail must be a positive integer"


def test_start_log_session_streams_chunks_and_finishes(monkeypatch: pytest.MonkeyPatch) -> None:
    ws = FakeWebSocket()
    monkeypatch.setattr(log_sessions.os.path, "isdir", lambda value: True)
    monkeypatch.setattr(log_sessions, "find_compose_file", lambda project_dir: "compose.yml")
    monkeypatch.setattr(log_sessions, "open_compose_process", lambda project_dir, args: FakeProcess("line-1\nline-2\n"))
    monkeypatch.setattr(log_sessions.threading, "Thread", ImmediateThread)

    log_sessions.start_log_session(
        ws,
        {
            "sessionId": "logs-4",
            "dir": "/srv/a",
            "service": "api",
            "tail": 20,
            "timestamps": True,
        },
    )

    decoded = _decode_messages(ws)
    assert decoded[0] == {
        "type": "logs_started",
        "sessionId": "logs-4",
        "service": "api",
        "tail": 20,
        "timestamps": True,
    }
    assert decoded[1] == {
        "type": "logs_chunk",
        "sessionId": "logs-4",
        "chunk": "line-1\n",
    }
    assert decoded[2] == {
        "type": "logs_chunk",
        "sessionId": "logs-4",
        "chunk": "line-2\n",
    }
    assert decoded[3] == {
        "type": "logs_finished",
        "sessionId": "logs-4",
        "exitCode": 0,
        "stopped": False,
        "chunks": 2,
    }
    assert log_sessions._sessions == {}


def test_stop_log_session_terminates_running_process() -> None:
    process = FakeProcess("", returncode=None)
    log_sessions._sessions["logs-5"] = {
        "process": process,
        "stop_requested": False,
    }

    log_sessions.stop_log_session({"sessionId": "logs-5"})

    assert process.terminated is True
    assert log_sessions._sessions["logs-5"]["stop_requested"] is True
