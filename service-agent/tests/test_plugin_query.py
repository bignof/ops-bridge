import importlib
import sys
import threading
import time


def _fresh():
    sys.modules.pop('core.plugin_query', None)
    return importlib.import_module('core.plugin_query')


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
    assert sent[0] == {'type': 'plugin_query', 'requestId': rid, 'service': 'done-admin'}
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
