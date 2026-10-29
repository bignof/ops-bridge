import hmac
import json
import logging
import re
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from config import AGENT_ID, AGENT_LOCAL_SECRET, CHINA_TZ, HEALTH_HOST, HEALTH_PORT
from core import hub_rpc, ops_code, plugin_query
from core.handlers import get_command_execution_state
from core.ws_client import get_connection_state

logger = logging.getLogger(__name__)

QUERY_PLUGIN_TIMEOUT = 8.0  # 必须 < 15s（sync-plugins.js:201 硬编码客户端超时上限）
# 转发给中枢的两个查询的总时限（含等 hub_hello 协商的时间）：
# 必须大于 hub_rpc.NEGOTIATE_SECONDS，否则旧版中枢（始终没有 hub_hello）会被判成超时 504，而不是 501；
# 也要小于应用侧等 agent 的时限（环境开关 10 秒、快照 12 秒），中枢慢时应用拿到的是 agent 的 504，不是自己先超时
ENV_CONFIG_TIMEOUT = 8.0
SNAPSHOT_TIMEOUT = 10.0
# 转发给中枢的本机查询：路径（不含查询串）精确匹配 → (请求帧, 中枢须在 hub_hello.features 声明的特性, 总时限)
HUB_QUERY_ROUTES = {
    '/envConfig': ('env_config_query', 'env_config', ENV_CONFIG_TIMEOUT),
    '/instanceSnapshot': ('instance_snapshot_query', 'instance_snapshot', SNAPSHOT_TIMEOUT),
}
VERIFY_BODY_LIMIT = 4096
OPERATION_RE = re.compile(r'^[A-Za-z0-9_.-]{1,64}:[A-Za-z0-9_.-]{1,64}$')


def _text(value, low, high):
    return isinstance(value, str) and low <= len(value) <= high


def parse_verify_body(raw):
    """校验 /verifyOpsCode 的请求体，只保留协议字段返回；长度按字符计。不合法抛 ValueError。"""
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError('body')
    service, code, operation = payload.get('service'), payload.get('code'), payload.get('operation')
    if (not _text(service, 1, 255) or not _text(code, 1, 64)
            or not isinstance(operation, str) or not OPERATION_RE.fullmatch(operation)):
        raise ValueError('fields')
    fields = {'service': service, 'code': code, 'operation': operation}
    label = payload.get('operationLabel')
    if label is not None:
        if not _text(label, 0, 64):
            raise ValueError('operationLabel')
        fields['operationLabel'] = label
    operator = payload.get('operator')
    if not isinstance(operator, dict):
        raise ValueError('operator')
    user_id, name, ip = operator.get('userId'), operator.get('name'), operator.get('ip')
    valid_id = (isinstance(user_id, int) and not isinstance(user_id, bool) and 0 <= user_id < 2 ** 63) \
        or _text(user_id, 1, 64)
    if not valid_id or (name is not None and not _text(name, 0, 128)) or (ip is not None and not _text(ip, 0, 64)):
        raise ValueError('operator')
    fields['operator'] = {'userId': user_id, 'name': name or '', 'ip': ip or ''}
    file = payload.get('file')
    if file is not None:
        if not isinstance(file, dict):
            raise ValueError('file')
        file_name, size = file.get('name'), file.get('size')
        if (not _text(file_name, 0, 255) or isinstance(size, bool) or not isinstance(size, int)
                or not 0 <= size <= 2 ** 53):
            raise ValueError('file')
        fields['file'] = {'name': file_name, 'size': size}
    target = payload.get('target')
    if target is not None:
        if not _text(target, 0, 255):
            raise ValueError('target')
        fields['target'] = target
    return fields


def _format_timestamp(value):
    if value is None:
        return None
    return datetime.fromtimestamp(value, CHINA_TZ).isoformat(timespec='seconds')


