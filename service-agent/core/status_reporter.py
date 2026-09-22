"""
status_reporter.py — 巡检目标清单维护 + 定时状态上报。

watch_targets 由 hub 主动推送（agent 连接建立时、该 agent 名下 deployment 增删改时），
agent 侧只是被动存最新的一份（覆盖式，不增量合并）；定时巡检按这份清单跑。
"""
import logging
import threading
import time

from config import STATUS_REPORT_INTERVAL
from core.handlers import send_message
from services.compose import collect_service_statuses
from services.plugins import enrich_statuses

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


_report_thread = None
_active_ws = None
_refresh_lock = threading.Lock()
_refresh_services = set()
_refresh_running = False
_collection_lock = threading.Lock()


def _collect_and_send(ws, services_filter=None):
    # 周期巡检和即时通知不并发重复扫描 Docker；单轮所有部署共享一次 inspect。
    with _collection_lock:
        _collect_batch(ws, services_filter)


def _collect_batch(ws, services_filter=None):
    reports = []
    for target in get_watch_targets():
        if services_filter and target.get('service') and target['service'] not in services_filter:
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
