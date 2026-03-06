import json
import logging
import threading
import time

import websocket

from config import AGENT_ID, HEARTBEAT_INTERVAL, TOKEN, WS_URL
from core.handlers import dispatch, send_message

logger = logging.getLogger(__name__)

_heartbeat_thread = None


def _on_open(ws):
    logger.info("Connected to ServiceHub!")
    _start_heartbeat(ws)


def _on_message(ws, message):
    try:
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
    logger.error(f"WebSocket error: {error}")


def _on_close(ws, close_status_code, close_msg):
    logger.warning(f"Connection closed: {close_status_code} {close_msg}")


def _start_heartbeat(ws):
    global _heartbeat_thread

    def _beat():
        while ws and ws.keep_running:
            time.sleep(HEARTBEAT_INTERVAL)
            if ws and ws.keep_running:
                send_message(ws, {'type': 'heartbeat', 'ts': time.time()})

    _heartbeat_thread = threading.Thread(target=_beat, daemon=True)
    _heartbeat_thread.start()


def connect():
    url = f"{WS_URL}/{AGENT_ID}?token={TOKEN}"
    logger.info(f"Connecting to {url}...")
    ws = websocket.WebSocketApp(
        url,
        on_open=_on_open,
        on_message=_on_message,
        on_error=_on_error,
        on_close=_on_close,
    )
    ws.run_forever(ping_interval=20, ping_timeout=10)
