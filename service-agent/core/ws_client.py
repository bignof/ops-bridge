import json
import logging
import threading
import time

import websocket

from config import AGENT_ID, AGENT_KEY, HEARTBEAT_INTERVAL, OUTBOX_PATH, WS_URL
from core import outbox, plugin_query
from services import agent_upgrade
from core.handlers import dispatch, send_message
from core.log_sessions import start_log_session, stop_log_session, stop_all as stop_all_log_sessions
from core.compose_inspect import handle_discover as handle_compose_discover, handle_inspect as handle_compose_inspect
from core.log_fetch import abort_all as abort_all_fetch, start_fetch as start_logfile_fetch
from core.log_follow import start_follow as start_logfile_follow, stop_all as stop_all_follow, stop_follow as stop_logfile_follow
from core.log_paths import handle_list as handle_logfile_list
from core.status_reporter import set_watch_targets, start_status_reporting, stop_status_reporting, request_report

logger = logging.getLogger(__name__)

_heartbeat_thread = None
_state = {
    'connected': False,
    'last_connect_ts': None,
    'last_disconnect_ts': None,
    'last_heartbeat_ts': None,
    'last_message_ts': None,
    'last_error': None,
}
_state_lock = threading.Lock()


def _update_state(**kwargs):
    with _state_lock:
        _state.update(kwargs)


def get_connection_state():
    with _state_lock:
        return dict(_state)


def _on_open(ws):
    logger.info("Connected to ServiceHub!")
    now = time.time()
    _update_state(
        connected=True,
        last_connect_ts=now,
        last_message_ts=now,
        last_error=None,
    )
    # 出站队列切到本条新连接并立即全量补投——上一条连接死亡窗口内丢失的 result 由此送达。
    # sender 必须用会抛错的裸 ws.send:吞异常的 send_message 会让 flush 把失败发送也当成功计预算
    outbox.set_sender(lambda message: ws.send(json.dumps(message)))
    plugin_query.set_sender(lambda message: ws.send(json.dumps(message)))
    outbox.flush(force=True)
    _start_heartbeat(ws)
    start_status_reporting(ws)
    threading.Thread(target=agent_upgrade.on_connected, args=(ws,), daemon=True).start()


def _on_message(ws, message):
    try:
        _update_state(last_message_ts=time.time())
        data = json.loads(message)
        msg_type = data.get('type')
        if msg_type == 'agent_upgrade':
            threading.Thread(target=agent_upgrade.start_upgrade, args=(ws, data), daemon=True).start()
        elif msg_type == 'agent_upgrade_confirm':
            agent_upgrade.confirm(data)
        elif msg_type == 'command':
            # 在独立线程中执行，避免阻塞 WebSocket 接收循环
            threading.Thread(target=dispatch, args=(ws, data), daemon=True).start()
        elif msg_type == 'logs_start':
            start_log_session(ws, data)
        elif msg_type == 'logs_stop':
            stop_log_session(data)
        elif msg_type == 'logfile_list':
            threading.Thread(target=handle_logfile_list, args=(ws, data), daemon=True).start()
        elif msg_type == 'logfile_fetch':
            start_logfile_fetch(ws, data)  # 自起线程
        elif msg_type == 'logfile_follow':
            start_logfile_follow(ws, data)  # 自起线程
        elif msg_type == 'logfile_unfollow':
            stop_logfile_follow(data)
        elif msg_type == 'compose_discover':
            threading.Thread(target=handle_compose_discover, args=(ws, data), daemon=True).start()
        elif msg_type == 'compose_inspect':
            threading.Thread(target=handle_compose_inspect, args=(ws, data), daemon=True).start()
        elif msg_type == 'result_ack':
            # hub 已确认 result 落库:出站队列清账,停止补投
            outbox.ack(data.get('requestId'))
        elif msg_type == 'ping':
            send_message(ws, {'type': 'pong', 'timestamp': time.time()})
        elif msg_type == 'watch_targets':
            if set_watch_targets(data.get('targets')):
                request_report()
        elif msg_type == 'plugin_query_result':
            plugin_query.resolve(data.get('requestId'), data.get('plugins', []))
    except Exception as e:
        logger.error(f"Error processing message: {e}")


def _on_error(ws, error):
    _update_state(last_error=str(error))
    logger.error(f"WebSocket error: {error}")


def _on_close(ws, close_status_code, close_msg):
    stop_status_reporting(ws)
    _update_state(connected=False, last_disconnect_ts=time.time())
    outbox.clear_sender()  # 补投暂停,等重连的 _on_open 换新通道
    plugin_query.clear_sender()  # 与 _on_open 的成对注册对称,断连期间 request() 走 sender-is-None 快速失败
    stopped = stop_all_log_sessions()  # 泄漏修复：hub 断了，compose logs -f 子进程不能再挂着
    if stopped:
        logger.info(f"Stopped {stopped} log session(s) on disconnect")
    followed = stop_all_follow()
    if followed:
        logger.info(f"Stopped {followed} follow session(s) on disconnect")
    abort_all_fetch()
    logger.warning(f"Connection closed: {close_status_code} {close_msg}")


def _start_heartbeat(ws):
    global _heartbeat_thread

    def _beat():
        while ws and ws.keep_running:
            time.sleep(HEARTBEAT_INTERVAL)
            if ws and ws.keep_running:
                _update_state(last_heartbeat_ts=time.time())
                send_message(ws, {'type': 'heartbeat', 'ts': time.time()})
                agent_upgrade.report(ws)
                outbox.flush()  # 按退避补投未确认 result(连接存续但此前发送失败/ack 丢失的场景)

    _heartbeat_thread = threading.Thread(target=_beat, daemon=True)
    _heartbeat_thread.start()


def connect():
    outbox.configure(OUTBOX_PATH)  # 幂等:每轮重连前确保已从磁盘恢复(进程首连即初始化)
    url = f"{WS_URL}/{AGENT_ID}?key={AGENT_KEY}"
    logger.info("Connecting to %s/%s...", WS_URL, AGENT_ID)
    ws = websocket.WebSocketApp(
        url,
        on_open=_on_open,
        on_message=_on_message,
        on_error=_on_error,
        on_close=_on_close,
    )
    ws.run_forever(ping_interval=20, ping_timeout=10)
