import json
import logging
import os
import threading
from pathlib import Path

import pytest
import yaml

from core import compose_inspect, log_constants
from core.capabilities import CAPABILITIES


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


# ---- 扫描时带回目录里 sync-plugins.config.json 配的拉取编码（read_sync_service） ----

SYNC_CONFIG = "sync-plugins.config.json"
SYNC_CONFIG_LIMIT = 65536


@pytest.fixture
def sync_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """扫描根。读配置文件只认 PROJECTS_ROOT 内的真实路径，所以把它指到这里。"""
    value = tmp_path / "data"
    value.mkdir()
    monkeypatch.setattr(compose_inspect, "PROJECTS_ROOT", str(value))
    return value


def _project(d: Path, config: object = None, raw: bytes | str | None = None) -> Path:
    """建一个 compose 项目。config 写成 JSON，raw 原样写入（用来造坏文件）；都不给就没有配置文件。"""
    _write_compose(d, {"app": {"image": "a"}})
    if raw is not None:
        (d / SYNC_CONFIG).write_bytes(raw if isinstance(raw, bytes) else raw.encode("utf-8"))
    elif config is not None:
        (d / SYNC_CONFIG).write_text(json.dumps(config), encoding="utf-8")
    return d


def _discover(root: Path) -> dict[str, dict]:
    projects, _ = compose_inspect.discover_projects(str(root))
    return {Path(p["dir"]).name: p for p in projects}


def _only_project(root: Path) -> dict:
    found = _discover(root)
    assert len(found) == 1
    return next(iter(found.values()))


def _symlink(link: Path, target: Path, directory: bool = False) -> None:
    try:
        os.symlink(str(target), str(link), target_is_directory=directory)
    except (OSError, NotImplementedError):
        pytest.skip("本机无符号链接权限，Linux 验收覆盖此分支")


def test_discover_reports_sync_service(sync_root: Path) -> None:
    _project(sync_root / "app", config={"service": "k3f9qm", "apiPath": "/queryPlugin"})
    p = _only_project(sync_root)
    assert p["syncService"] == "k3f9qm"
    assert "syncConfigError" not in p


@pytest.mark.parametrize(
    "config, expected",
    [
        pytest.param({"appName": "wms"}, "wms", id="app-name-only"),  # 旧字段
        pytest.param({"service": "", "appName": "wms"}, "wms", id="service-empty-string"),
        pytest.param({"service": None, "appName": "wms"}, "wms", id="service-null"),
        pytest.param({"service": "k3f9qm", "appName": "wms"}, "k3f9qm", id="service-wins-over-app-name"),
        pytest.param({"service": "a"}, "a", id="length-1"),
        pytest.param({"service": "a" * 255}, "a" * 255, id="length-255"),
    ],
)
def test_sync_service_value_rules(sync_root: Path, config: dict, expected: str) -> None:
    _project(sync_root / "app", config=config)
    p = _only_project(sync_root)
    assert p["syncService"] == expected and "syncConfigError" not in p


def test_missing_config(sync_root: Path) -> None:
    _project(sync_root / "app")
    p = _only_project(sync_root)
    assert (p["syncService"], p["syncConfigError"]) == (None, "missing")


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("{not json", id="not-json"),
        pytest.param("", id="empty-file"),
        pytest.param("[]", id="array"),
        pytest.param("null", id="null"),
        pytest.param('"k3f9qm"', id="bare-string"),
        pytest.param('{"service": 12}', id="service-number"),
        pytest.param('{"service": ["k3f9qm"]}', id="service-list"),
        pytest.param('{"service": ""}', id="service-empty-and-no-app-name"),
        pytest.param('{"service": "", "appName": 5}', id="app-name-not-string"),
        pytest.param('{"service": "", "appName": ""}', id="service-and-app-name-both-empty"),
        pytest.param('{"service": "%s"}' % ("a" * 256), id="service-256-chars"),
        pytest.param(b'\xef\xbb\xbf{"service": "k3f9qm"}', id="utf8-bom"),  # worker 的 JSON.parse 同样读不了带 BOM 的文件
        pytest.param(b"\xff\xfe not json", id="not-utf8"),
        pytest.param("x" * 70000, id="oversized-garbage"),
        pytest.param('{"service": "k3f9qm", "pad": "' + "x" * 70000 + '"}', id="oversized-but-valid-json"),
        pytest.param("[" * 50000, id="deeply-nested"),  # json 抛 RecursionError，不是 ValueError；read_json 统一转成 PluginFileError
    ],
)
def test_invalid_config(sync_root: Path, content: bytes | str) -> None:
    _project(sync_root / "bad", raw=content)
    _project(sync_root / "good", config={"service": "k3f9qm"})
    found = _discover(sync_root)
    assert (found["bad"]["syncService"], found["bad"]["syncConfigError"]) == (None, "invalid")
    # 一个目录的配置有问题，不影响整次扫描
    assert found["good"]["syncService"] == "k3f9qm" and "syncConfigError" not in found["good"]


