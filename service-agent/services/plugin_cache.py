"""
plugin_cache.py — agent 本机插件缓存(P1)

内容寻址:缓存键 = attachmentId(同 id = 同包),命中直接给;未命中由调用方回源后落盘。
同主机多容器同包**只回源一次**(per-attachmentId 锁 + 双检)。容量上限 + LRU(按 mtime)淘汰。
只缓存 .tgz 字节,不解包(解包/安装在 worker 侧 sync-plugins)。

锁模式照搬 core.handlers 的 per-key 锁(_locks + _locks_guard + lazy create)。
"""
from __future__ import annotations

import logging
import os
import re
import threading
from typing import Callable

import config

logger = logging.getLogger(__name__)

# attachmentId 仅允许安全字符,防路径穿越(它是平台侧的 id,正常为数字 / uuid)。
_SAFE_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _safe_id(attachment_id) -> str:
    aid = str(attachment_id).strip()
    if '..' in aid or not _SAFE_ID.match(aid):
        raise ValueError(f'非法 attachmentId: {attachment_id!r}')
    return aid


def _get_lock(attachment_id: str) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(attachment_id)
        if lock is None:
            lock = threading.Lock()
            _locks[attachment_id] = lock
        return lock


def cache_dir() -> str:
    d = config.PLUGIN_CACHE_DIR
    os.makedirs(d, exist_ok=True)
    return d


def cache_path(attachment_id) -> str:
    return os.path.join(cache_dir(), f'{_safe_id(attachment_id)}.tgz')


def is_cached(attachment_id) -> bool:
    try:
        p = cache_path(attachment_id)
    except ValueError:
        return False
    return os.path.isfile(p) and os.path.getsize(p) > 0


def _touch(path: str) -> None:
    # 更新 mtime,LRU 据此判定「最近使用」。
    try:
        os.utime(path, None)
    except OSError:
        pass


def get_or_fetch(attachment_id, fetcher: Callable[[str], None]) -> str:
    """
    返回该 attachmentId 的 .tgz 缓存路径;未命中则用 fetcher(临时路径) 回源后**原子落盘**。
    - fetcher 把字节写入传入的临时路径(写失败应抛异常);产出空文件视为失败。
    - 同主机多容器并发同包:per-attachmentId 锁 + 双检,只有一个真正回源。
    """
    aid = _safe_id(attachment_id)
    final = cache_path(aid)
    if is_cached(aid):
        _touch(final)
        return final

    lock = _get_lock(aid)
    with lock:
        # 双检:等锁期间可能已被其他线程填好(同主机多容器只回源一次)。
        if is_cached(aid):
            _touch(final)
            return final
        tmp = f'{final}.tmp.{os.getpid()}.{threading.get_ident()}'
        try:
            fetcher(tmp)
            if not os.path.isfile(tmp) or os.path.getsize(tmp) == 0:
                raise RuntimeError(f'回源未产出有效文件: attachmentId={aid}')
            os.replace(tmp, final)  # 原子替换,避免读到半截文件
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    _evict_if_needed()
    return final


def _evict_if_needed() -> None:
    cap = config.PLUGIN_CACHE_MAX_BYTES
    if cap is None or cap <= 0:
        return
    d = cache_dir()
    entries = []
    total = 0
    for name in os.listdir(d):
        if not name.endswith('.tgz'):
            continue
        p = os.path.join(d, name)
        try:
            st = os.stat(p)
        except OSError:
            continue
        entries.append((st.st_mtime, st.st_size, p))
        total += st.st_size
    if total <= cap:
        return
    # LRU:mtime 升序(最久未用先删),删到不超过 cap。
    entries.sort(key=lambda e: e[0])
    for _mtime, size, p in entries:
        if total <= cap:
            break
        try:
            os.remove(p)
            total -= size
            logger.info('插件缓存 LRU 淘汰: %s (%d bytes)', os.path.basename(p), size)
        except OSError:
            pass
