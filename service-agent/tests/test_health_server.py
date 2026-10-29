import importlib
import io
import json
import logging
import sys
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from core import hub_rpc
from core.capabilities import CAPABILITIES


def _import_health_server(monkeypatch: pytest.MonkeyPatch):
    for key, value in {
        "WS_URL": "ws://localhost:8080/ws/agent",
        "AGENT_KEY": "token",
        "AGENT_ID": "agent-health",
        "HEALTH_HOST": "127.0.0.1",
        "HEALTH_PORT": "18081",
    }.items():
        monkeypatch.setenv(key, value)
    for name in ["config", "core.ws_client", "core.health_server"]:
        sys.modules.pop(name, None)
    return importlib.import_module("core.health_server")


def _make_handler(module, path: str, state: dict, execution_state: dict | None = None):
    module.get_connection_state = lambda: state
    module.get_command_execution_state = lambda: execution_state or {"activeCommands": 0, "queuedCommands": 0, "projects": []}
    handler = module._HealthHandler.__new__(module._HealthHandler)
    handler.path = path
    handler.wfile = io.BytesIO()
    responses: list[int] = []
    headers: list[tuple[str, str]] = []
    handler.send_response = lambda code: responses.append(code)
    handler.send_header = lambda key, value: headers.append((key, value))
    handler.end_headers = lambda: None
    return handler, responses, headers