def test_config_that_is_a_directory_is_invalid(sync_root: Path) -> None:
    proj = _project(sync_root / "app")
    (proj / SYNC_CONFIG).mkdir()
    assert compose_inspect.read_sync_service(str(proj)) == (None, "invalid")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="本机无 mkfifo，Linux 验收覆盖此分支")
def test_named_pipe_config_is_invalid_and_does_not_block(sync_root: Path) -> None:
    """目录里的命名管道不能把扫描卡死在 open 上：线程加超时，退化时是断言失败而不是整套测试挂住。"""
    proj = _project(sync_root / "app")
    os.mkfifo(proj / SYNC_CONFIG)
    result: list[tuple[str | None, str | None]] = []
    worker = threading.Thread(target=lambda: result.append(compose_inspect.read_sync_service(str(proj))), daemon=True)
    worker.start()
    worker.join(timeout=5)
    assert result == [(None, "invalid")]


def test_config_size_limit_is_65536_bytes(sync_root: Path) -> None:
    head = b'{"service": "k3f9qm"}'
    _project(sync_root / "at-limit", raw=head + b" " * (SYNC_CONFIG_LIMIT - len(head)))
    _project(sync_root / "over-by-one", raw=head + b" " * (SYNC_CONFIG_LIMIT + 1 - len(head)))
    found = _discover(sync_root)
    assert found["at-limit"]["syncService"] == "k3f9qm"
    assert found["over-by-one"]["syncConfigError"] == "invalid"


def test_oversized_config_is_not_read_into_memory(sync_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """超限的文件不能先读进内存再判断：大小在 read 之前判，读取量也始终有界（limit + 1）。"""
    head = b'{"service": "k3f9qm"}'
    at_limit = _project(sync_root / "at-limit", raw=head + b" " * (SYNC_CONFIG_LIMIT - len(head)))
    huge = _project(sync_root / "huge", raw=head + b" " * (5 * 1024 * 1024))
    reads: list[int] = []
    real_fdopen = os.fdopen

    class ReadSpy:
        def __init__(self, stream) -> None:
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *exc):
            return self.stream.__exit__(*exc)

        def fileno(self) -> int:
            return self.stream.fileno()

        def read(self, size: int | None = -1) -> bytes:
            reads.append(-1 if size is None else size)
            return self.stream.read(size)

    monkeypatch.setattr(os, "fdopen", lambda *a, **k: ReadSpy(real_fdopen(*a, **k)))

    assert compose_inspect.read_sync_service(str(at_limit)) == ("k3f9qm", None)
    assert reads and all(0 < n <= SYNC_CONFIG_LIMIT + 1 for n in reads)  # 先确认探针确实接上了，且不是无界 read()
    reads.clear()
    assert compose_inspect.read_sync_service(str(huge)) == (None, "invalid")
    assert reads == []  # 5 MiB 的文件一个字节都没读


