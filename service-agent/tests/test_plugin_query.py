import importlib
import sys
import threading
import time

import pytest


def _fresh():
    sys.modules.pop('core.plugin_query', None)
    return importlib.import_module('core.plugin_query')


class FakeSender:
    """替身发送函数：记下发出的 plugin_query 帧，并像中枢那样立刻回 plugin_query_result（plugins、error、env 取自 reply）。"""

    def __init__(self, module):
        self.module = module
        self.frames = []
        self.reply = {}

    def __call__(self, frame):
        self.frames.append(frame)
        reply = self.reply
        self.module.resolve(frame['requestId'], reply.get('plugins', []), reply.get('error'), reply.get('env'))


@pytest.fixture
def fake_sender():
    return FakeSender(_fresh())


def plugin_query_round_trip(fake_sender, with_env, reply):
    """让 request 发一次查询、由替身按 reply 回结果帧，返回 request 的返回值。"""
    fake_sender.reply = reply
    fake_sender.module.set_sender(fake_sender)
    return fake_sender.module.request('svc', 2, with_env=with_env)


def test_request_returns_none_when_no_sender():
    pq = _fresh()
    assert pq.request('svc', timeout=0.1) is None


def test_request_resolve_roundtrip_and_cleanup():
    pq = _fresh()
    sent = []
    pq.set_sender(lambda m: sent.append(m))
    box = {}

    def caller():
        box['r'] = pq.request('done-admin', timeout=2)

    t = threading.Thread(target=caller)
    t.start()
    for _ in range(200):          # 等 request 把消息发出来
        if sent:
            break
        time.sleep(0.01)
    rid = sent[0]['requestId']
    assert sent[0] == {'type': 'plugin_query', 'requestId': rid, 'service': 'done-admin', 'manifestVersion': 2}
    pq.resolve(rid, [{'pluginName': 'p', 'version': '1', 'url': 'u'}])
    t.join(timeout=2)
    assert box['r'] == [{'pluginName': 'p', 'version': '1', 'url': 'u'}]
    assert pq._pending == {}       # 用完清空（防泄漏）


def test_request_times_out_and_cleans_up():
    pq = _fresh()
    pq.set_sender(lambda m: None)
    assert pq.request('svc', timeout=0.2) is None
    assert pq._pending == {}


def test_resolve_unknown_or_late_is_noop():
    pq = _fresh()
    pq.resolve('nonexistent', [{'x': 1}])    # 无条目，不抛
    pq.set_sender(lambda m: None)
    pq.request('svc', timeout=0.1)            # 超时后 pop
    pq.resolve('any', [{'x': 1}])            # 迟到 resolve，no-op 不抛


def test_request_returns_none_when_sender_raises():
    pq = _fresh()
    def boom(_m):
        raise RuntimeError('ws closed')
    pq.set_sender(boom)
    assert pq.request('svc', timeout=1) is None
    assert pq._pending == {}


def test_request_timeout_not_blocked_by_slow_sender():
    # 评审 M2：裸 ws.send 无 socket 超时，半开连接可阻塞远超 timeout——send 不得占用调用方预算
    pq = _fresh()
    pq.set_sender(lambda m: time.sleep(5))
    start = time.monotonic()
    assert pq.request('svc', timeout=0.3) is None
    assert time.monotonic() - start < 2      # 不被慢 send 拖住
    assert pq._pending == {}


# ---- withEnv：应用要求时，拉插件顺带带回环境的两个开关 ----

def test_with_env_adds_flag_and_returns_env(fake_sender):
    reply = {'plugins': [{'pluginName': 'a'}], 'env': {'allowUi': True, 'allowDs': False}}
    assert plugin_query_round_trip(fake_sender, with_env=True, reply=reply) == reply
    assert fake_sender.frames[0]['withEnv'] is True
    assert fake_sender.module._pending == {}    # 用完清空（防泄漏）


