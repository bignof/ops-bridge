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


def _make_qp_handler(module, path, connected, secret_header=None, request_return=None):
    module.get_connection_state = lambda: {'connected': connected}
    module.plugin_query.request = lambda service, timeout: request_return
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
    h, responses = _make_qp_handler(module, '/queryPlugin?service=x', True, secret_header='wrong')
    h.do_GET()
    assert responses == [401]


def test_query_plugin_503_when_disconnected(monkeypatch):
    monkeypatch.setenv('AGENT_LOCAL_SECRET', 'topsecret')
    module = _import_health_server(monkeypatch)
    h, responses = _make_qp_handler(module, '/queryPlugin?service=x', False, secret_header='topsecret')
    h.do_GET()
    assert responses == [503]


def test_query_plugin_504_on_timeout(monkeypatch):
    monkeypatch.setenv('AGENT_LOCAL_SECRET', 'topsecret')
    module = _import_health_server(monkeypatch)
    h, responses = _make_qp_handler(module, '/queryPlugin?service=x', True, secret_header='topsecret', request_return=None)
    h.do_GET()
    assert responses == [504]


def test_query_plugin_200_returns_bare_array(monkeypatch):
    monkeypatch.setenv('AGENT_LOCAL_SECRET', 'topsecret')
    module = _import_health_server(monkeypatch)
    items = [{'pluginName': 'p', 'version': '1', 'url': 'u'}]
    h, responses = _make_qp_handler(module, '/queryPlugin?service=x', True, secret_header='topsecret', request_return=items)
    h.do_GET()
    assert responses == [200]
    body = json.loads(h.wfile.getvalue().decode('utf-8'))
    assert isinstance(body, list)                 # 顶层是纯数组，不含 type/requestId 包装
    assert body == items


def test_query_plugin_403_when_secret_unconfigured(monkeypatch):
    # 评审 H1：secret 未配置必须 fail-closed（403），不得放行任意请求
    monkeypatch.delenv('AGENT_LOCAL_SECRET', raising=False)
    module = _import_health_server(monkeypatch)
    h, responses = _make_qp_handler(module, '/queryPlugin?service=x', True, secret_header=None)
    h.do_GET()
    assert responses == [403]


def test_query_plugin_400_when_service_missing(monkeypatch):
    # 评审 L3：缺 service 参数应 400（与 hubSync:queryPlugin 的必填校验对齐），不得静默 200 []
    monkeypatch.setenv('AGENT_LOCAL_SECRET', 'topsecret')
    module = _import_health_server(monkeypatch)
    h, responses = _make_qp_handler(module, '/queryPlugin', True, secret_header='topsecret')
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
    do_GET 之后看 status（回出的状态码）、sent_headers、json()（响应体，没有响应体为 None）。"""
    def make(method, path, *, secret='s', headers=None):
        module = _import_health_server(monkeypatch)
        monkeypatch.setattr(module, 'AGENT_LOCAL_SECRET', secret)
        handler = module._HealthHandler.__new__(module._HealthHandler)
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
    monkeypatch.setattr(hub_rpc, 'call', lambda t, f, feat, timeout: seen.append((t, f, feat, timeout)) or (200, {'ok': True}))
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
    hub_rpc.on_connected(lambda message: pytest.fail('中枢没声明这个特性，不该发出帧'))
    hub_rpc.set_features(['header_auth', 'ops_code'])
    h = handler_for('GET', f'{path}?service=k3f9qm')
    h.do_GET()
    assert (h.status, h.json()) == (501, {'reason': 'hub_unsupported'})


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
    assert {'env_config', 'instance_snapshot'} <= set(CAPABILITIES)
