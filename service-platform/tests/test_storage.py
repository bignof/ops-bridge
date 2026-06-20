"""插件包存储测试(Task 8):.tgz 校验/解析 package.json + 平台生成路径落盘/读流。

覆盖(评审 B1/H5/L3):
- `parse_tgz` 读 package.json —— **两种布局各一例**:根级(真实 `build --tar`)与
  `package/` 前缀(`npm pack`);两者都应解析出 name/version。
- `parse_tgz` 拒 garbage(非法 .tgz)与缺 name/version。
- `parse_tgz` 拒**非对象 package.json**(合法 JSON 标量/数组/null)→ BadPackage(评审 A5)。
- `parse_tgz` **精确成员名优先于诱饵**(诱饵 dist/node_modules/.../package.json + 真根级
  package.json,断言取后者;npm pack 布局同理优先于诱饵)(评审 A8)。
- `store_tgz` + `open_stream` 往返(平台生成路径,落盘后读回字节一致)。
- **穿越对抗(评审 H5/A17)**:`open_stream('../../x')` / 绝对路径 → raise(直击
  open_stream 对 DB 中被篡改 storage_path 的 realpath 穿越守卫);
  `_sanitize('../../x')` 结果不含 `..` 与路径分隔符。
- **解压炸弹守卫(评审 L3/A16)**:超大声明 member.size 被拒 + 头扫描阶段无界解压总量上限。
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


def test_parse_tgz_rejects_decompression_bomb_in_header_scan() -> None:
    """评审 A16(storage.py:41 头扫描阶段无界解压):member.size 守卫只挡单个 package.json,
    但 tarfile 解析 tar 头(getmember/遍历)需把 gzip 流解压到目标成员之前的所有内容。

    构造:在 package.json **之前**塞一个高度可压缩的巨大成员(解压后远超解压总量上限,
    压缩后很小)。修复后 parse_tgz 在累计解压字节超阈时 raise BadPackage,而非把数 GB
    解压进内存(单请求 DoS)。

    变异验证:移除解压总量上限,本用例不再 raise(会无界解压巨大成员)→ 红。
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        # 巨大但高度可压缩的诱饵成员,排在 package.json 之前
        bomb = b"\0" * (storage.MAX_DECOMPRESS_BYTES + 1024 * 1024)
        bi = tarfile.TarInfo("dist/bomb.bin")
        bi.size = len(bomb)
        t.addfile(bi, io.BytesIO(bomb))

        real = json.dumps({"name": "@business/plugin-x", "version": "1.2.3"}).encode()
        ri = tarfile.TarInfo("package.json")
        ri.size = len(real)
        t.addfile(ri, io.BytesIO(real))
    with pytest.raises(storage.BadPackage):
        storage.parse_tgz(buf.getvalue())


def _make_tgz_raw_pkg(raw: bytes, *, prefix: str = "") -> bytes:
    """构造一个 package.json 内容为任意原始字节(raw)的最小 .tgz(用于非对象 JSON 测试)。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        info = tarfile.TarInfo(prefix + "package.json")
        info.size = len(raw)
        t.addfile(info, io.BytesIO(raw))
    return buf.getvalue()


@pytest.mark.parametrize("body", [b"null", b"5", b"3.14", b'"x"', b"true", b"[1, 2]", b"[]"])
def test_parse_tgz_rejects_non_object_package_json(body: bytes) -> None:
    """评审 A5(storage.py:63 真实 bug):package.json 是**合法 JSON 但非对象**
    (null / 标量 / 数组)时,`pkg.get('name')` 会抛 AttributeError 冒泡成 500。
    修复后:这些都应被归一化成 BadPackage(端点层 → 400),绝不让 AttributeError 逃出。

    变异验证:删掉 `isinstance(pkg, dict)` 守卫,本用例(尤其 list 分支)会抛
    AttributeError(非 BadPackage)→ pytest.raises(BadPackage) 红。
    """
    with pytest.raises(storage.BadPackage):
        storage.parse_tgz(_make_tgz_raw_pkg(body))


# --- parse_tgz:精确成员名优先于诱饵(评审 A8,假绿补强) ---


def _make_tgz_with_decoy(real_prefix: str) -> bytes:
    """先放诱饵 `dist/node_modules/dep/package.json`(@x/dep@9.9.9),
    再放真正目标 `<real_prefix>package.json`(@business/plugin-x@1.2.3)。

    若 parse_tgz 退化成「遍历任意 *package.json 取第一个」,会命中诱饵(9.9.9)。
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        decoy = json.dumps({"name": "@x/dep", "version": "9.9.9"}).encode()
        di = tarfile.TarInfo("dist/node_modules/dep/package.json")
        di.size = len(decoy)
        t.addfile(di, io.BytesIO(decoy))

        real = json.dumps({"name": "@business/plugin-x", "version": "1.2.3"}).encode()
        ri = tarfile.TarInfo(real_prefix + "package.json")
        ri.size = len(real)
        t.addfile(ri, io.BytesIO(real))
    return buf.getvalue()


