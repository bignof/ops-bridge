import json
import logging
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from config import AGENT_ID, CHINA_TZ, HEALTH_HOST, HEALTH_PORT
from core.ws_client import get_connection_state

logger = logging.getLogger(__name__)


def _format_timestamp(value):
    if value is None:
        return None
    return datetime.fromtimestamp(value, CHINA_TZ).isoformat(timespec='seconds')


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != '/health':
            self.send_response(404)
            self.end_headers()
            return

        state = get_connection_state()
        healthy = bool(state.get('connected'))
        payload = {
            'status': 'ok' if healthy else 'degraded',
            'agentId': AGENT_ID,
            'connected': healthy,
            'lastConnectTs': _format_timestamp(state.get('last_connect_ts')),
            'lastDisconnectTs': _format_timestamp(state.get('last_disconnect_ts')),
            'lastHeartbeatTs': _format_timestamp(state.get('last_heartbeat_ts')),
            'lastMessageTs': _format_timestamp(state.get('last_message_ts')),
            'lastError': state.get('last_error'),
        }

        body = json.dumps(payload).encode('utf-8')
        self.send_response(200 if healthy else 503)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def start_health_server():
    server = ThreadingHTTPServer((HEALTH_HOST, HEALTH_PORT), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f'Health server listening on http://{HEALTH_HOST}:{HEALTH_PORT}/health')
    return server