def test_health_handler_returns_404_for_unknown_path(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _import_health_server(monkeypatch)
    handler, responses, headers = _make_handler(module, "/nope", {})

    handler.do_GET()

    assert responses == [404]
    assert headers == []


def test_health_handler_returns_degraded_and_ok_payloads(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _import_health_server(monkeypatch)

    degraded, responses, headers = _make_handler(module, "/health", {"connected": False, "last_error": "boom"})
    degraded.do_GET()
    degraded_payload = json.loads(degraded.wfile.getvalue().decode("utf-8"))

    assert responses == [503]
    assert ("Content-Type", "application/json") in headers
    assert degraded_payload["status"] == "degraded"
    assert degraded_payload["agentId"] == "agent-health"
    assert degraded_payload["lastError"] == "boom"

    healthy_state = {
        "connected": True,
        "last_connect_ts": 1,
        "last_disconnect_ts": 2,
        "last_heartbeat_ts": 3,
        "last_message_ts": 4,
        "last_error": None,
    }
    healthy, responses, headers = _make_handler(module, "/health", healthy_state)
    healthy.do_GET()
    healthy_payload = json.loads(healthy.wfile.getvalue().decode("utf-8"))

    assert responses == [200]
    assert healthy_payload["status"] == "ok"
    assert healthy_payload["connected"] is True
    assert healthy_payload["lastConnectTs"] == "1970-01-01T08:00:01+08:00"
    assert healthy_payload["lastDisconnectTs"] == "1970-01-01T08:00:02+08:00"
    assert healthy_payload["lastHeartbeatTs"] == "1970-01-01T08:00:03+08:00"
    assert healthy_payload["lastMessageTs"] == "1970-01-01T08:00:04+08:00"
    assert healthy_payload["commandExecution"] == {"activeCommands": 0, "queuedCommands": 0, "projects": []}
    assert module._HealthHandler.log_message(healthy, "%s") is None


def test_health_handler_includes_project_lock_execution_state(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _import_health_server(monkeypatch)
    handler, responses, _ = _make_handler(
        module,
        "/health",
        {"connected": True},
        {
            "activeCommands": 1,
            "queuedCommands": 2,
            "projects": [
                {
                    "projectDir": "/srv/app",
                    "activeRequestId": "req-1",
                    "activeAction": "update",
                    "activeSinceTs": 3,
                    "queuedCount": 2,
                }
            ],
        },
    )
    handler.do_GET()
    payload = json.loads(handler.wfile.getvalue().decode("utf-8"))

    assert responses == [200]
    assert payload["commandExecution"]["activeCommands"] == 1
    assert payload["commandExecution"]["queuedCommands"] == 2
    assert payload["commandExecution"]["projects"] == [
        {
            "projectDir": "/srv/app",
            "activeRequestId": "req-1",
            "activeAction": "update",
            "activeSinceTs": "1970-01-01T08:00:03+08:00",
            "queuedCount": 2,
        }
    ]


def test_start_health_server_creates_background_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _import_health_server(monkeypatch)
    server_calls: list[tuple[tuple[str, int], object]] = []
    thread_calls: list[tuple[object, bool]] = []

    class FakeServer:
        def __init__(self, address, handler):
            server_calls.append((address, handler))

        def serve_forever(self) -> None:
            return None

    class FakeThread:
        def __init__(self, target, daemon):
            self.target = target
            thread_calls.append((target, daemon))

        def start(self) -> None:
            self.target()

    monkeypatch.setattr(module, "ThreadingHTTPServer", FakeServer)
    monkeypatch.setattr(module.threading, "Thread", FakeThread)

    server = module.start_health_server()

    assert isinstance(server, FakeServer)
    assert server_calls == [(("127.0.0.1", 18081), module._HealthHandler)]
    assert thread_calls[0][1] is True


def _make_qp_handler(monkeypatch, module, path, connected, secret_header=None, request_return=None):
    # 用 monkeypatch 换：core.plugin_query 是跨用例共享的模块，直接赋值会让替身留到后面的用例，
    # 用真实 plugin_query.request 的用例就会拿到这个替身的返回值
    monkeypatch.setattr(module, 'get_connection_state', lambda: {'connected': connected})
    monkeypatch.setattr(module.plugin_query, 'request', lambda service, timeout, with_env=False: request_return)
    handler = module._HealthHandler.__new__(module._HealthHandler)
    handler.path = path
    handler.headers = {'X-Agent-Secret': secret_header} if secret_header is not None else {}
    handler.wfile = io.BytesIO()
    responses, headers = [], []
    handler.send_response = lambda code: responses.append(code)
    handler.send_header = lambda k, v: headers.append((k, v))
    handler.end_headers = lambda: None
    return handler, responses


def test_query_plugin_rejects_wrong_secret(monkeypatch):
    monkeypatch.setenv('AGENT_LOCAL_SECRET', 'topsecret')
    module = _import_health_server(monkeypatch)
    h, responses = _make_qp_handler(monkeypatch, module, '/queryPlugin?service=x', True, secret_header='wrong')
    h.do_GET()
    assert responses == [401]


def test_query_plugin_503_when_disconnected(monkeypatch):
    monkeypatch.setenv('AGENT_LOCAL_SECRET', 'topsecret')
    module = _import_health_server(monkeypatch)
    h, responses = _make_qp_handler(monkeypatch, module, '/queryPlugin?service=x', False, secret_header='topsecret')
    h.do_GET()
    assert responses == [503]


def test_query_plugin_504_on_timeout(monkeypatch):
    monkeypatch.setenv('AGENT_LOCAL_SECRET', 'topsecret')
    module = _import_health_server(monkeypatch)
    h, responses = _make_qp_handler(monkeypatch, module, '/queryPlugin?service=x', True, secret_header='topsecret', request_return=None)
    h.do_GET()
    assert responses == [504]


def test_query_plugin_200_returns_bare_array(monkeypatch):
    monkeypatch.setenv('AGENT_LOCAL_SECRET', 'topsecret')
    module = _import_health_server(monkeypatch)
    items = [{'pluginName': 'p', 'version': '1', 'url': 'u'}]
    h, responses = _make_qp_handler(monkeypatch, module, '/queryPlugin?service=x', True, secret_header='topsecret', request_return=items)
    h.do_GET()
    assert responses == [200]
    body = json.loads(h.wfile.getvalue().decode('utf-8'))
    assert isinstance(body, list)                 # 顶层是纯数组，不含 type/requestId 包装
    assert body == items


def test_query_plugin_403_when_secret_unconfigured(monkeypatch):
    # 评审 H1：secret 未配置必须 fail-closed（403），不得放行任意请求
    monkeypatch.delenv('AGENT_LOCAL_SECRET', raising=False)
    module = _import_health_server(monkeypatch)
    h, responses = _make_qp_handler(monkeypatch, module, '/queryPlugin?service=x', True, secret_header=None)
    h.do_GET()
    assert responses == [403]


def test_query_plugin_400_when_service_missing(monkeypatch):
    # 评审 L3：缺 service 参数应 400（与 hubSync:queryPlugin 的必填校验对齐），不得静默 200 []
    monkeypatch.setenv('AGENT_LOCAL_SECRET', 'topsecret')
    module = _import_health_server(monkeypatch)
    h, responses = _make_qp_handler(monkeypatch, module, '/queryPlugin', True, secret_header='topsecret')
    h.do_GET()
    assert responses == [400]


# ---- /envConfig、/instanceSnapshot：应用经本机 agent 向中枢查环境开关、实例快照，agent 只转发 hub_rpc.call ----

HUB_ROUTES = [
    # 路径, 请求帧, 中枢须在 hub_hello.features 声明的特性, 传给 call 的总时限（秒）
    ('/envConfig', 'env_config_query', 'env_config', 8.0),
    ('/instanceSnapshot', 'instance_snapshot_query', 'instance_snapshot', 10.0),
]
HUB_PATHS = [route[0] for route in HUB_ROUTES]


@pytest.fixture
def handler_for(monkeypatch):
    """造一个不起服务的 _HealthHandler（同 _make_handler 的 __new__ 写法）。
    secret 是 agent 配置的 AGENT_LOCAL_SECRET（'' = 未配置）；headers 默认带对的 X-Agent-Secret，传 {} 就是不带。
    do_GET 之后看 status（回出的状态码）、sent_headers、json()（响应体，没有响应体为 None）；
    module 是这个 handler 用的 core.health_server 模块，用例要替换它里面的 plugin_query、get_connection_state 时用。"""
    def make(method, path, *, secret='s', headers=None):
        module = _import_health_server(monkeypatch)
        monkeypatch.setattr(module, 'AGENT_LOCAL_SECRET', secret)
        handler = module._HealthHandler.__new__(module._HealthHandler)
        handler.module = module
        handler.command, handler.path = method, path
        handler.headers = {'X-Agent-Secret': secret} if headers is None else headers
        handler.wfile = io.BytesIO()
        handler.status, handler.sent_headers = None, []

        def send_response(code):
            assert handler.status is None, '同一个请求回了两次状态码'
            handler.status = code

        def body():
            raw = handler.wfile.getvalue()
            return json.loads(raw.decode('utf-8')) if raw else None

        handler.send_response = send_response
        handler.send_header = lambda key, value: handler.sent_headers.append((key, value))
        handler.end_headers = lambda: None
        handler.json = body
        return handler
    return make


@pytest.mark.parametrize('path,frame,feature,timeout', HUB_ROUTES)
def test_forwards_to_hub_rpc(handler_for, monkeypatch, path, frame, feature, timeout):
    seen = []
    monkeypatch.setattr(hub_rpc, 'call', lambda t, f, feat, actual_timeout: seen.append((t, f, feat, actual_timeout)) or (200, {'ok': True}))
    h = handler_for('GET', f'{path}?service=k3f9qm', secret='s')
    h.do_GET()
    assert (h.status, h.json(), seen) == (200, {'ok': True}, [(frame, {'service': 'k3f9qm'}, feature, timeout)])
    assert ('Content-Type', 'application/json') in h.sent_headers


@pytest.mark.parametrize('path', HUB_PATHS)
@pytest.mark.parametrize('secret,headers,status', [
    ('', None, 403),                                  # 没配置密钥：fail-closed
    ('', {'X-Agent-Secret': 'anything'}, 403),        # 没配置时请求带了什么都不放行
    ('s', {'X-Agent-Secret': 'nope'}, 401),
    ('s', {'X-Agent-Secret': ''}, 401),
    ('s', {}, 401),                                   # 没带请求头
], ids=['unconfigured', 'unconfigured-with-header', 'wrong', 'empty-header', 'no-header'])
def test_requires_secret(handler_for, monkeypatch, path, secret, headers, status):
    calls = []
    monkeypatch.setattr(hub_rpc, 'call', lambda *args: calls.append(args))
    h = handler_for('GET', f'{path}?service=k3f9qm', secret=secret, headers=headers)
    h.do_GET()
    # 响应体必须是 None：agent 自己拒绝的 401 / 403 不带响应体，应用侧靠「没有 reason」把它和中枢经 agent 回来的错误区分开
    assert (h.status, h.json(), calls) == (status, None, [])


@pytest.mark.parametrize('path', HUB_PATHS)
@pytest.mark.parametrize('query', [
    '', '?', '?service=', '?other=1',
    '?service=' + 'x' * 256, '?service=' + quote('备' * 256),
], ids=['no-query', 'bare-question-mark', 'empty-service', 'other-param', '256-ascii', '256-cjk'])
def test_requires_service(handler_for, monkeypatch, path, query):
    calls = []
    monkeypatch.setattr(hub_rpc, 'call', lambda *args: calls.append(args))
    h = handler_for('GET', path + query)
    h.do_GET()
    # 响应体必须是 None：agent 自己拒绝的 400 不带响应体，应用侧靠「没有 reason」区分
    assert (h.status, h.json(), calls) == (400, None, [])


@pytest.mark.parametrize('service', ['x' * 255, '备' * 255], ids=['255-ascii', '255-cjk'])
def test_service_of_255_characters_is_forwarded(handler_for, monkeypatch, service):
    # 长度按字符计，不按字节：255 个汉字（765 字节）也放行
    calls = []
    monkeypatch.setattr(hub_rpc, 'call', lambda t, f, feat, timeout: calls.append(f) or (200, {}))
    h = handler_for('GET', '/envConfig?service=' + quote(service))
    h.do_GET()
    assert (h.status, calls) == (200, [{'service': service}])


@pytest.mark.parametrize('path', [
    '/envConfigs', '/envConfig/', '/envConfig/x', '/envconfig', '/envConfig;x',
    '/instanceSnapshot2', '/instanceSnapshot/', '/instance', '/Snapshot',
])
def test_path_must_match_exactly(handler_for, monkeypatch, path):
    calls = []
    monkeypatch.setattr(hub_rpc, 'call', lambda *args: calls.append(args))
    h = handler_for('GET', path + '?service=k3f9qm')
    h.do_GET()
    # 响应体必须是 None：agent 自己回的 404（路径不认，旧版 agent 也是这样）不带响应体；
    # 中枢经 agent 回来的 404 带 {"reason": "not_found"}，应用侧靠有没有 reason 区分
    assert (h.status, h.json(), calls) == (404, None, [])


@pytest.mark.parametrize('result', [
    (403, {'reason': 'not_bound'}), (404, {'reason': 'not_found'}), (501, {'reason': 'hub_unsupported'}),
    (502, {'reason': 'unavailable'}), (503, {'reason': 'hub_disconnected'}), (503, {'reason': 'busy'}),
    (504, {'reason': 'timeout'}),
], ids=lambda result: f"{result[0]}-{result[1]['reason']}")
def test_passes_error_status_through(handler_for, monkeypatch, result):
    monkeypatch.setattr(hub_rpc, 'call', lambda t, f, feat, timeout: result)
    h = handler_for('GET', '/envConfig?service=k3f9qm')
    h.do_GET()
    assert (h.status, h.json()) == result


@pytest.mark.parametrize('path', HUB_PATHS)
def test_hub_not_connected_is_503(handler_for, path):
    # 不替换 hub_rpc.call：没连上中枢时真实的 call 直接回 503，不等
    h = handler_for('GET', f'{path}?service=k3f9qm')
    h.do_GET()
    assert (h.status, h.json()) == (503, {'reason': 'hub_disconnected'})


@pytest.mark.parametrize('path', HUB_PATHS)
def test_hub_without_the_feature_is_501(handler_for, path):
    sent = []
    # 发送函数在通道的发送线程里调用，线程里 pytest.fail 抛的异常不会让测试失败：记下来，最后断言没发出过帧
    hub_rpc.on_connected(sent.append)
    hub_rpc.set_features(['header_auth', 'ops_code'])
    h = handler_for('GET', f'{path}?service=k3f9qm')
    h.do_GET()
    assert (h.status, h.json(), sent) == (501, {'reason': 'hub_unsupported'}, [])    # 中枢没声明这个特性，不该发出帧


@pytest.mark.parametrize('path,frame,feature,body_key,payload', [
    ('/envConfig', 'env_config_query', 'env_config', 'env', {'allowUi': False, 'allowDs': True}),
    ('/instanceSnapshot', 'instance_snapshot_query', 'instance_snapshot', 'snapshot',
     {'schemaVersion': 1, 'instances': []}),
])
def test_end_to_end_through_hub_rpc(handler_for, path, frame, feature, body_key, payload):
    # 不替换 hub_rpc.call：中枢声明了特性，发出的请求帧原样到达，结果帧回来后折成 200 和响应体
    sent = []

    def hub(message):
        sent.append(message)
        hub_rpc.resolve({'type': frame.replace('_query', '_result'), 'requestId': message['requestId'], body_key: payload})

    hub_rpc.on_connected(hub)
    hub_rpc.set_features(['header_auth', feature])
    h = handler_for('GET', f'{path}?service=k3f9qm')
    h.do_GET()
    assert (h.status, h.json()) == (200, payload)
    assert [{key: value for key, value in message.items() if key != 'requestId'} for message in sent] == [
        {'type': frame, 'service': 'k3f9qm'}]


def test_timeouts_exceed_the_negotiation_window(monkeypatch):
    # 总时限含等 hub_hello 协商的时间：不比协商窗口长，旧版中枢（始终没有 hub_hello）会被判成 504 超时，而不是 501
    module = _import_health_server(monkeypatch)
    assert module.ENV_CONFIG_TIMEOUT > hub_rpc.NEGOTIATE_SECONDS
    assert module.SNAPSHOT_TIMEOUT > hub_rpc.NEGOTIATE_SECONDS


def test_log_has_only_path_service_and_status(handler_for, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(hub_rpc, 'call', lambda t, f, feat, timeout: (200, {'note': 'snapshot-content-marker'}))
    handler_for('GET', '/instanceSnapshot?service=k3f9qm', secret='s3cret').do_GET()
    handler_for('GET', '/envConfig?service=k3f9qm', secret='s3cret', headers={'X-Agent-Secret': 'nope-guess'}).do_GET()
    handler_for('GET', '/envConfig?service=k3f9qm', secret='', headers={'X-Agent-Secret': 'nope-guess'}).do_GET()
    lines = [record.getMessage() for record in caplog.records if record.name == 'core.health_server']
    assert "/instanceSnapshot service='k3f9qm' status=200" in lines
    for text in ('s3cret', 'nope-guess', 'snapshot-content-marker'):
        assert text not in caplog.text


def test_capabilities_include_new_names():
    assert {'plugin_query_env', 'env_config', 'instance_snapshot'} <= set(CAPABILITIES)


# ---- /queryPlugin?withEnv=1：拉插件顺带带回环境开关，并区分「服务器没绑定到应用所在的环境」 ----

PLUGINS = [{'pluginName': 'a'}]
ENV = {'allowUi': True, 'allowDs': False}


def _serve_query_plugin(h, monkeypatch, request):
    """让 h（GET /queryPlugin…）在 agent 已连上中枢、plugin_query.request 换成 request 的情况下处理一次请求。"""
    monkeypatch.setattr(h.module, 'get_connection_state', lambda: {'connected': True})
    monkeypatch.setattr(h.module.plugin_query, 'request', request)
    h.do_GET()


def test_not_bound_maps_to_403(handler_for, monkeypatch):
    h = handler_for('GET', '/queryPlugin?service=k3f9qm')
    error = h.module.plugin_query.QueryError('not_bound')
    _serve_query_plugin(h, monkeypatch, lambda service, timeout, with_env=False: error)
    # 带 reason：应用据此把「这台服务器没绑定到应用所在的环境」和 agent 自己拒绝密钥的 403（没有响应体）区分开
    assert (h.status, h.json()) == (403, {'reason': 'not_bound'})
    assert ('Content-Type', 'application/json') in h.sent_headers


@pytest.mark.parametrize('query,expected', [
    ('', False), ('&withEnv=1', True), ('&withEnv=true', True),
    ('&withEnv=0', False), ('&withEnv=false', False), ('&withEnv=', False), ('&withEnv=yes', False),
], ids=['absent', '1', 'true', '0', 'false', 'empty', 'yes'])
def test_query_plugin_reads_with_env_param(handler_for, monkeypatch, query, expected):
    seen = []
    h = handler_for('GET', '/queryPlugin?service=k3f9qm' + query)
    _serve_query_plugin(h, monkeypatch, lambda service, timeout, with_env=False: seen.append((service, with_env)) or [])
    assert (h.status, seen) == (200, [('k3f9qm', expected)])


def test_query_plugin_with_env_param_returns_object(handler_for, monkeypatch):
    reply = {'plugins': PLUGINS, 'env': ENV}
    h = handler_for('GET', '/queryPlugin?service=k3f9qm&withEnv=1')
    _serve_query_plugin(h, monkeypatch, lambda service, timeout, with_env=False: reply)
    assert (h.status, h.json()) == (200, reply)
    assert ('Content-Type', 'application/json') in h.sent_headers


@pytest.mark.parametrize('query,hub_env,expected', [
    ('', ENV, PLUGINS),                                             # 老应用不带 withEnv：哪怕中枢回了 env，响应也仍是纯数组
    ('&withEnv=1', ENV, {'plugins': PLUGINS, 'env': ENV}),
    ('&withEnv=1', None, PLUGINS),                                  # 旧中枢不回 env：应用要了也仍是纯数组
], ids=['old-app', 'new-app', 'old-hub'])
def test_query_plugin_end_to_end_through_plugin_query(handler_for, monkeypatch, query, hub_env, expected):
    # 不替换 plugin_query.request：请求帧原样发给中枢替身，结果帧回来后折成响应
    h = handler_for('GET', '/queryPlugin?service=k3f9qm' + query)
    monkeypatch.setattr(h.module, 'get_connection_state', lambda: {'connected': True})
    pq, frames = h.module.plugin_query, []

    def hub(frame):    # 像中枢那样回 plugin_query_result：不管请求带不带 withEnv，手里有 env 就回
        frames.append(frame)
        pq.resolve(frame['requestId'], PLUGINS, None, hub_env)

    monkeypatch.setattr(pq, '_sender', hub)
    h.do_GET()
    assert (h.status, h.json()) == (200, expected)
    assert [('withEnv' in frame) for frame in frames] == [bool(query)]    # 帧带 withEnv 当且仅当应用要了


# ---- 本机鉴权：四个需要密钥的端点共用 _check_local_secret（/pluginSyncChanged 写法不同，见 test_plugin_reporting.py）----

# 方法, 请求路径, 未配置密钥时 warning 里的端点名（/queryPlugin、/verifyOpsCode 不带斜杠，新端点是路径，原样保留）
SECRET_ENDPOINTS = [
    ('GET', '/queryPlugin?service=k3f9qm', 'queryPlugin'),
    ('POST', '/verifyOpsCode', 'verifyOpsCode'),
    ('GET', '/envConfig?service=k3f9qm', '/envConfig'),
    ('GET', '/instanceSnapshot?service=k3f9qm', '/instanceSnapshot'),
]
SECRET_ENDPOINT_IDS = ['queryPlugin', 'verifyOpsCode', 'envConfig', 'instanceSnapshot']
SECRET_REQUESTS = [(method, path) for method, path, _ in SECRET_ENDPOINTS]    # 不关心 warning 端点名的用例用


def _serve(h):
    """按请求方法跑一次 do_GET / do_POST。"""
    getattr(h, 'do_' + h.command)()


def _warnings(caplog):
    return [record.getMessage() for record in caplog.records
            if record.name == 'core.health_server' and record.levelno >= logging.WARNING]


@pytest.mark.parametrize('method,path,label', SECRET_ENDPOINTS, ids=SECRET_ENDPOINT_IDS)
def test_unconfigured_secret_is_403_with_a_warning_naming_only_the_endpoint(handler_for, caplog, method, path, label):
    caplog.set_level(logging.DEBUG)
    h = handler_for(method, path, secret='', headers={'X-Agent-Secret': 'guess-123'})
    _serve(h)
    # 响应体必须是 None：agent 自己拒绝的 403 不带响应体，应用侧靠「没有 reason」把它和 not_bound 的 403 区分开
    assert (h.status, h.json()) == (403, None)
    assert _warnings(caplog) == [f'{label} 拒绝请求：AGENT_LOCAL_SECRET 未配置']
    assert 'guess-123' not in caplog.text


@pytest.mark.parametrize('method,path', SECRET_REQUESTS, ids=SECRET_ENDPOINT_IDS)
@pytest.mark.parametrize('supplied', [None, '', 'nope', 's3cre', 's3cret!', '密钥'],
                         ids=['no-header', 'empty', 'wrong', 'prefix', 'longer', 'non-ascii'])
def test_wrong_secret_is_401_without_body_or_warning(handler_for, caplog, method, path, supplied):
    caplog.set_level(logging.DEBUG)
    h = handler_for(method, path, secret='s3cret', headers={} if supplied is None else {'X-Agent-Secret': supplied})
    _serve(h)
    assert (h.status, h.json()) == (401, None)    # agent 自己拒绝的 401 不带响应体
    assert _warnings(caplog) == []
    assert 's3cret' not in caplog.text


@pytest.mark.parametrize('method,path', SECRET_REQUESTS, ids=SECRET_ENDPOINT_IDS)
def test_secret_is_compared_in_constant_time(handler_for, monkeypatch, method, path):
    h = handler_for(method, path, secret='s3cret', headers={'X-Agent-Secret': 'guess'})
    seen = []
    monkeypatch.setattr(h.module.hmac, 'compare_digest', lambda a, b: seen.append((a, b)) or False)
    _serve(h)
    assert (h.status, seen) == (401, [(b'guess', b's3cret')])


def test_check_local_secret_passes_silently_when_the_secret_matches(handler_for):
    h = handler_for('GET', '/envConfig?service=k3f9qm', secret='s3cret', headers={'X-Agent-Secret': 's3cret'})
    assert h._check_local_secret('/envConfig') is True
    assert (h.status, h.json()) == (None, None)    # 通过时不回任何响应，由调用方接着处理


@pytest.mark.parametrize('secret,headers,status', [
    ('', {}, 403), ('', {'X-Agent-Secret': ''}, 403), ('s3cret', {}, 401), ('s3cret', {'X-Agent-Secret': 'x'}, 401),
], ids=['unconfigured', 'unconfigured-empty-header', 'no-header', 'wrong'])
def test_check_local_secret_replies_and_returns_false_when_refused(handler_for, secret, headers, status):
    h = handler_for('GET', '/envConfig?service=k3f9qm', secret=secret, headers=headers)
    assert h._check_local_secret('/envConfig') is False
    # 响应体必须是 None：agent 自己拒绝的 401 / 403 不带响应体，应用侧靠「没有 reason」把它和中枢经 agent 回来的错误区分开
    assert (h.status, h.json()) == (status, None)


def test_start_warns_that_every_secret_endpoint_refuses_when_unconfigured(monkeypatch, caplog):
    module = _import_health_server(monkeypatch)
    monkeypatch.setattr(module, 'AGENT_LOCAL_SECRET', '')
    monkeypatch.setattr(module, 'ThreadingHTTPServer', lambda address, handler: SimpleNamespace(serve_forever=lambda: None))
    monkeypatch.setattr(module, 'threading', SimpleNamespace(Thread=lambda target, daemon: SimpleNamespace(start=lambda: None)))
    caplog.set_level(logging.DEBUG)
    module.start_health_server()
    warnings = _warnings(caplog)
    assert len(warnings) == 1 and 'AGENT_LOCAL_SECRET' in warnings[0] and '403' in warnings[0]
    for path in ('/queryPlugin', '/pluginSyncChanged', '/verifyOpsCode', '/envConfig', '/instanceSnapshot'):
        assert path in warnings[0]