def test_parse_tgz_root_level_preferred_over_decoy() -> None:
    """评审 A8:诱饵 dist/node_modules/dep/package.json 在前,真正**根级** package.json 在后,
    parse_tgz 必须返回根级真包(1.2.3),而非诱饵(9.9.9)。

    变异验证:把 parse_tgz 改成遍历任意 *package.json(取第一个匹配),此用例会拿到诱饵 9.9.9 → 红。
    """
    meta = storage.parse_tgz(_make_tgz_with_decoy(real_prefix=""))
    assert meta == {"name": "@business/plugin-x", "version": "1.2.3"}


def test_parse_tgz_package_prefix_preferred_over_decoy() -> None:
    """评审 A8:诱饵在前,真正 **npm pack 布局** package/package.json 在后,取后者(1.2.3)。"""
    meta = storage.parse_tgz(_make_tgz_with_decoy(real_prefix="package/"))
    assert meta == {"name": "@business/plugin-x", "version": "1.2.3"}


# --- R4(复审,A16 改写副作用):package/ 必须优先于根级(无论物理顺序) ---
#    A16 把「按 _PKG_JSON_MEMBERS 偏好顺序择优」退化成「物理顺序命中即停」,丢了
#    「package/ 优先于根级」(B1 不变式 + 节点 sync-plugins.js 的 exists(package/)?package/:root)。
#    修复后:同含根级 + package/ 两布局时恒取 package/,与物理顺序无关。
#
#    变异验证:把 parse_tgz 改回「物理顺序命中即停」,根级在前的布局会取到根级(ROOT-VER)→ 红。


