"""
status_reporter.py — 巡检目标清单维护 + 定时状态上报。

watch_targets 由 hub 主动推送（agent 连接建立时、该 agent 名下 deployment 增删改时），
agent 侧只是被动存最新的一份（覆盖式，不增量合并）；定时巡检按这份清单跑。
"""
import logging
import os
import threading
import time

from config import PLUGIN_FOLLOW_UP_INTERVAL, PLUGIN_FOLLOW_UP_TIMEOUT, STATUS_REPORT_INTERVAL
from core.handlers import send_message
from services.compose import collect_service_statuses
from services.plugins import enrich_statuses

logger = logging.getLogger(__name__)

_watch_targets: list[dict] = []
_watch_targets_lock = threading.Lock()


def set_watch_targets(targets):
    """覆盖式更新：hub 每次推送的都是该 agent 名下的完整清单，不是增量。"""
    global _watch_targets
    normalized = sorted(list(targets or []), key=lambda t: str(t.get('deploymentId', '')))
    with _watch_targets_lock:
        if _watch_targets == normalized:
            return False
        _watch_targets = normalized
    logger.info(f"watch_targets updated: {len(_watch_targets)} deployment(s)")
    return True


def get_watch_targets():
    with _watch_targets_lock:
        return list(_watch_targets)


_report_thread = None
_active_ws = None
_refresh_lock = threading.Lock()
_refresh_services = set()
_refresh_running = False
_collection_lock = threading.Lock()


def _collect_and_send(ws, services_filter=None, dirs=None):
    # 周期巡检和即时通知不并发重复扫描 Docker；单轮所有部署共享一次 inspect。
    with _collection_lock:
        return _collect_batch(ws, services_filter, dirs)


def _dir_key(path):
    return os.path.normcase(os.path.abspath(str(path)))


def _collect_batch(ws, services_filter=None, dirs=None):
    reports = []
    for target in get_watch_targets():
        if services_filter and target.get('service') and target['service'] not in services_filter:
            continue
        if dirs is not None and _dir_key(target.get('dir', '')) not in dirs:
            continue
        services = collect_service_statuses(target['dir'])
        if not services:
            continue
        reports.append({'deploymentId': target['deploymentId'], 'services': services})
    if reports:
        enriched = enrich_statuses([service for report in reports for service in report['services']])
        offset = 0
        for report in reports:
            count = len(report['services'])
            report['services'] = enriched[offset:offset + count]
            offset += count
        send_message(ws, {'type': 'status_report', 'reports': reports})
    return reports


def start_status_reporting(ws):
    """独立于心跳的定时巡检线程：按 STATUS_REPORT_INTERVAL 周期跑一次 _collect_and_send。"""
    global _report_thread, _active_ws
    _active_ws = ws

    def _loop():
        while ws and ws.keep_running:
            time.sleep(STATUS_REPORT_INTERVAL)
            if ws and ws.keep_running:
                try:
                    _collect_and_send(ws)
                except Exception as e:
                    logger.error(f"status report cycle failed: {e}")

    _report_thread = threading.Thread(target=_loop, daemon=True)
    _report_thread.start()


def stop_status_reporting(ws):
    global _active_ws
    if _active_ws is ws:
        _active_ws = None


def request_report(service=None):
    """启动脚本通知、watch_targets 更新和命令结束复用此入口；合并并发通知。"""
    global _refresh_running
    ws = _active_ws
    if not ws or not ws.keep_running:
        return False
    if service and not any(not t.get('service') or t.get('service') == service for t in get_watch_targets()):
        return False
    if any(not target.get('service') for target in get_watch_targets()):
        service = None
    with _refresh_lock:
        _refresh_services.add(service or '*')
        if _refresh_running:
            return True
        _refresh_running = True

    def work():
        global _refresh_running
        while True:
            with _refresh_lock:
                if not _refresh_services:
                    _refresh_running = False
                    return
                pending = set(_refresh_services)
                _refresh_services.clear()
            target_ws = _active_ws
            if not target_ws or not target_ws.keep_running:
                continue
            try:
                _collect_and_send(target_ws, None if '*' in pending else pending)
            except Exception as exc:
                logger.warning('插件状态采集失败: %s', exc)

    threading.Thread(target=work, daemon=True).start()
    return True


_follow_ups: dict[str, dict] = {}  # 目录 → {'deadline': monotonic 截止时间, 'gen': 命令代次}


def _sync_settled(reports):
    """本次启动的插件同步都已结束（含失败）才停止跟踪；没有插件目录的部署无需跟踪。"""
    inventories = [service.get('plugins') for report in reports for service in report['services']
                   if isinstance(service.get('plugins'), dict)]
    return all(item.get('status') == 'ok' and item.get('syncCurrent') and (item.get('sync') or {}).get('finishedAt')
               for item in inventories)


def follow_up(project_dir):
    """restart/update 结束后按固定间隔采集该部署，直到插件同步结束或超时。

    worker 直连 hub 拉清单或使用旧镜像时不会通知 Agent，只靠命令结束时的一次采集（早于同步）和
    周期巡检会让 hub 长时间停在「待安装」。同一目录只保留一个跟踪线程：跟踪期间再来命令只顺延
    截止时间并递增代次，已判定结束的采集若早于新命令则继续跟踪。断连期间跳过采集，重连后继续。
    """
    key = _dir_key(project_dir)
    with _refresh_lock:
        state = _follow_ups.get(key)
        deadline = time.monotonic() + PLUGIN_FOLLOW_UP_TIMEOUT
        if state:
            state.update(deadline=deadline, gen=state['gen'] + 1)
            return False
        _follow_ups[key] = {'deadline': deadline, 'gen': 0}

    def work():
        try:
            while True:
                with _refresh_lock:
                    state = _follow_ups[key]
                    if time.monotonic() >= state['deadline']:
                        _follow_ups.pop(key, None)
                        return
                    generation = state['gen']
                ws = _active_ws
                settled = False
                if ws and ws.keep_running:
                    try:
                        settled = _sync_settled(_collect_and_send(ws, dirs={key}))
                    except Exception as exc:
                        logger.warning('插件同步跟踪采集失败: %s', exc)
                if settled:
                    with _refresh_lock:
                        # 判定与出队同在锁内，避免与新命令的顺延交错后丢失跟踪
                        if _follow_ups[key]['gen'] == generation:
                            _follow_ups.pop(key, None)
                            return
                time.sleep(PLUGIN_FOLLOW_UP_INTERVAL)
        except BaseException:
            with _refresh_lock:
                _follow_ups.pop(key, None)
            raise

    threading.Thread(target=work, daemon=True).start()
    return True
