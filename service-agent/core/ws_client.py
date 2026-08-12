import json
import logging
import threading
import time

import websocket

from config import AGENT_ID, AGENT_KEY, HEARTBEAT_INTERVAL, OUTBOX_PATH, WS_URL
from core import outbox, plugin_query
from core.handlers import dispatch, send_message
from core.log_sessions import start_log_session, stop_log_session
from core.status_reporter import set_watch_targets, start_status_reporting

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


def _on_message(ws, message):
    try:
        _update_state(last_message_ts=time.time())
        data = json.loads(message)
        msg_type = data.get('type')
        if msg_type == 'command':
            # 在独立线程中执行，避免阻塞 WebSocket 接收循环
            threading.Thread(target=dispatch, args=(ws, data), daemon=True).start()
        elif msg_type == 'logs_start':
            start_log_session(ws, data)
        elif msg_type == 'logs_stop':
            stop_log_session(data)
        elif msg_type == 'result_ack':
            # hub 已确认 result 落库:出站队列清账,停止补投
            outbox.ack(data.get('requestId'))
        elif msg_type == 'ping':
            send_message(ws, {'type': 'pong', 'timestamp': time.time()})
        elif msg_type == 'watch_targets':
            set_watch_targets(data.get('targets'))
        elif msg_type == 'plugin_query_result':
            plugin_query.resolve(data.get('requestId'), data.get('plugins', []))
    except Exception as e:
        logger.error(f"Error processing message: {e}")


def _on_error(ws, error):
    _update_state(last_error=str(error))
    logger.error(f"WebSocket error: {error}")


def _on_close(ws, close_status_code, close_msg):
    _update_state(connected=False, last_disconnect_ts=time.time())
    outbox.clear_sender()  # 补投暂停,等重连的 _on_open 换新通道
    logger.warning(f"Connection closed: {close_status_code} {close_msg}")


def _start_heartbeat(ws):
    global _heartbeat_thread

    def _beat():
        while ws and ws.keep_running:
            time.sleep(HEARTBEAT_INTERVAL)
            if ws and ws.keep_running:
                _update_state(last_heartbeat_ts=time.time())
                send_message(ws, {'type': 'heartbeat', 'ts': time.time()})
                outbox.flush()  # 按退避补投未确认 result(连接存续但此前发送失败/ack 丢失的场景)

    _heartbeat_thread = threading.Thread(target=_beat, daemon=True)
    _heartbeat_thread.start()


def connect():
    outbox.configure(OUTBOX_PATH)  # 幂等:每轮重连前确保已从磁盘恢复(进程首连即初始化)
    url = f"{WS_URL}/{AGENT_ID}?key={AGENT_KEY}"
    logger.info(f"Connecting to {url}...")
    ws = websocket.WebSocketApp(
        url,
        on_open=_on_open,
        on_message=_on_message,
        on_error=_on_error,
        on_close=_on_close,
    )
    ws.run_forever(ping_interval=20, ping_timeout=10)