def test_symlink_outside_root_is_invalid(
    sync_root: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # 与扫描根同前缀的兄弟目录（data-other）：按字符串前缀判断会误放行
    outside = tmp_path / "data-other"
    outside.mkdir()
    (outside / "shared.json").write_text(json.dumps({"service": "leaked"}), encoding="utf-8")
    proj = _project(sync_root / "app")
    _symlink(proj / SYNC_CONFIG, outside / "shared.json")
    caplog.set_level(logging.INFO)

    p = _only_project(sync_root)

    assert (p["syncService"], p["syncConfigError"]) == (None, "invalid")
    assert str(proj) in caplog.text and "outside_projects_root" in caplog.text  # 记目录和原因
    assert "leaked" not in caplog.text  # 目录外文件的内容没读


def test_symlinked_project_dir_outside_root_is_invalid(sync_root: Path, tmp_path: Path) -> None:
    outside = _project(tmp_path / "elsewhere" / "app", config={"service": "leaked"})
    link = sync_root / "link"
    _symlink(link, outside, directory=True)
    assert compose_inspect.read_sync_service(str(link)) == (None, "invalid")


def test_symlink_inside_root_is_followed(sync_root: Path) -> None:
    (sync_root / "shared").mkdir()
    (sync_root / "shared" / "common.json").write_text(json.dumps({"service": "k3f9qm"}), encoding="utf-8")
    proj = _project(sync_root / "app")
    _symlink(proj / SYNC_CONFIG, sync_root / "shared" / "common.json")
    assert _only_project(sync_root)["syncService"] == "k3f9qm"


def test_path_on_another_drive_counts_as_outside_root(sync_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 上两个路径不在同一盘符时 os.path.commonpath 抛 ValueError：要当作越界，不能让整次扫描失败。"""

    def other_drive(paths):
        raise ValueError("Paths don't have the same drive")

    _project(sync_root / "app", config={"service": "k3f9qm"})
    monkeypatch.setattr(os.path, "commonpath", other_drive)
    p = _only_project(sync_root)
    assert (p["syncService"], p["syncConfigError"]) == (None, "invalid")


def test_projects_root_that_is_itself_a_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_root = tmp_path / "real"
    real_root.mkdir()
    link_root = tmp_path / "data"
    _symlink(link_root, real_root, directory=True)
    _project(real_root / "app", config={"service": "k3f9qm"})
    monkeypatch.setattr(compose_inspect, "PROJECTS_ROOT", str(link_root))
    assert _only_project(link_root)["syncService"] == "k3f9qm"


@pytest.mark.parametrize(
    "raw, reason",
    [
        pytest.param('{"service": "TOP-SECRET-VALUE", ', "JSONDecodeError", id="truncated-json"),
        pytest.param('["TOP-SECRET-VALUE"]', "PluginFileError", id="not-an-object"),
        pytest.param('{"service": ["TOP-SECRET-VALUE"]}', "bad_service_value", id="service-not-string"),
    ],
)
def test_sync_config_problem_is_logged_without_file_content(
    sync_root: Path, caplog: pytest.LogCaptureFixture, raw: str, reason: str
) -> None:
    proj = _project(sync_root / "app", raw=raw)
    caplog.set_level(logging.INFO)
    assert compose_inspect.read_sync_service(str(proj)) == (None, "invalid")
    assert str(proj) in caplog.text and reason in caplog.text  # 记目录和错误类别
    assert "TOP-SECRET-VALUE" not in caplog.text  # 不记文件内容


def test_discover_frame_carries_sync_service_even_when_compose_is_broken(sync_root: Path) -> None:
    _project(sync_root / "ok", config={"service": "k3f9qm"})
    _project(sync_root / "none")
    _project(sync_root / "bad-config", raw="{")
    _project(sync_root / "broken-compose", config={"appName": "wms"})
    (sync_root / "broken-compose" / "docker-compose.yaml").write_text("services: [", encoding="utf-8")
    ws = FakeWs()

    compose_inspect.handle_discover(ws, {"requestId": "d1"})

    found = {Path(p["dir"]).name: p for p in ws.messages[-1]["projects"]}
    assert found["ok"]["syncService"] == "k3f9qm" and "syncConfigError" not in found["ok"]
    assert (found["none"]["syncService"], found["none"]["syncConfigError"]) == (None, "missing")
    assert (found["bad-config"]["syncService"], found["bad-config"]["syncConfigError"]) == (None, "invalid")
    # compose 本身解析失败的目录也照样带回拉取编码
    assert found["broken-compose"]["error"]["code"] == "invalid" and found["broken-compose"]["syncService"] == "wms"


def test_inspect_does_not_report_sync_config(sync_root: Path) -> None:
    proj = _project(sync_root / "app", config={"service": "k3f9qm"})
    assert not {"syncService", "syncConfigError"} & set(compose_inspect.inspect_compose(str(proj)))
    ws = FakeWs()
    compose_inspect.handle_inspect(ws, {"requestId": "i1", "dir": str(proj)})
    assert not {"syncService", "syncConfigError"} & set(ws.messages[-1])


def test_capability_listed() -> None:
    assert "discover_sync_service" in CAPABILITIES


# ---- 评审留下的小问题：路径解析出错、读文件权限、悬空链接、用 discover_projects 自己的 root ----


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(OSError(5, "I/O error"), id="OSError"),
        pytest.param(ValueError("embedded null byte"), id="ValueError"),
        # posixpath.realpath 每跳一个符号链接递归一层：一条约 1000 环的链就让它抛 RecursionError（Linux 上实测 1100 环）
        pytest.param(RecursionError("maximum recursion depth exceeded"), id="RecursionError"),
    ],
)
def test_config_path_resolution_failure_makes_only_that_project_invalid(
    sync_root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    _project(sync_root / "flaky", config={"service": "k3f9qm"})
    _project(sync_root / "good", config={"service": "k3f9qm"})
    real_realpath = os.path.realpath

    def flaky_realpath(path, *args, **kwargs):
        if Path(path).parent.name == "flaky":  # 只有这个目录里的配置文件解析不了路径
            raise error
        return real_realpath(path, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", flaky_realpath)
    caplog.set_level(logging.INFO)

    found = _discover(sync_root)  # 修复前：异常穿出 discover_projects，handle_discover 会把整次扫描回成 io_error

    assert (found["flaky"]["syncService"], found["flaky"]["syncConfigError"]) == (None, "invalid")
    assert found["good"]["syncService"] == "k3f9qm" and "syncConfigError" not in found["good"]
    assert str(sync_root / "flaky") in caplog.text and type(error).__name__ in caplog.text  # 记目录和错误类别
    ws = FakeWs()
    compose_inspect.handle_discover(ws, {"requestId": "d1"})
    assert "error" not in ws.messages[-1] and len(ws.messages[-1]["projects"]) == 2


def test_projects_root_resolution_failure_does_not_fail_the_scan(
    sync_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project(sync_root / "a", config={"service": "k3f9qm"})
    _project(sync_root / "b", config={"service": "k3f9qm"})
    real_realpath = os.path.realpath

    def flaky_realpath(path, *args, **kwargs):
        if os.path.normcase(str(path)) == os.path.normcase(str(sync_root)):  # 解析扫描根本身出错
            raise OSError(5, "I/O error")
        return real_realpath(path, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", flaky_realpath)
    ws = FakeWs()

    compose_inspect.handle_discover(ws, {"requestId": "d1"})

    frame = ws.messages[-1]
    assert "error" not in frame  # 不是 io_error：目录照常列出，只是读不了拉取编码
    assert [(p["syncService"], p["syncConfigError"]) for p in frame["projects"]] == [(None, "invalid")] * 2


def test_unreadable_config_is_invalid_not_missing(
    sync_root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """打开配置文件时 PermissionError（OSError，但不是 FileNotFoundError）归 invalid：文件在，只是读不了。"""
    locked = _project(sync_root / "locked", config={"service": "k3f9qm"})
    _project(sync_root / "good", config={"service": "k3f9qm"})
    real_open = os.open

    def deny(path, *args, **kwargs):
        if Path(path).name == SYNC_CONFIG and Path(path).parent.name == "locked":
            raise PermissionError(13, "Permission denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", deny)
    caplog.set_level(logging.INFO)

    found = _discover(sync_root)

    assert (found["locked"]["syncService"], found["locked"]["syncConfigError"]) == (None, "invalid")
    assert found["good"]["syncService"] == "k3f9qm"
    assert str(locked) in caplog.text and "PermissionError" in caplog.text


def test_dangling_symlink_to_a_target_inside_root_is_missing(sync_root: Path) -> None:
    proj = _project(sync_root / "app")
    _symlink(proj / SYNC_CONFIG, sync_root / "shared" / "gone.json")  # 目标在根内，但不存在
    assert compose_inspect.read_sync_service(str(proj)) == (None, "missing")


def test_dangling_symlink_to_a_target_outside_root_is_invalid(
    sync_root: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    proj = _project(sync_root / "app")
    _symlink(proj / SYNC_CONFIG, tmp_path / "data-other" / "gone.json")  # 目标在根外，也不存在：根本不去看
    caplog.set_level(logging.INFO)
    assert compose_inspect.read_sync_service(str(proj)) == (None, "invalid")
    assert str(proj) in caplog.text and "outside_projects_root" in caplog.text


def test_discover_checks_containment_against_its_own_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """判越界用 discover_projects 传进来的 root，不是全局 PROJECTS_ROOT；单参数的 read_sync_service 仍取全局值。"""
    root = tmp_path / "data"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setattr(compose_inspect, "PROJECTS_ROOT", str(elsewhere))
    proj = _project(root / "app", config={"service": "k3f9qm"})

    p = _only_project(root)  # 修复前：项目在全局根之外，得 invalid

    assert p["syncService"] == "k3f9qm" and "syncConfigError" not in p
    assert compose_inspect.read_sync_service(str(proj)) == (None, "invalid")  # 原有的单参数调用形态不变
    assert compose_inspect.read_sync_service(str(proj), str(root)) == ("k3f9qm", None)
    assert compose_inspect.read_sync_service(str(proj), root=str(elsewhere)) == (None, "invalid")
