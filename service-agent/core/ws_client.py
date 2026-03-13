import json
import logging
import threading
import time

import websocket

from config import AGENT_ID, AGENT_KEY, HEARTBEAT_INTERVAL, WS_URL
from core.handlers import dispatch, send_message

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
    _start_heartbeat(ws)


def _on_message(ws, message):
    try:
        _update_state(last_message_ts=time.time())
        data = json.loads(message)
        msg_type = data.get('type')
        if msg_type == 'command':
            # 在独立线程中执行，避免阻塞 WebSocket 接收循环
            threading.Thread(target=dispatch, args=(ws, data), daemon=True).start()
        elif msg_type == 'ping':
            send_message(ws, {'type': 'pong', 'timestamp': time.time()})
    except Exception as e:
        logger.error(f"Error processing message: {e}")


def _on_error(ws, error):
    _update_state(last_error=str(error))
    logger.error(f"WebSocket error: {error}")


def _on_close(ws, close_status_code, close_msg):
    _update_state(connected=False, last_disconnect_ts=time.time())
    logger.warning(f"Connection closed: {close_status_code} {close_msg}")


def _start_heartbeat(ws):
    global _heartbeat_thread

    def _beat():
        while ws and ws.keep_running:
            time.sleep(HEARTBEAT_INTERVAL)
            if ws and ws.keep_running:
                _update_state(last_heartbeat_ts=time.time())
                send_message(ws, {'type': 'heartbeat', 'ts': time.time()})

    _heartbeat_thread = threading.Thread(target=_beat, daemon=True)
    _heartbeat_thread.start()


def connect():
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
