import hmac
import json
import logging
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from config import AGENT_ID, AGENT_LOCAL_SECRET, CHINA_TZ, HEALTH_HOST, HEALTH_PORT
from core import plugin_query
from core.handlers import get_command_execution_state
from core.ws_client import get_connection_state

logger = logging.getLogger(__name__)

QUERY_PLUGIN_TIMEOUT = 8.0  # 必须 < 15s（sync-plugins.js:201 硬编码客户端超时上限）


def _format_timestamp(value):
    if value is None:
        return None
    return datetime.fromtimestamp(value, CHINA_TZ).isoformat(timespec='seconds')


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/queryPlugin'):
            return self._handle_query_plugin()
        if self.path != '/health':
            self.send_response(404)
            self.end_headers()
            return

        state = get_connection_state()
        execution_state = get_command_execution_state()
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
            'commandExecution': {
                'activeCommands': execution_state['activeCommands'],
                'queuedCommands': execution_state['queuedCommands'],
                'projects': [
                    {
                        'projectDir': item['projectDir'],
                        'activeRequestId': item['activeRequestId'],
                        'activeAction': item['activeAction'],
                        'activeSinceTs': _format_timestamp(item['activeSinceTs']),
                        'queuedCount': item['queuedCount'],
                    }
                    for item in execution_state['projects']
                ],
            },
        }

        body = json.dumps(payload).encode('utf-8')
        self.send_response(200 if healthy else 503)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_query_plugin(self):
        from urllib.parse import urlparse, parse_qs
        if not AGENT_LOCAL_SECRET:
            # 评审 H1：secret 未配置 fail-closed（403），绝不放行——端点发布在宿主全网卡，
            # 空 secret 放行等于把该 namespace 插件清单匿名公开给同网段
            logger.warning('queryPlugin 拒绝请求：AGENT_LOCAL_SECRET 未配置')
            self.send_response(403)
            self.end_headers()
            return
        supplied = self.headers.get('X-Agent-Secret') or ''
        # 评审 M4：恒时比较（与 hub 侧 safeEqualHex 对齐），防逐字节短路的时序侧信道
        if not hmac.compare_digest(supplied.encode('utf-8'), AGENT_LOCAL_SECRET.encode('utf-8')):
            self.send_response(401)
            self.end_headers()
            return
        service = (parse_qs(urlparse(self.path).query).get('service') or [''])[0]
        if not service:
            self.send_response(400)   # 评审 L3：与 hubSync:queryPlugin 的必填校验对齐，不折叠成 200 []
            self.end_headers()
            return
        if not get_connection_state().get('connected'):
            self.send_response(503)   # 未连 hub：让 sync-plugins.js 回退本地版本
            self.end_headers()
            return
        items = plugin_query.request(service, QUERY_PLUGIN_TIMEOUT)
        if items is None:
            self.send_response(504)
            self.end_headers()
            return
        body = json.dumps(items).encode('utf-8')   # 顶层纯数组（request 已解包）
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def start_health_server():
    if not AGENT_LOCAL_SECRET:
        logger.warning('AGENT_LOCAL_SECRET 未配置：/queryPlugin 将拒绝所有请求（403）。'
                       '如需 admin 经本机 agent 拉插件，请在 .env 配置该值并同步进 admin 的 sync-plugins 配置')
    server = ThreadingHTTPServer((HEALTH_HOST, HEALTH_PORT), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f'Health server listening on http://{HEALTH_HOST}:{HEALTH_PORT}/health')
    return server