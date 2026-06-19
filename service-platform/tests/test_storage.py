"""插件包存储测试(Task 8):.tgz 校验/解析 package.json + 平台生成路径落盘/读流。

覆盖(评审 B1/H5/L3):
- `parse_tgz` 读 package.json —— **两种布局各一例**:根级(真实 `build --tar`)与
  `package/` 前缀(`npm pack`);两者都应解析出 name/version。
- `parse_tgz` 拒 garbage(非法 .tgz)与缺 name/version。
- `store_tgz` + `open_stream` 往返(平台生成路径,落盘后读回字节一致)。
- **穿越对抗(评审 H5)**:`open_stream('../../x')` / 绝对路径 → raise;
  `_sanitize('../../x')` 结果不含 `..` 与路径分隔符。
- **解压炸弹守卫(评审 L3)**:超大 package.json member 被拒。
- **根级回退 sanity(评审 B1)**:用仓库内真实交付包(首条目=根级 package.json)
  跑一次 parse,证明根级回退在真实数据上生效(包不存在则 skip)。

frozen `storage.settings` 一律整体替换模块引用(`types.SimpleNamespace`),
禁用 `monkeypatch.setattr(..., raising=False)`(评审 H8)。
"""

import io
import json
import os
import tarfile
from pathlib import Path

import pytest

from app import storage


def _make_tgz(name: str, version: str, *, prefix: str = "") -> bytes:
    """构造仅含一个 package.json 的最小 .tgz。

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


# --- parse_tgz:两种布局各一例(评审 B1) ---


def test_parse_tgz_root_level_layout() -> None:
    """真实 `build --tar`(根级 package.json)必须能解析。"""
    meta = storage.parse_tgz(_make_tgz("@business/plugin-x", "1.2.3"))
    assert meta == {"name": "@business/plugin-x", "version": "1.2.3"}


def test_parse_tgz_package_prefix_layout() -> None:
    """`npm pack`(package/ 前缀)也兼容。"""
    meta = storage.parse_tgz(_make_tgz("@business/plugin-x", "1.2.3", prefix="package/"))
    assert meta == {"name": "@business/plugin-x", "version": "1.2.3"}


# --- parse_tgz:拒非法 / 缺字段 ---


def test_parse_tgz_rejects_garbage() -> None:
    with pytest.raises(storage.BadPackage):
        storage.parse_tgz(b"not a tgz")


def test_parse_tgz_rejects_missing_package_json() -> None:
    """.tgz 合法但不含 package.json(package/ 与根级均无)→ BadPackage。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        content = b"hello"
        info = tarfile.TarInfo("README.md")
        info.size = len(content)
        t.addfile(info, io.BytesIO(content))
    with pytest.raises(storage.BadPackage):
        storage.parse_tgz(buf.getvalue())


def test_parse_tgz_rejects_missing_version() -> None:
    """package.json 缺 version → BadPackage(version 不变式)。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        content = json.dumps({"name": "@business/plugin-x"}).encode()
        info = tarfile.TarInfo("package.json")
        info.size = len(content)
        t.addfile(info, io.BytesIO(content))
    with pytest.raises(storage.BadPackage):
        storage.parse_tgz(buf.getvalue())


def test_parse_tgz_rejects_oversized_package_json() -> None:
    """解压炸弹守卫(评审 L3):超大 package.json member 被拒。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        # 高度可压缩的大内容:压缩后很小,但 member.size 远超上限
        content = b" " * (storage.MAX_PKG_JSON_SIZE + 1)
        info = tarfile.TarInfo("package.json")
        info.size = len(content)
        t.addfile(info, io.BytesIO(content))
    with pytest.raises(storage.BadPackage):
        storage.parse_tgz(buf.getvalue())


# --- store_tgz + open_stream 往返 ---


def test_store_and_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    monkeypatch.setattr(storage, "settings", types.SimpleNamespace(plugin_storage_dir=str(tmp_path)))
    rel = storage.store_tgz(1, 10, "x.tgz", b"bytes")
    # 平台生成路径:<plugin_id>/<version_id>/<sanitized>
    assert rel == os.path.join("1", "10", "x.tgz")
    assert b"bytes" == b"".join(storage.open_stream(rel))


def test_store_tgz_uses_basename_not_client_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """落盘路径平台生成 + basename 白名单,不用客户端 filename 拼路径(评审 H5)。"""
    import types

    monkeypatch.setattr(storage, "settings", types.SimpleNamespace(plugin_storage_dir=str(tmp_path)))
    rel = storage.store_tgz(2, 20, "../../evil/x.tgz", b"data")
    # 结果相对路径里目录段只能是 plugin_id/version_id;文件名经 _sanitize 不含 ..
    assert rel.startswith(os.path.join("2", "20") + os.sep)
    assert ".." not in os.path.basename(rel)
    assert b"data" == b"".join(storage.open_stream(rel))


# --- 穿越对抗(评审 H5) ---


def test_open_stream_rejects_traversal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    monkeypatch.setattr(storage, "settings", types.SimpleNamespace(plugin_storage_dir=str(tmp_path)))
    with pytest.raises(storage.BadPackage):
        list(storage.open_stream("../../../etc/passwd"))
    with pytest.raises(storage.BadPackage):
        list(storage.open_stream("/etc/passwd"))


def test_sanitize_strips_traversal() -> None:
    out = storage._sanitize("../../x")
    assert ".." not in out and "/" not in out and "\\" not in out


# --- 根级回退 sanity:真实交付包(评审 B1) ---

_REAL_TGZ = Path(
    r"C:\Users\bigno\Documents\work\orchisky\src\cnp\storage\tar\@business\plugin-mom-print-1.7.20.20260612134426.tgz"
)


@pytest.mark.skipif(not _REAL_TGZ.is_file(), reason="真实交付包不在本机,跳过根级回退 sanity")
def test_parse_tgz_real_build_tar_root_fallback() -> None:
    """真实 NocoBase `build --tar` 包(首条目=根级 package.json)走根级回退能解析。"""
    meta = storage.parse_tgz(_REAL_TGZ.read_bytes())
    assert meta["name"] == "@business/plugin-mom-print"
    assert meta["version"]  # 版本号非空(具体值随构建时间戳变化)
