import json
import subprocess
from services import docker_cli

class FakeProc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err

def test_run_docker_ok(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: FakeProc(0, "done", ""))
    assert docker_cli.run_docker(["ps"]) == (True, "done")

def test_list_running_containers_empty(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: FakeProc(0, "\n", ""))
    assert docker_cli.list_running_containers() == []

def test_list_running_containers_parses_inspect(monkeypatch):
    calls = []
    def fake_run(cmd, **k):
        calls.append(cmd)
        if cmd[:2] == ["docker", "ps"]:
            return FakeProc(0, "abc\n", "")
        return FakeProc(0, json.dumps([{"Id": "abc"}]), "")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert docker_cli.list_running_containers() == [{"Id": "abc"}]
    assert calls[1][:2] == ["docker", "inspect"]

def test_list_running_containers_ps_fail(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: FakeProc(1, "", "boom"))
    import pytest
    with pytest.raises(RuntimeError):
        docker_cli.list_running_containers()

def test_restart_container(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: FakeProc(0, "abc", ""))
    assert docker_cli.restart_container("abc") == (True, "abc")