def test_without_env_keeps_plain_list(fake_sender):
    reply = {'plugins': [{'pluginName': 'a'}], 'env': {'allowUi': True, 'allowDs': False}}
    assert plugin_query_round_trip(fake_sender, with_env=False, reply=reply) == [{'pluginName': 'a'}]
    assert 'withEnv' not in fake_sender.frames[0]


def test_old_hub_without_env_returns_list_even_if_asked(fake_sender):
    assert plugin_query_round_trip(fake_sender, with_env=True, reply={'plugins': []}) == []


def test_env_is_returned_even_when_there_are_no_plugins(fake_sender):
    # 应用要的是环境开关：环境里一个插件都没发布时，也得拿到 env
    reply = {'plugins': [], 'env': {'allowUi': False, 'allowDs': False}}
    assert plugin_query_round_trip(fake_sender, with_env=True, reply=reply) == reply


@pytest.mark.parametrize('env', [
    None, 'on', [], [True, True], {}, {'allowUi': True}, {'allowDs': False},
    # 两个字段各自单独不合规（另一个是合规的布尔）：整数、字符串、null 都不算布尔
    {'allowUi': 1, 'allowDs': True}, {'allowUi': True, 'allowDs': 0},
    {'allowUi': 'true', 'allowDs': True}, {'allowUi': True, 'allowDs': 'false'},
    {'allowUi': None, 'allowDs': True}, {'allowUi': True, 'allowDs': None},
], ids=['none', 'string', 'empty-list', 'list-of-bool', 'empty-dict', 'only-ui', 'only-ds',
        'int-ui', 'int-ds', 'string-ui', 'string-ds', 'null-ui', 'null-ds'])
def test_malformed_env_is_ignored_even_if_asked(fake_sender, env):
    # env 不是含两个布尔的对象：当作中枢没回，仍是纯数组（应用侧据此当「环境开关未知」）
    reply = {'plugins': [{'pluginName': 'a'}], 'env': env}
    assert plugin_query_round_trip(fake_sender, with_env=True, reply=reply) == [{'pluginName': 'a'}]


def test_hub_rejection_wins_over_env(fake_sender):
    reply = {'plugins': [], 'error': 'not_bound', 'env': {'allowUi': True, 'allowDs': True}}
    result = plugin_query_round_trip(fake_sender, with_env=True, reply=reply)
    assert isinstance(result, fake_sender.module.QueryError) and result.reason == 'not_bound'


def test_on_close_clears_plugin_query_sender(monkeypatch):
    # 评审 M1：_on_close 必须与 _on_open 对称，同时清 outbox 与 plugin_query 的 sender
    import importlib
    import sys
    for name in ['config', 'core.plugin_query', 'core.ws_client']:
        sys.modules.pop(name, None)
    monkeypatch.setenv('WS_URL', 'ws://localhost:8080/ws/agent')
    monkeypatch.setenv('AGENT_KEY', 'k')
    pq = importlib.import_module('core.plugin_query')
    wsc = importlib.import_module('core.ws_client')
    pq.set_sender(lambda m: None)
    wsc._on_close(None, 1000, 'bye')
    assert pq._sender is None


def test_on_message_routes_plugin_query_result_to_resolve(monkeypatch):
    import importlib
    import sys
    for name in ['config', 'core.plugin_query', 'core.ws_client']:
        sys.modules.pop(name, None)
    monkeypatch.setenv('WS_URL', 'ws://localhost:8080/ws/agent')
    monkeypatch.setenv('AGENT_KEY', 'k')
    pq = importlib.import_module('core.plugin_query')
    wsc = importlib.import_module('core.ws_client')
    captured = {}
    monkeypatch.setattr(pq, 'resolve', lambda rid, plugins, error=None, env=None: captured.update(rid=rid, plugins=plugins, env=env))
    import json
    wsc._on_message(None, json.dumps({'type': 'plugin_query_result', 'requestId': 'r9', 'plugins': [{'a': 1}]}))
    assert captured == {'rid': 'r9', 'plugins': [{'a': 1}], 'env': None}    # 旧中枢的结果帧没有 env