def _make_tgz_both_layouts(*, package_first: bool) -> bytes:
    """构造同时含**根级** package.json(version=ROOT-VER)与 **package/** package.json
    (version=PKG-VER)的 tgz;`package_first` 控制二者物理先后。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        root = json.dumps({"name": "@business/plugin-x", "version": "ROOT-VER"}).encode()
        pkg = json.dumps({"name": "@business/plugin-x", "version": "PKG-VER"}).encode()

        def _add(member_name: str, content: bytes) -> None:
            info = tarfile.TarInfo(member_name)
            info.size = len(content)
            t.addfile(info, io.BytesIO(content))

        if package_first:
            _add("package/package.json", pkg)
            _add("package.json", root)
        else:
            _add("package.json", root)
            _add("package/package.json", pkg)
    return buf.getvalue()


@pytest.mark.parametrize("package_first", [True, False])
def test_parse_tgz_package_prefix_preferred_over_root_regardless_of_order(package_first: bool) -> None:
    """R4:同含根级 + package/ 两布局时,parse_tgz **恒取 package/**(PKG-VER),与物理顺序无关。

    变异验证:实现改回「物理顺序命中即停」,`package_first=False`(根级在前)会取到 ROOT-VER → 红。
    """
    meta = storage.parse_tgz(_make_tgz_both_layouts(package_first=package_first))
    assert meta == {"name": "@business/plugin-x", "version": "PKG-VER"}, (
        f"package/ 未优先于根级(physical package_first={package_first})"
    )


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


def test_open_stream_traversal_guard_does_not_read_outside(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """评审 A17(直击 open_stream 防穿越,针对 DB 中被篡改的 storage_path):

    在 storage 根**之外**放一个真实可读文件(模拟 /etc/passwd),用 `..` 序列与绝对路径
    构造能 realpath 命中它的 storage_path;断言 open_stream 一律 raise(BadPackage),
    且**绝不读出越界文件内容**。

    变异验证:临时削弱 open_stream 的前缀守卫(放行越界路径),本用例会读到 SECRET 字节
    而非 raise → 红;还原后绿。
    """
    import types

    root = tmp_path / "storage_root"
    root.mkdir()
    monkeypatch.setattr(storage, "settings", types.SimpleNamespace(plugin_storage_dir=str(root)))

    # 在 storage 根之外放一个真实可读的"机密"文件
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"TOP-SECRET-OUTSIDE-ROOT")

    # 相对穿越:从 root 用 ../secret.txt 命中根外真实文件
    rel = os.path.join("..", "secret.txt")
    with pytest.raises(storage.BadPackage):
        list(storage.open_stream(rel))

    # 绝对路径穿越:直接给越界文件的绝对路径
    with pytest.raises(storage.BadPackage):
        list(storage.open_stream(str(secret)))


def test_sanitize_strips_traversal() -> None:
    out = storage._sanitize("../../x")
    assert ".." not in out and "/" not in out and "\\" not in out


# --- R6(复审,既有边角):store_tgz 写盘中途失败不留半成品孤儿文件 ---
#    旧实现 `open(abspath,'wb')` 立即创建/截断目标文件后 `f.write(data)`;write 中途 OSError
#    (磁盘满/IO)时 store_tgz 未 return → 外层 stored_path 仍 None → 补偿 `if stored_path:` 假
#    → 不调 safe_remove → 盘上留 0~部分字节孤儿。修复:写临时文件成功后 os.replace 到目标;
#    write 失败则删半成品 temp 再重抛——**目标路径绝不出现部分写入文件**。
#
#    变异验证:把落盘改回直接 open(目标)+write(无 temp+replace),本用例目标路径会残留半成品 → 红。


def test_store_tgz_mid_write_failure_leaves_no_target_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R6:write 中途抛 OSError → 目标路径盘上**无文件**(不留半成品孤儿)。"""
    import builtins
    import types

    root = tmp_path / "plugins"
    monkeypatch.setattr(storage, "settings", types.SimpleNamespace(plugin_storage_dir=str(root)))

    target_rel = os.path.join("7", "70", "x.tgz")
    target_abs = os.path.join(str(root), target_rel)

    real_open = builtins.open

    class _FailingWriter:
        """包装真实文件句柄:真实创建/截断文件(复现磁盘上落地),但 write 抛 OSError。

        如此可忠实复现旧实现「open(目标) 已在盘上建/截断文件 → write 中途失败 → 留孤儿」;
        修复后写的是 temp 文件,失败时 temp 被清理,目标永不出现。
        """

        def __init__(self, fh):
            self._fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._fh.close()
            return False

        def write(self, _data):
            raise OSError("disk full (simulated)")

    def fake_open(file, mode="r", *args, **kwargs):
        # 仅拦截 store_tgz 的二进制写(无论写到 temp 还是目标);其它(alembic 等)走真实 open。
        if "w" in mode and "b" in mode:
            return _FailingWriter(real_open(file, mode, *args, **kwargs))
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)

    with pytest.raises(OSError):
        storage.store_tgz(7, 70, "x.tgz", b"PAYLOAD")

    # 还原 open 后检查盘面:目标路径绝不应残留(部分写入的)文件。
    monkeypatch.setattr(builtins, "open", real_open)
    assert not os.path.exists(target_abs), "store_tgz 写盘中途失败残留目标孤儿文件"
    # 目录下也不应有任何遗留(temp 半成品须被清理)。
    parent = os.path.dirname(target_abs)
    leftovers = os.listdir(parent) if os.path.isdir(parent) else []
    assert leftovers == [], f"残留半成品文件: {leftovers}"


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