class _HealthHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == '/verifyOpsCode':
            return self._handle_verify_ops_code()
        if self.path != '/pluginSyncChanged':
            self.send_response(404)
            self.end_headers()
            return
        supplied = self.headers.get('X-Agent-Secret') or ''
        if not AGENT_LOCAL_SECRET or not hmac.compare_digest(supplied.encode(), AGENT_LOCAL_SECRET.encode()):
            self.send_response(401 if AGENT_LOCAL_SECRET else 403)
            self.end_headers()
            return
        try:
            length = int(self.headers.get('Content-Length') or '0')
            if length <= 0 or length > 4096:
                raise ValueError('invalid length')
            payload = json.loads(self.rfile.read(length))
            service = payload.get('service')
            if not isinstance(service, str) or not service or len(service) > 255:
                raise ValueError('invalid service')
        except (ValueError, TypeError, AttributeError):
            self.send_response(400)
            self.end_headers()
            return
        from core.status_reporter import request_report
        self.send_response(202 if request_report(service) else 503)
        self.end_headers()

    def do_GET(self):
        path = self.path.partition('?')[0]
        route = HUB_QUERY_ROUTES.get(path)    # 路径精确匹配：/envConfigs、/envConfig/ 都不认
        if route is not None:
            return self._handle_hub_query(path, *route)
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

    def _check_local_secret(self, label):
        """本机端点共用的鉴权：请求头 X-Agent-Secret 须与 AGENT_LOCAL_SECRET 一致。
        通过返回 True；不通过时已经回出响应、返回 False，调用方直接 return：
          AGENT_LOCAL_SECRET 未配置 → 403，并记一条只含端点名（label）的 warning；
          请求头不符或没带          → 401。
        这两种拒绝都没有响应体，也不要给它们加：agent 自己拒绝的 400 / 401 / 403 / 404 一律不带响应体。
        应用侧据此判断：没有响应体的 401 / 403 就是 agent 本机拒绝了密钥；带 {"reason": ...} 的响应不是鉴权拒绝
        （如 403 {"reason": "not_bound"}，以及 hub_rpc.call 折出的 501 / 503 / 504——它们由 agent 自己产生，也带 reason）。
        反过来，没有响应体的不一定是 agent 拒绝的：/queryPlugin 的 404 / 502 来自中枢，同样不带响应体。
        warning 只记 label，绝不记请求头里的值。"""
        if not AGENT_LOCAL_SECRET:
            # 评审 H1：secret 未配置 fail-closed（403），绝不放行——端点发布在宿主全网卡，
            # 空 secret 放行等于把该 namespace 插件清单匿名公开给同网段
            logger.warning('%s 拒绝请求：AGENT_LOCAL_SECRET 未配置', label)
            self.send_response(403)
            self.end_headers()
            return False
        supplied = self.headers.get('X-Agent-Secret') or ''
        # 评审 M4：恒时比较（与 hub 侧 safeEqualHex 对齐），防逐字节短路的时序侧信道
        if not hmac.compare_digest(supplied.encode('utf-8'), AGENT_LOCAL_SECRET.encode('utf-8')):
            self.send_response(401)
            self.end_headers()
            return False
        return True

    def _handle_query_plugin(self):
        if not self._check_local_secret('queryPlugin'):
            return
        query = parse_qs(urlparse(self.path).query)
        service = (query.get('service') or [''])[0]
        if not service:
            self.send_response(400)   # 评审 L3：与 hubSync:queryPlugin 的必填校验对齐，不折叠成 200 []
            self.end_headers()
            return
        # withEnv=1（或 true）：应用要同时拿环境开关，成功时响应由纯数组变成 {plugins, env}；不带的老应用仍是纯数组
        with_env = (query.get('withEnv') or [''])[0] in ('1', 'true')
        if not get_connection_state().get('connected'):
            self.send_response(503)   # 未连 hub：让 sync-plugins.js 回退本地版本
            self.end_headers()
            return
        items = plugin_query.request(service, QUERY_PLUGIN_TIMEOUT, with_env=with_env)
        if items is None:
            self.send_response(504)
            self.end_headers()
            return
        if isinstance(items, plugin_query.QueryError):
            if items.reason == 'not_bound':
                # 这台服务器没有绑定到应用所在的环境 → 403，且带 reason：应用据此和 agent 自己拒绝密钥的 403（没有响应体）区分
                return self._send_json(403, {'reason': 'not_bound'})
            # 服务不存在或未部署到本机 → 404；hub 生成清单失败（如下载基址缺配）→ 502
            self.send_response(404 if items.reason == 'not_found' else 502)
            self.end_headers()
            return
        body = json.dumps(items).encode('utf-8')   # 纯数组；只有带 withEnv 且 hub 回了 env 时才是 {plugins, env}（request 已决定）
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_verify_ops_code(self):
        if not self._check_local_secret('verifyOpsCode'):
            return
        try:
            length = int(self.headers.get('Content-Length') or '0')
            if length <= 0 or length > VERIFY_BODY_LIMIT:
                raise ValueError('length')
            fields = parse_verify_body(self.rfile.read(length))
        except (ValueError, TypeError, RecursionError):
            self.send_response(400)
            self.end_headers()
            return
        status, body = ops_code.verify(fields)
        outcome = body.get('valid') if status == 200 and isinstance(body, dict) else (body or {}).get('reason')
        # 只记服务、操作和结果，绝不记口令
        logger.info('verifyOpsCode service=%r operation=%s status=%s result=%s',
                    fields['service'], fields['operation'], status, outcome)
        self._send_json(status, body)

    def _handle_hub_query(self, path, frame_type, feature, timeout):
        """/envConfig、/instanceSnapshot：应用经本机 agent 向中枢查环境开关、实例快照，agent 只转发，不缓存、不解读。
        鉴权同 /queryPlugin；service（拉取编码）必填，长度按字符计；其余状态码和响应体照 hub_rpc.call 的结果原样发出。

        应用侧靠「有没有 reason」判断是谁拒绝的，所以 agent 自己拒绝的请求一律不带响应体：
        400（service 缺失或超长）、401 / 403（密钥不符、没带、agent 未配置）、404（路径不认）；
        而 hub_rpc.call 折出的错误（中枢经 agent 回来的 403 / 404 / 502，以及断线、不支持、忙、超时的 501 / 503 / 504）
        都带 {"reason": ...}。于是没有 reason 的 401 / 403 是 agent 拒绝了应用的密钥，
        没有 reason 的 404 是 agent 版本旧（还没有这个端点）。不要给 agent 自己的拒绝加响应体。"""
        if not self._check_local_secret(path):
            return
        service = (parse_qs(urlparse(self.path).query).get('service') or [''])[0]
        if not _text(service, 1, 255):
            self.send_response(400)
            self.end_headers()
            return
        status, body = hub_rpc.call(frame_type, {'service': service}, feature, timeout)
        # 只记路径、拉取编码和状态码，不记密钥，也不记结果内容。每个节点每分钟都会查一次，
        # 成功（200）只记 DEBUG，免得生产的 INFO 日志被刷屏；非 200 记 INFO
        logger.log(logging.DEBUG if status == 200 else logging.INFO, '%s service=%r status=%s', path, service, status)
        self._send_json(status, body)

    def _send_json(self, status, body):
        self.send_response(status)
        if body is None:
            self.end_headers()
            return
        data = json.dumps(body).encode('utf-8')
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        return


def start_health_server():
    if not AGENT_LOCAL_SECRET:
        logger.warning('AGENT_LOCAL_SECRET 未配置：本机所有需要密钥的端点（/queryPlugin、/pluginSyncChanged、/verifyOpsCode、'
                       '/envConfig、/instanceSnapshot）将拒绝所有请求（403）。'
                       '如需 admin 等业务服务经本机 agent 调用它们，请在 .env 配置该值并同步进业务服务的配置'
                       '（拉插件为 admin 的 sync-plugins 配置）')
    server = ThreadingHTTPServer((HEALTH_HOST, HEALTH_PORT), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f'Health server listening on http://{HEALTH_HOST}:{HEALTH_PORT}/health')
    return server
