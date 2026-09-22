"""本地插件文件清单与可恢复删除。路径只由已验证的挂载根和 npm 包名构造。"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time
import uuid

REPORT_FILE = '.hub-plugin-sync.json'
LOCK_FILE = '.hub-plugin-operation.lock'
TRASH_DIR = '.hub-plugin-trash'
MAX_JSON_BYTES = 256 * 1024
MAX_PLUGINS = 500
RETENTION_SECONDS = 7 * 24 * 60 * 60
PACKAGE_RE = re.compile(r'^(?:@[a-zA-Z0-9][a-zA-Z0-9._-]*/)?[a-zA-Z0-9][a-zA-Z0-9._-]*$')
TRASH_RE = re.compile(r'^[a-f0-9]{32}$')


class PluginFileError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def utc_stamp(value=None):
    return datetime.fromtimestamp(time.time() if value is None else value, timezone.utc).isoformat()


def _inside(child, root):
    try:
        return os.path.commonpath([str(child), str(root)]) == str(root)
    except ValueError:
        return False


def checked_root(root):
    absolute = Path(os.path.abspath(root))
    if not absolute.is_dir() or absolute.is_symlink() or absolute != absolute.resolve():
        raise PluginFileError('unsafe_path', '插件挂载目录不存在或包含符号链接')
    return absolute


def checked_path(root, relative, *, allow_missing=False):
    root = checked_root(root)
    target = root / relative
    if not _inside(os.path.abspath(target), root) or target == root:
        raise PluginFileError('unsafe_path', '插件路径越界')
    current = root
    for part in Path(relative).parts:
        if part in ('..', '.'):
            raise PluginFileError('unsafe_path', '插件路径非法')
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if allow_missing:
                continue
            raise PluginFileError('not_found', '本地插件文件不存在')
        if stat.S_ISLNK(info.st_mode):
            raise PluginFileError('unsafe_path', '插件路径包含符号链接，拒绝操作')
    return target


def package_path(root, name, *, allow_missing=False):
    if not isinstance(name, str) or len(name) > 214 or not PACKAGE_RE.fullmatch(name):
        raise PluginFileError('invalid', '插件包名非法')
    return checked_path(root, name, allow_missing=allow_missing)


def read_json(filename, limit=MAX_JSON_BYTES):
    if not stat.S_ISREG(os.lstat(filename).st_mode):
        raise PluginFileError('invalid', '插件元数据不是普通文件')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    fd = os.open(filename, flags)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise PluginFileError('invalid', '插件元数据不是普通文件或超过大小限制')
        raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise PluginFileError('invalid', '插件元数据超过大小限制')
        value = json.loads(raw.decode('utf-8'))
        if not isinstance(value, dict):
            raise PluginFileError('invalid', '插件元数据格式非法')
        return value, raw, info


def atomic_json(filename, value):
    filename = Path(filename)
    temp = filename.with_name(filename.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False)
        os.replace(temp, filename)
    finally:
        if temp.exists():
            temp.unlink()


@contextmanager
def storage_lock(root):
    root = checked_root(root)
    filename = checked_path(root, LOCK_FILE, allow_missing=True)
    fd = os.open(filename, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    locked = False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise PluginFileError('unsafe_path', '插件目录锁不是普通文件')
        try:
            if os.name == 'nt':
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b'0')
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            raise PluginFileError('busy', '插件正在安装或被其他操作占用，请稍后重试') from exc
        yield root
    finally:
        if locked:
            if os.name == 'nt':
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def plugin_entry(root, name):
    directory = package_path(root, name)
    if not directory.is_dir():
        raise PluginFileError('invalid', '插件目录不是文件夹')
    metadata = checked_path(root, name + '/package.json')
    pkg, raw, info = read_json(metadata)
    if pkg.get('name') != name or not isinstance(pkg.get('version'), str) or not pkg['version'] or len(pkg['version']) > 255:
        raise PluginFileError('invalid', 'package.json 的包名或版本与目录不一致')
    identity = f'{info.st_dev}:{info.st_ino}:{info.st_mtime_ns}:{directory.stat().st_ino}'.encode()
    return {'name': name, 'version': pkg['version'], 'fingerprint': hashlib.sha256(raw + identity).hexdigest(), 'path': str(directory)}


def _trash_root(root, create=False):
    target = checked_path(root, TRASH_DIR, allow_missing=True)
    if create:
        target.mkdir(mode=0o700, exist_ok=True)
    if target.exists() and not target.is_dir():
        raise PluginFileError('unsafe_path', '插件隔离区不是文件夹')
    return target


def trash_record(root, trash_id):
    if not isinstance(trash_id, str) or not TRASH_RE.fullmatch(trash_id):
        raise PluginFileError('invalid', '隔离记录标识非法')
    directory = checked_path(root, f'{TRASH_DIR}/{trash_id}')
    meta, _, _ = read_json(checked_path(root, f'{TRASH_DIR}/{trash_id}/meta.json'))
    package_path(root, meta.get('name'), allow_missing=True)
    if meta.get('trashId') != trash_id or not isinstance(meta.get('expiresAtEpoch'), (int, float)):
        raise PluginFileError('invalid', '隔离记录损坏')
    return directory, meta


def list_trash(root):
    result = []
    trash = _trash_root(root)
    if not trash.exists():
        return result
    for entry in sorted(trash.iterdir()):
        if len(result) >= MAX_PLUGINS:
            break
        if not TRASH_RE.fullmatch(entry.name):
            continue
        try:
            directory, meta = trash_record(root, entry.name)
            payload = checked_path(root, f'{TRASH_DIR}/{entry.name}/plugin', allow_missing=True)
            if payload.is_dir() and meta['expiresAtEpoch'] > time.time():
                result.append({key: meta.get(key) for key in ('trashId', 'name', 'version', 'deletedAt', 'expiresAt')})
        except (OSError, ValueError, PluginFileError):
            continue
    return result


def cleanup_expired(root):
    """只清理本功能生成、校验通过的隔离记录，不扫描或删除用户的 .bak 目录。"""
    trash = _trash_root(root)
    if not trash.exists():
        return
    for entry in list(trash.iterdir())[:MAX_PLUGINS]:
        if not TRASH_RE.fullmatch(entry.name):
            continue
        try:
            directory, meta = trash_record(root, entry.name)
            if meta['expiresAtEpoch'] <= time.time():
                shutil.rmtree(checked_path(root, f'{TRASH_DIR}/{entry.name}'))
        except (OSError, ValueError, PluginFileError):
            continue


def scan_plugins(root):
    root = checked_root(root)
    names, excluded = [], 0
    for item in sorted(root.iterdir()):
        if item.name.startswith('.'):
            if item.is_dir() and not item.is_symlink() and not item.name.startswith('.hub-'):
                excluded += 1
            continue
        if item.name.startswith('@') and item.is_dir() and not item.is_symlink():
            for child in sorted(item.iterdir()):
                if child.name.startswith('.'):
                    excluded += 1
                else:
                    names.append(f'{item.name}/{child.name}')
        else:
            names.append(item.name)
    entries, errors = [], []
    for name in names[:MAX_PLUGINS]:
        try:
            entries.append(plugin_entry(root, name))
        except (OSError, ValueError, PluginFileError) as exc:
            errors.append({'name': name[:214], 'error': str(exc)[:200]})
    report = None
    report_error = None
    try:
        report, _, _ = read_json(checked_path(root, REPORT_FILE))
        if report.get('schemaVersion') != 1 or not isinstance(report.get('items'), list):
            raise PluginFileError('invalid', '同步报告格式不支持')
    except (OSError, ValueError, PluginFileError) as exc:
        report = None
        report_error = '尚无同步回执' if isinstance(exc, (FileNotFoundError, PluginFileError)) and getattr(exc, 'code', 'not_found') == 'not_found' else '同步回执无法读取'
    return {'schemaVersion': 1, 'status': 'ok', 'scannedAt': utc_stamp(), 'entries': entries, 'errors': errors,
            'truncated': len(names) > MAX_PLUGINS, 'excludedCount': excluded, 'sync': report,
            'syncError': report_error, 'recycle': list_trash(root)}


def remove_plugin(root, name, expected_fingerprint, request_id, on_remove=None, on_rollback=None):
    try:
        trash_id = uuid.UUID(str(request_id)).hex
    except ValueError as exc:
        raise PluginFileError('invalid', '操作标识非法') from exc
    with storage_lock(root) as root:
        _trash_root(root, create=True)
        receipt = root / TRASH_DIR / trash_id
        if receipt.exists():
            _, previous = trash_record(root, trash_id)
            if previous.get('name') != name or previous.get('fingerprint') != expected_fingerprint:
                raise PluginFileError('conflict', '重复操作的参数不一致')
            if previous.get('state') != 'prepared' or (receipt / 'plugin').is_dir():
                return previous
            # 在写入准备记录后、移动目录前中断的命令可以安全重试。
            shutil.rmtree(receipt)
        entry = plugin_entry(root, name)
        if not expected_fingerprint or entry['fingerprint'] != expected_fingerprint:
            raise PluginFileError('conflict', '插件版本或文件已变化，请刷新后重新确认')
        deleted = time.time()
        meta = {**entry, 'trashId': trash_id, 'deletedAt': utc_stamp(deleted), 'expiresAt': utc_stamp(deleted + RETENTION_SECONDS),
                'expiresAtEpoch': deleted + RETENTION_SECONDS, 'state': 'prepared'}
        receipt.mkdir(mode=0o700)
        atomic_json(receipt / 'meta.json', meta)
        source = package_path(root, name)
        os.rename(source, receipt / 'plugin')
        try:
            if on_remove:
                meta['linkRemoved'] = bool(on_remove())
            meta['state'] = 'deleted'
            atomic_json(receipt / 'meta.json', meta)
        except Exception:
            os.rename(receipt / 'plugin', package_path(root, name, allow_missing=True))
            # docker exec 可能已移除链接但回包中断；恢复目录后再确保链接可用。
            if on_remove and on_rollback:
                on_rollback(name)
            shutil.rmtree(receipt)
            raise
        cleanup_expired(root)
        return meta


def restore_plugin(root, trash_id, on_restore=None, on_rollback=None):
    with storage_lock(root) as root:
        directory, meta = trash_record(root, trash_id)
        if meta.get('state') == 'restored':
            return meta
        if meta['expiresAtEpoch'] <= time.time():
            raise PluginFileError('expired', '文件已超过 7 天恢复期限')
        target = package_path(root, meta['name'], allow_missing=True)
        if target.exists():
            raise PluginFileError('conflict', '本地已有此插件，不会覆盖现有文件')
        payload = checked_path(root, f'{TRASH_DIR}/{trash_id}/plugin')
        pkg, _, _ = read_json(checked_path(root, f'{TRASH_DIR}/{trash_id}/plugin/package.json'))
        if pkg.get('name') != meta['name'] or pkg.get('version') != meta['version']:
            raise PluginFileError('conflict', '隔离文件内容已变化，拒绝恢复')
        target.parent.mkdir(parents=True, exist_ok=True)
        target = package_path(root, meta['name'], allow_missing=True)
        os.rename(payload, target)
        try:
            if on_restore and meta.get('linkRemoved'):
                on_restore(meta['name'])
            meta['state'] = 'restored'
            atomic_json(directory / 'meta.json', meta)
        except Exception:
            os.rename(target, payload)
            if on_restore and meta.get('linkRemoved') and on_rollback:
                on_rollback(meta['name'])
            raise
        return meta
