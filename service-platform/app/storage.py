"""本地卷插件包存储:.tgz 校验 + 解析 package.json + 平台生成路径落盘/读流。

设计要点(评审 B1/H5/L3):
- **根级回退(B1)**:NocoBase `build --tar` 产物首条目即**根级 `package.json`**
  (无 `package/` 前缀);只有 `npm pack` 才把内容塞进 `package/` 子目录。节点脚本
  `sync-plugins.js` 用 `contentDir = exists(package/) ? package/ : root` 的回退,本模块
  **同源对齐**:`parse_tgz` 先试 `package/package.json`(npm pack),再回退根级
  `package.json`(build --tar)。只用 `package/` 布局会让真实数据 100% BadPackage。
  按精确成员名查找(非遍历任意 `*package.json`),避免误命中 `dist/node_modules/.../package.json`。
- **防穿越(H5)**:`store_tgz` 落盘路径**平台生成**(`<plugin_id>/<version_id>/<sanitized>`),
  `_sanitize` 用 basename + 字符白名单(结果不含 `..`/分隔符),**不用客户端 filename 拼路径**;
  `open_stream` 用 realpath 校验目标在 storage 根内,否则 raise。
- **解压炸弹守卫(L3)**:解 package.json 前校验 `member.size`,超 `MAX_PKG_JSON_SIZE` 即拒。
  上传请求体大小上限在 Task 9 端点处理 + README 注明依赖 nginx `client_max_body_size`。
"""

import io
import json
import os
import re
import tarfile

from app.config import settings


class BadPackage(Exception):
    """非法 .tgz / 缺 package.json / 缺 name|version / 路径越界。"""


# package.json 上限 1MB,防解压炸弹(评审 L3):压缩比可极高,解压前先看声明大小。
MAX_PKG_JSON_SIZE = 1 * 1024 * 1024

# 解析 .tgz 时累计解压字节硬上限(评审 A16):`tarfile` 解析 tar 头(遍历/getmember)需把
# gzip 流解压到目标成员之前的所有内容;member.size 守卫只挡单个 package.json,挡不住
# 「目标成员之前塞高压缩比巨大成员」的头扫描阶段无界解压(可膨胀数 GB → 单请求 DoS)。
# 上限取 64MB:足够覆盖真实包内 package.json 之前的合理内容,又远低于内存炸弹规模。
MAX_DECOMPRESS_BYTES = 64 * 1024 * 1024

# 候选 package.json 成员名(评审 B1:package/ 优先 npm pack,回退根级 build --tar)。
# 用**精确成员名**而非遍历任意 *package.json,避免误命中 dist/node_modules/<dep>/package.json。
_PKG_JSON_MEMBERS = ("package/package.json", "./package/package.json", "package.json", "./package.json")


def parse_tgz(data: bytes) -> dict:
    """解析 .tgz 内 package.json,返回 {name, version}。

    优先 `package/package.json`(npm pack),回退根级 `package.json`(build --tar);
    非法 tgz / 缺 package.json / 非对象 package.json / 缺 name|version 抛 BadPackage。
    """
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
            member = None
            # 评审 A16:逐成员遍历,累计解压字节超 MAX_DECOMPRESS_BYTES 即拒;**命中首个目标
            # package.json 即停**(不扫到流尾),把头扫描阶段的解压量也限死。
            decompressed = 0
            for info in t:
                decompressed += max(info.size or 0, 0)
                if decompressed > MAX_DECOMPRESS_BYTES:
                    raise BadPackage("解压总量超上限(疑似解压炸弹)")
                if info.name in _PKG_JSON_MEMBERS:
                    member = info
                    break  # 命中即停,避免继续解压到流尾
            if member is None:
                raise BadPackage("缺 package.json(package/ 与根级均无)")
            if member.size is not None and member.size > MAX_PKG_JSON_SIZE:  # L3:防炸弹
                raise BadPackage("package.json 过大")
            extracted = t.extractfile(member)
            if extracted is None:  # member 非常规文件(目录/链接)等异常情形
                raise BadPackage("package.json 不是常规文件")
            pkg = json.loads(extracted.read().decode())
    except BadPackage:
        raise
    except Exception as e:
        raise BadPackage(f"非法 .tgz: {e}") from None
    # 评审 A5:package.json 可能是合法 JSON 但**非对象**(null/标量/数组),此时 `.get` 会抛
    # AttributeError 冒泡成 500;归一化成 BadPackage(端点层 → 400,上传是攻击者入口)。
    if not isinstance(pkg, dict):
        raise BadPackage("package.json 不是 JSON 对象")
    name, version = pkg.get("name"), pkg.get("version")
    if not name or not version:
        raise BadPackage("package.json 缺 name/version")
    return {"name": name, "version": version}


def _sanitize(filename: str) -> str:
    """取 basename + 字符白名单,结果绝不含 `..` 或路径分隔符(评审 H5)。"""
    base = os.path.basename(filename or "plugin.tgz")
    base = re.sub(r"[^A-Za-z0-9._@+-]", "_", base)
    return base or "plugin.tgz"


def store_tgz(plugin_id: int, version_id: int, filename: str, data: bytes) -> str:
    """落盘到 `<storage>/<plugin_id>/<version_id>/<sanitized>`,返回**相对路径**(入库)。

    路径段完全由平台生成(plugin_id / version_id / 经 _sanitize 的 basename),
    客户端 filename 仅用于派生安全文件名,不参与目录拼接(评审 H5)。
    """
    rel = os.path.join(str(plugin_id), str(version_id), _sanitize(filename))
    abspath = os.path.join(settings.plugin_storage_dir, rel)
    os.makedirs(os.path.dirname(abspath), exist_ok=True)
    with open(abspath, "wb") as f:
        f.write(data)
    return rel  # 库里存相对路径


def safe_remove(storage_path: str) -> bool:
    """安全删除 storage 根内的已落盘文件(评审 A6/B2 补偿清理)。

    realpath 归一化后校验目标**落在 storage 根内**(与 `open_stream` 同一道防穿越守卫),
    再 `os.remove`;文件不存在静默吞(`FileNotFoundError`)。越界路径**绝不删**(返回 False),
    杜绝补偿逻辑被篡改 storage_path 诱导去删根外文件。

    返回:成功删除 True;文件不存在 / 路径越界 / 其它 OS 错误 False(补偿是 best-effort,
    绝不让清理自身异常掩盖原始失败)。
    """
    if not storage_path:
        return False
    root = os.path.realpath(settings.plugin_storage_dir)
    abspath = os.path.realpath(os.path.join(root, storage_path))
    if not (abspath == root or abspath.startswith(root + os.sep)):
        return False  # 越界:绝不删根外文件
    try:
        os.remove(abspath)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def open_stream(storage_path: str):
    """按相对 storage_path 返回字节迭代器;目标必须落在 storage 根内,否则抛 BadPackage。

    realpath 归一化后校验前缀,堵死 `../../etc/passwd` 与绝对路径穿越(评审 H5)。
    """
    root = os.path.realpath(settings.plugin_storage_dir)
    abspath = os.path.realpath(os.path.join(root, storage_path))
    if not (abspath == root or abspath.startswith(root + os.sep)):
        raise BadPackage("路径越界")
    if not os.path.isfile(abspath):
        raise FileNotFoundError(storage_path)

    def _gen():
        with open(abspath, "rb") as f:
            while chunk := f.read(65536):
                yield chunk

    return _gen()
