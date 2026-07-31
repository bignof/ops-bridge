"""
status_reporter.py — 巡检目标清单维护 + 定时状态上报。

watch_targets 由 hub 主动推送（agent 连接建立时、该 agent 名下 deployment 增删改时），
agent 侧只是被动存最新的一份（覆盖式，不增量合并）；定时巡检按这份清单跑。
"""
import logging
import threading

logger = logging.getLogger(__name__)

_watch_targets: list[dict] = []
_watch_targets_lock = threading.Lock()


def set_watch_targets(targets):
    """覆盖式更新：hub 每次推送的都是该 agent 名下的完整清单，不是增量。"""
    global _watch_targets
    with _watch_targets_lock:
        _watch_targets = list(targets or [])
    logger.info(f"watch_targets updated: {len(_watch_targets)} deployment(s)")


def get_watch_targets():
    with _watch_targets_lock:
        return list(_watch_targets)
