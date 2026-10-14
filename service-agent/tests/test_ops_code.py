import json
import logging
import threading
import time

import pytest

from core import ops_code
from core.capabilities import CAPABILITIES

FIELDS = {'service': 'done-admin', 'code': '135790', 'operation': 'backups:upload',
          'operator': {'userId': 1, 'name': '张三', 'ip': '172.18.0.1'}}


def _connect(features=('ops_code',)):
    sent = []
    ops_code.on_connected(sent.append)
    if features is not None:
        ops_code.set_features(list(features))
    return sent


def _verify_async(timeout=5.0):
    box = {}
    thread = threading.Thread(target=lambda: box.update(result=ops_code.verify(dict(FIELDS), timeout=timeout)))
    thread.start()
    return thread, box


def _wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_capability_is_declared():
    assert 'ops_code' in CAPABILITIES


def test_not_connected_is_unreachable():
    assert ops_code.verify(dict(FIELDS)) == (503, {'reason': 'unreachable'})


def test_negotiating_window_then_outdated_hub(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(ops_code, '_monotonic', lambda: now[0])
    _connect(features=None)
    assert ops_code.verify(dict(FIELDS)) == (503, {'reason': 'negotiating'})
    now[0] += ops_code.NEGOTIATE_SECONDS - 0.01
    assert ops_code.verify(dict(FIELDS)) == (503, {'reason': 'negotiating'})
    now[0] += 0.01
    assert ops_code.verify(dict(FIELDS)) == (501, None)   # 连上 5 秒仍无 hub_hello：旧中枢


@pytest.mark.parametrize('features', [['header_auth'], None, 'ops_code', {'ops_code': True}])
def test_hello_without_ops_code_is_outdated_hub(features):
    ops_code.on_connected(lambda frame: None)
    ops_code.set_features(features)
    assert ops_code.verify(dict(FIELDS)) == (501, None)


def test_round_trip_strips_envelope():
    sent = _connect()
    thread, box = _verify_async()
    assert _wait_for(lambda: sent)
    frame = sent[0]
    assert frame == {'type': 'ops_code_verify', 'requestId': frame['requestId'], **FIELDS}
    assert ops_code.resolve({'type': 'ops_code_verify_result', 'requestId': frame['requestId'], 'valid': True,
                             'namespace': {'code': 'prod', 'name': '生产'}}) is True
    thread.join(2)
    assert box['result'] == (200, {'valid': True, 'namespace': {'code': 'prod', 'name': '生产'}})
    assert ops_code._pending == {}


def test_late_result_after_timeout_is_dropped():
    sent = _connect()
    thread, box = _verify_async(timeout=0.5)
    assert _wait_for(lambda: sent)
    thread.join(2)
    assert box['result'] == (504, None)
    assert ops_code.resolve({'type': 'ops_code_verify_result', 'requestId': sent[0]['requestId'], 'valid': True}) is False
    assert ops_code._pending == {}


def test_resolve_ignores_unknown_or_malformed_ids():
    _connect()
    assert ops_code.resolve({'requestId': 'nope', 'valid': True}) is False
    assert ops_code.resolve({'requestId': None, 'valid': True}) is False
    assert ops_code.resolve({}) is False


def test_disconnect_wakes_every_pending_request_at_once():
    _connect()
    workers = [_verify_async(timeout=5) for _ in range(3)]
    assert _wait_for(lambda: len(ops_code._pending) == 3)
    started = time.monotonic()
    ops_code.on_disconnected()
    for thread, box in workers:
        thread.join(2)
        assert box['result'] == (503, {'reason': 'disconnected'})
    assert time.monotonic() - started < 1
    assert ops_code.verify(dict(FIELDS)) == (503, {'reason': 'unreachable'})


def test_reconnect_clears_features_and_restarts_negotiation():
    _connect()
    ops_code.on_disconnected()
    ops_code.set_features(['ops_code'])            # 断线后迟到的 hub_hello 不生效
    ops_code.on_connected(lambda frame: None)
    assert ops_code.verify(dict(FIELDS)) == (503, {'reason': 'negotiating'})


def test_result_from_previous_generation_is_dropped():
    sent = _connect()
    thread, box = _verify_async(timeout=5)
    assert _wait_for(lambda: sent)
    with ops_code._lock:                            # 模拟：结果到达前连接已换代，等待方还没来得及清理
        ops_code._generation += 1
    assert ops_code.resolve({'requestId': sent[0]['requestId'], 'valid': True}) is False
    ops_code.on_disconnected()
    thread.join(2)
    assert box['result'] == (503, {'reason': 'disconnected'})


def test_in_flight_cap_returns_busy():
    _connect()
    workers = [_verify_async(timeout=5) for _ in range(ops_code.MAX_IN_FLIGHT)]
    assert _wait_for(lambda: len(ops_code._pending) == ops_code.MAX_IN_FLIGHT)
    assert ops_code.verify(dict(FIELDS)) == (503, {'reason': 'busy'})
    ops_code.on_disconnected()
    for thread, _ in workers:
        thread.join(2)


def test_blocked_sender_keeps_threads_and_queue_bounded():
    release = threading.Event()
    calls = []

    def blocking_send(frame):
        calls.append(frame)
        release.wait(5)

    ops_code.on_connected(blocking_send)
    ops_code.set_features(['ops_code'])
    baseline = threading.active_count()
    try:
        results = [ops_code.verify(dict(FIELDS), timeout=0.05) for _ in range(30)]
        assert len(calls) == 1                                   # 只有一个发送线程卡在 send 上
        assert ops_code._queue.qsize() <= ops_code.SEND_QUEUE_SIZE
        assert threading.active_count() <= baseline + 1          # 至多多出这一个发送线程
        assert all(r in ((504, None), (503, {'reason': 'busy'})) for r in results)
        assert (503, {'reason': 'busy'}) in results              # 队列满后直接 busy，不再排队
    finally:
        release.set()
    assert _wait_for(lambda: ops_code._queue.qsize() == 0)
    assert len(calls) == 1                                       # 调用方都已放弃，排队的帧全部丢弃


def test_frames_queued_on_old_connection_are_not_sent_after_reconnect():
    release = threading.Event()
    old_calls = []

    def blocking_send(frame):
        old_calls.append(frame)
        release.wait(5)

    ops_code.on_connected(blocking_send)
    ops_code.set_features(['ops_code'])
    first = _verify_async(timeout=5)
    assert _wait_for(lambda: old_calls)                          # 发送线程卡在旧连接的 send 上
    queued = _verify_async(timeout=5)
    assert _wait_for(lambda: ops_code._queue.qsize() == 1)
    ops_code.on_disconnected()                                   # 断线：两个在途请求都立即返回
    for thread, box in (first, queued):
        thread.join(2)
        assert box['result'] == (503, {'reason': 'disconnected'})
    new_calls = _connect()
    fresh = _verify_async(timeout=5)
    release.set()                                                # 旧 send 返回：旧帧丢弃，新帧照常发出
    assert _wait_for(lambda: new_calls)
    assert len(old_calls) == 1 and len(new_calls) == 1
    assert new_calls[0]['requestId'] != old_calls[0]['requestId']
    assert ops_code.resolve({'requestId': new_calls[0]['requestId'], 'valid': True}) is True
    fresh[0].join(2)
    assert fresh[1]['result'] == (200, {'valid': True})


def test_send_one_drops_stale_old_or_abandoned_frames():
    calls = []
    ops_code.on_connected(calls.append)
    generation = ops_code._generation
    with ops_code._lock:
        ops_code._pending['live'] = {'event': threading.Event(), 'generation': generation, 'result': None}
    frame = {'type': 'ops_code_verify', 'requestId': 'live'}
    now = ops_code._monotonic()
    assert ops_code._send_one((now - ops_code.SEND_STALE_SECONDS - 1, generation, 'live', frame)) is False
    assert ops_code._send_one((now, generation - 1, 'live', frame)) is False
    assert ops_code._send_one((now, generation, 'gone', frame)) is False
    assert calls == []
    assert ops_code._send_one((now, generation, 'live', frame)) is True
    assert calls == [frame]


def test_send_failure_wakes_request_and_never_logs_code(caplog):
    def broken_send(frame):
        raise RuntimeError(json.dumps(frame))                    # 异常信息里带着整帧（含口令）

    caplog.set_level(logging.DEBUG)
    ops_code.on_connected(broken_send)
    ops_code.set_features(['ops_code'])
    assert ops_code.verify(dict(FIELDS), timeout=5) == (503, {'reason': 'disconnected'})
    assert FIELDS['code'] not in caplog.text
    assert 'RuntimeError' in caplog.text
