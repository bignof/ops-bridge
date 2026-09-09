import json
from pathlib import Path

import pytest
import yaml

from core import compose_inspect, log_constants


class FakeWs:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    def send(self, payload: str) -> None:
        self.messages.append(json.loads(payload))


def _write_compose(d: Path, services: dict, name: str = "docker-compose.yaml") -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(yaml.dump({"services": services}), encoding="utf-8")
    return d


def test_inspect_compose_extracts_services_and_skips_long_ports(tmp_path: Path) -> None:
    proj = _write_compose(
        tmp_path / "admin",
        {
            "app": {"image": "oci.example.com/orchidea:1.7.20", "container_name": "wms-admin", "ports": ["13029:80", {"target": 80}]},
            "db": {"image": "mysql:8"},
            "junk": "not-a-dict",
        },
    )
    info = compose_inspect.inspect_compose(str(proj))
    assert info["composeFile"] == "docker-compose.yaml"
    assert info["dir"] == str(proj)
    assert info["services"] == [
        {"name": "app", "containerName": "wms-admin", "image": "oci.example.com/orchidea:1.7.20", "ports": ["13029:80"]},
        {"name": "db", "containerName": None, "image": "mysql:8", "ports": []},
    ]


def test_inspect_compose_errors(tmp_path: Path) -> None:
    with pytest.raises(compose_inspect.ComposeInspectError) as ei:
        compose_inspect.inspect_compose(str(tmp_path / "missing"))
    assert ei.value.code == "not_found"
    bare = tmp_path / "bare"
    bare.mkdir()
    with pytest.raises(compose_inspect.ComposeInspectError) as ei2:
        compose_inspect.inspect_compose(str(bare))
    assert ei2.value.code == "not_found"
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "docker-compose.yml").write_text("services: [unclosed", encoding="utf-8")
    with pytest.raises(compose_inspect.ComposeInspectError) as ei3:
        compose_inspect.inspect_compose(str(bad))
    assert ei3.value.code == "invalid"
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "docker-compose.yml").write_text("", encoding="utf-8")
    assert compose_inspect.inspect_compose(str(empty))["services"] == []
    listy = tmp_path / "listy"
    listy.mkdir()
    (listy / "docker-compose.yml").write_text("- just\n- a list\n", encoding="utf-8")
    assert compose_inspect.inspect_compose(str(listy))["services"] == []


def test_discover_projects_depth_skip_and_sort(tmp_path: Path) -> None:
    root = tmp_path / "data"
    _write_compose(root / "business-service" / "admin", {"app": {"image": "a", "container_name": "c-admin"}})
    _write_compose(root / "business-service" / "2admin", {"app": {"image": "a"}})
    _write_compose(root / "gateway", {"gw": {"image": "g"}}, name="docker-compose.yml")
    _write_compose(root / ".hidden" / "x", {"x": {"image": "x"}})
    _write_compose(root / "node_modules" / "y", {"y": {"image": "y"}})
    _write_compose(root / "a" / "b" / "c" / "too-deep", {"z": {"image": "z"}})  # 深度 4：裁掉
    _write_compose(root / "business-service" / "admin" / "nested", {"n": {"image": "n"}})  # 项目内不再下钻
    (root / "broken").mkdir()
    (root / "broken" / "docker-compose.yaml").write_text("services: [", encoding="utf-8")

    projects, truncated = compose_inspect.discover_projects(str(root))

    assert truncated is False
    assert [p["dir"] for p in projects] == [
        str(root / "broken"),
        str(root / "business-service" / "2admin"),
        str(root / "business-service" / "admin"),
        str(root / "gateway"),
    ]
    assert projects[2]["services"][0]["containerName"] == "c-admin"
    assert projects[0]["services"] == [] and projects[0]["error"]["code"] == "invalid"


def test_discover_projects_truncates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(log_constants, "DISCOVER_MAX_PROJECTS", 2)
    root = tmp_path / "data"
    for name in ("a", "b", "c"):
        _write_compose(root / name, {"s": {"image": "i"}})
    projects, truncated = compose_inspect.discover_projects(str(root))
    assert [p["dir"] for p in projects] == [str(root / "a"), str(root / "b")] and truncated is True


def test_discover_missing_root(tmp_path: Path) -> None:
    assert compose_inspect.discover_projects(str(tmp_path / "nope")) == ([], False)


def test_handlers_send_frames(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "data"
    proj = _write_compose(root / "admin", {"app": {"image": "a", "container_name": "c"}})
    monkeypatch.setattr(compose_inspect, "PROJECTS_ROOT", str(root))
    ws = FakeWs()

    compose_inspect.handle_discover(ws, {"requestId": "d1"})
    d = ws.messages[-1]
    assert d["type"] == "compose_discover_result" and d["requestId"] == "d1" and d["root"] == str(root)
    assert [p["dir"] for p in d["projects"]] == [str(proj)] and d["truncated"] is False

    compose_inspect.handle_inspect(ws, {"requestId": "i1", "dir": str(proj)})
    i = ws.messages[-1]
    assert i["type"] == "compose_inspect_result" and i["dir"] == str(proj) and i["services"][0]["containerName"] == "c"

    compose_inspect.handle_inspect(ws, {"requestId": "i2", "dir": str(tmp_path / "nope")})
    assert ws.messages[-1]["error"]["code"] == "not_found" and ws.messages[-1]["services"] == []

    compose_inspect.handle_inspect(ws, {"dir": str(proj)})  # 缺 requestId
    compose_inspect.handle_discover(ws, {})
    assert len(ws.messages) == 3


def test_handlers_io_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):
        raise OSError("disk")

    monkeypatch.setattr(compose_inspect, "discover_projects", boom)
    monkeypatch.setattr(compose_inspect, "inspect_compose", boom)
    ws = FakeWs()
    compose_inspect.handle_discover(ws, {"requestId": "d"})
    compose_inspect.handle_inspect(ws, {"requestId": "i", "dir": "/x"})
    assert [m["error"]["code"] for m in ws.messages] == ["io_error", "io_error"]
