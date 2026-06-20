"""插件上传端到端测试(Task 9)。

经 conftest 的 `client` fixture(临时文件库 + swap 单例)。**额外**把 frozen
`settings.plugin_storage_dir` 指到 `tmp_path`(用 `object.__setattr__`,照 conftest
改 frozen settings 的范式),避免落盘污染真实 `./data/plugins`。

绑定约束(评审 M8/M2,见 task-9-brief):
- **version = .tgz 内 package.json.version**(**非文件名 split**)——本测试用与文件名
  **不一致**的 version 构造 .tgz(文件名带 `rc.999` 垃圾值,package.json 里是 `1.2.3`),
  断言入库 `version == "1.2.3"`,锁死「取自 package.json 而非文件名」这一约束。
- list 用 `{count, rows, page, pageSize, totalPage}` 信封。

覆盖:
- 上传成功 → 建 plugin_version(version 取自 package.json)+ plugin_attachment;响应 camelCase。
- 同 (plugin_id, version) 再传 → 409。
- 未知包名(无匹配 plugin)→ 400;多命中 → 400。
- list 信封形状 + `?pluginId=` 过滤;get 单条 / 404。
- 无 Bearer → default-deny 中间件 401。
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _make_tgz(name: str, version: str, *, prefix: str = "") -> bytes:
    """构造仅含一个 package.json 的最小 .tgz(同 test_storage 的 fixture 法,Task 8)。

    prefix="" → 根级布局(真实 NocoBase `build --tar`);
    prefix="package/" → `npm pack` 子目录布局。
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        content = json.dumps({"name": name, "version": version}).encode()
        info = tarfile.TarInfo(prefix + "package.json")
        info.size = len(content)
        t.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _h(client: TestClient) -> dict[str, str]:
    """登录拿 JWT,组装 Authorization 头。"""
    token = client.post("/auth/login", json={"username": "admin", "password": "admin-pw"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def storage_tmp(tmp_path: Path):
    """把 frozen settings.plugin_storage_dir 指到 tmp_path,退出还原(照 conftest 范式)。"""
    import app.main as main_module

    old = main_module.settings.plugin_storage_dir
    object.__setattr__(main_module.settings, "plugin_storage_dir", str(tmp_path / "plugins"))
    yield
    object.__setattr__(main_module.settings, "plugin_storage_dir", old)


def _create_plugin(client: TestClient, h: dict[str, str], code: str) -> int:
    r = client.post("/api/plugins", json={"code": code}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_upload_version_comes_from_package_json_not_filename(client: TestClient, storage_tmp) -> None:
    """**核心约束(评审 M8)**:入库 version 取自 package.json,而非上传文件名。

    文件名故意带 `rc.999`(旧平台文件名 split 会得的垃圾值);package.json 里是 `1.2.3`。
    """
    h = _h(client)
    pid = _create_plugin(client, h, "@business/plugin-x")

    data = _make_tgz("@business/plugin-x", "1.2.3")
    # 文件名与 package.json.version 故意不一致:若实现误用文件名 split 会得 "rc.999"。
    r = client.post(
        "/api/plugin-versions/upload",
        files={"file": ("plugin-x-9.9.9-rc.999.tgz", data, "application/gzip")},
        headers=h,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # 响应 camelCase + version 来自 package.json
    assert body["version"] == "1.2.3"
    assert "pluginVersionId" in body and "attachmentId" in body

    # 经 get 端点二次确认入库 version == package.json 的 1.2.3(非文件名 9.9.9-rc.999)
    pv = client.get(f"/api/plugin-versions/{body['pluginVersionId']}", headers=h).json()
    assert pv["version"] == "1.2.3"
    assert pv["pluginId"] == pid


def test_upload_duplicate_version_conflict(client: TestClient, storage_tmp) -> None:
    """同 (plugin_id, version) 再传 → 409(UNIQUE(plugin_id, version))。"""
    h = _h(client)
    _create_plugin(client, h, "@business/plugin-x")
    data = _make_tgz("@business/plugin-x", "1.2.3")

    first = client.post(
        "/api/plugin-versions/upload",
        files={"file": ("a.tgz", data, "application/gzip")},
        headers=h,
    )
    assert first.status_code == 200, first.text
    # 同版本再传 → 409
    second = client.post(
        "/api/plugin-versions/upload",
        files={"file": ("a.tgz", data, "application/gzip")},
        headers=h,
    )
    assert second.status_code == 409, second.text


def test_upload_unknown_plugin_400(client: TestClient, storage_tmp) -> None:
    """包名匹配不到任何 plugin.code → 400(明确文案)。"""
    h = _h(client)
    # 不预建 plugin;直接传未知包名
    data = _make_tgz("@business/plugin-unknown", "1.0.0")
    r = client.post(
        "/api/plugin-versions/upload",
        files={"file": ("u.tgz", data, "application/gzip")},
        headers=h,
    )
    assert r.status_code == 400, r.text


def test_upload_ambiguous_plugin_400(client: TestClient, storage_tmp) -> None:
    """同尾段多命中(LIKE %/<尾段> 命中 >1)→ 400。"""
    h = _h(client)
    # 两个不同 scope、相同尾段 plugin-x:LIKE %/plugin-x 会命中两条
    _create_plugin(client, h, "@business/plugin-x")
    _create_plugin(client, h, "@orchisky/plugin-x")
    # 上传一个尾段为 plugin-x 但 code 不精确等于任一条的包名 → 触发 LIKE 多命中
    data = _make_tgz("@other/plugin-x", "2.0.0")
    r = client.post(
        "/api/plugin-versions/upload",
        files={"file": ("amb.tgz", data, "application/gzip")},
        headers=h,
    )
    assert r.status_code == 400, r.text


def test_upload_bad_package_400(client: TestClient, storage_tmp) -> None:
    """非法 .tgz(parse 失败)→ 400。"""
    h = _h(client)
    r = client.post(
        "/api/plugin-versions/upload",
        files={"file": ("bad.tgz", b"not a tgz", "application/gzip")},
        headers=h,
    )
    assert r.status_code == 400, r.text


def test_upload_oversize_413(client: TestClient, storage_tmp) -> None:
    """超大请求体(Content-Length 超上限)→ 413(评审 L3,读入前先挡)。"""
    from app.routers.plugin_versions import MAX_UPLOAD_BYTES

    h = _h(client)
    _create_plugin(client, h, "@business/plugin-x")
    # 构造一个超上限的 body(内容无所谓,大小触发上限即可)
    big = b"x" * (MAX_UPLOAD_BYTES + 1)
    r = client.post(
        "/api/plugin-versions/upload",
        files={"file": ("big.tgz", big, "application/gzip")},
        headers=h,
    )
    assert r.status_code == 413, r.text


def test_plugin_versions_list_envelope_and_filter(client: TestClient, storage_tmp) -> None:
    """list 返回信封形状 + `?pluginId=` 过滤;get 单条。"""
    h = _h(client)
    pid_x = _create_plugin(client, h, "@business/plugin-x")
    pid_y = _create_plugin(client, h, "@business/plugin-y")

    # 给 x 上传两个版本,y 上传一个
    for ver in ("1.0.0", "1.1.0"):
        r = client.post(
            "/api/plugin-versions/upload",
            files={"file": ("x.tgz", _make_tgz("@business/plugin-x", ver), "application/gzip")},
            headers=h,
        )
        assert r.status_code == 200, r.text
    ry = client.post(
        "/api/plugin-versions/upload",
        files={"file": ("y.tgz", _make_tgz("@business/plugin-y", "2.0.0"), "application/gzip")},
        headers=h,
    )
    assert ry.status_code == 200, ry.text

    # 信封形状
    body = client.get("/api/plugin-versions", headers=h).json()
    assert body["count"] >= 3
    assert "rows" in body and "totalPage" in body
    assert body["page"] == 1 and body["pageSize"] == 20

    # ?pluginId= 过滤:只回 x 的两条
    filtered = client.get(f"/api/plugin-versions?pluginId={pid_x}", headers=h).json()
    assert filtered["count"] == 2
    assert all(row["pluginId"] == pid_x for row in filtered["rows"])

    filtered_y = client.get(f"/api/plugin-versions?pluginId={pid_y}", headers=h).json()
    assert filtered_y["count"] == 1
    assert filtered_y["rows"][0]["version"] == "2.0.0"


def test_plugin_versions_list_joins_plugin_code_and_filename(client: TestClient, storage_tmp) -> None:
    """评审 A3(契约漂移第 3 例):list 端点 LEFT JOIN 回 pluginCode/filename(此前 list_rows
    无 JOIN → 上传页两列恒空)。上传一个包后,断言两字段**非空**且取值正确。

    红(未补 JOIN 时):PluginVersionOut 无这两字段 / 路由用 list_rows → pluginCode/filename 缺失或 None。
    绿(补 JOIN 后):pluginCode == plugin.code,filename == 上传文件名(经 _sanitize)。
    """
    h = _h(client)
    pid = _create_plugin(client, h, "@business/plugin-x")
    up = client.post(
        "/api/plugin-versions/upload",
        files={"file": ("plugin-x-1.2.3.tgz", _make_tgz("@business/plugin-x", "1.2.3"), "application/gzip")},
        headers=h,
    )
    assert up.status_code == 200, up.text

    body = client.get(f"/api/plugin-versions?pluginId={pid}", headers=h).json()
    assert body["count"] == 1
    row = body["rows"][0]
    # pluginCode = plugin.code(JOIN plugin);filename = 上传文件名(JOIN plugin_attachment)
    assert row["pluginCode"] == "@business/plugin-x", row
    assert row["filename"] == "plugin-x-1.2.3.tgz", row


def _list_stored_files(tmp_path: Path) -> list[Path]:
    """枚举 storage 根(tmp_path/plugins)下所有已落盘文件,用于断言无孤儿 .tgz。"""
    root = tmp_path / "plugins"
    if not root.exists():
        return []
    return [p for p in root.rglob("*") if p.is_file()]


def test_upload_attachment_failure_is_atomic_no_orphan(
    client: TestClient, storage_tmp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """评审 B2 + A6(原子性 + 孤儿文件):落盘成功后**附件入库失败**时,
    必须整体回滚——**既不留 plugin_version 行,也不留盘上 .tgz**。

    注入:把 store.PluginAttachment 换成构造即抛的类,模拟「version+落盘 OK,attachment 步失败」。

    红(未原子化时):version 行残留(撞 UNIQUE 卡重传)+ 盘上 .tgz 残留(不可回收孤儿)。
    绿(原子化后):version 不存在 + storage 根下无任何文件。
    """
    from app import store

    h = _h(client)
    _create_plugin(client, h, "@business/plugin-x")

    class _BoomAttachment:
        def __init__(self, *a, **k):
            raise RuntimeError("注入:附件入库失败")

    monkeypatch.setattr(store, "PluginAttachment", _BoomAttachment)

    data = _make_tgz("@business/plugin-x", "1.2.3")
    r = client.post(
        "/api/plugin-versions/upload",
        files={"file": ("x.tgz", data, "application/gzip")},
        headers=h,
    )
    # 落地失败对客户端是 500(非法包才是 400);关键在副作用必须被完全回滚。
    assert r.status_code == 500, r.text

    # 还原注入,以便用真实 ORM 查询验证副作用。
    monkeypatch.undo()

    # ① 盘上无任何残留文件(A6:孤儿 .tgz 必须被清)。
    assert _list_stored_files(tmp_path) == [], "落盘文件未被清理,残留孤儿 .tgz"

    # ② version 行不存在(B2:attachment 失败后 version 随事务回滚,不卡重传)。
    body = client.get("/api/plugin-versions", headers=h).json()
    assert body["count"] == 0, "plugin_version 残留(撞 UNIQUE 会卡重传)"


def test_upload_can_retry_after_atomic_failure(
    client: TestClient, storage_tmp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """评审 B2(回滚后可重传):第一次落地中途失败完全回滚后,
    用**同 (plugin, version)** 重传应成功(不被半成品 version 行的 UNIQUE 卡住)。"""
    from app import store

    h = _h(client)
    pid = _create_plugin(client, h, "@business/plugin-x")

    class _BoomAttachment:
        def __init__(self, *a, **k):
            raise RuntimeError("注入:附件入库失败")

    monkeypatch.setattr(store, "PluginAttachment", _BoomAttachment)
    data = _make_tgz("@business/plugin-x", "1.2.3")
    first = client.post(
        "/api/plugin-versions/upload",
        files={"file": ("x.tgz", data, "application/gzip")},
        headers=h,
    )
    assert first.status_code == 500, first.text

    # 解除注入后重传同版本:应成功(半成品已回滚,无 UNIQUE 卡阻),且盘上恰一个文件。
    monkeypatch.undo()
    retry = client.post(
        "/api/plugin-versions/upload",
        files={"file": ("x.tgz", data, "application/gzip")},
        headers=h,
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["version"] == "1.2.3"
    pv = client.get(f"/api/plugin-versions/{retry.json()['pluginVersionId']}", headers=h).json()
    assert pv["pluginId"] == pid
    assert len(_list_stored_files(tmp_path)) == 1


def test_plugin_versions_get_missing_404(client: TestClient, storage_tmp) -> None:
    h = _h(client)
    assert client.get("/api/plugin-versions/999999", headers=h).status_code == 404


def test_upload_requires_auth(client: TestClient, storage_tmp) -> None:
    """无 Bearer → default-deny 中间件 401。"""
    data = _make_tgz("@business/plugin-x", "1.2.3")
    r = client.post(
        "/api/plugin-versions/upload",
        files={"file": ("x.tgz", data, "application/gzip")},
    )
    assert r.status_code == 401
    assert client.get("/api/plugin-versions").status_code == 401
