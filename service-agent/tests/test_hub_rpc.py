import json
import logging
import threading
import time

import pytest

from core import hub_rpc, ops_code
from core.hub_channel import DISCONNECTED

ENV = {'allowUi': False, 'allowDs': True}
SNAPSHOT = {'schemaVersion': 1, 'collectedAt': '2026-10-29T08:00:00.000Z', 'instances': []}
ENV_QUERY = ('env_config_query', {'service': 'k3f9qm'}, 'env_config')
SNAPSHOT_QUERY = ('instance_snapshot_query', {'service': 'k3f9qm'}, 'instance_snapshot')
ENV_RESULT = 'env_config_result'
HUB_DISCONNECTED = (503, {'reason': 'hub_disconnected'})
HUB_UNSUPPORTED = (501, {'reason': 'hub_unsupported'})
BUSY = (503, {'reason': 'busy'})
TIMEOUT = (504, {'reason': 'timeout'})
UNAVAILABLE = (502, {'reason': 'unavailable'})


class Rpc:
    """替身中枢：充当 ws_client 里 `ws.send` 的那一端，记下发出的帧；可设置收到请求后自动回的结果帧。"""

    def __init__(self):
        self.sent = []
        self.events = []        # 'hello' / 'frame' 的先后：证明请求是等到 hub_hello 之后才发出的
        self.reply = None
        self.gate = None        # 设了就让 send 卡在这里，模拟半开连接下 send 阻塞
        self.arrived = []       # 已经进到 send 里的帧（含卡在 gate 上的）

    def send(self, frame):
        self.arrived.append(frame)
        if self.gate is not None:
            self.gate.wait(5)
        self.events.append('frame')
        self.sent.append(frame)
        if self.reply is not None:
            hub_rpc.resolve({**self.reply, 'requestId': frame['requestId']})

    def connect(self, features=('env_config', 'instance_snapshot')):
        """建立连接；features 为 None 表示连上了但还没收到 hub_hello。"""
        hub_rpc.on_connected(self.send)
        if features is not None:
            self.hello(features)
        return self

    def hello(self, features):
        self.events.append('hello')
        hub_rpc.set_features(list(features))


_timers = []


@pytest.fixture
def rpc():
    yield Rpc()
    for timer in _timers:         # 用例提前失败时，没触发的定时器不能漏进下一个用例
        timer.cancel()
    _timers.clear()


@pytest.fixture
def clock(monkeypatch):
    """可手动拨动的 hub_rpc._monotonic。只影响「现在几点」的算术，Event / Condition 的等待仍走真实时间。"""
    now = [100.0]
    monkeypatch.setattr(hub_rpc, '_monotonic', lambda: now[0])
    return now


def reply_with(rpc, frame):
    """此后 rpc 每收到一个请求帧，就用同一个 requestId 调 resolve 回这个结果帧。"""
    rpc.reply = frame


def later(delay, fn):
    timer = threading.Timer(delay, fn)
    timer.daemon = True
    _timers.append(timer)
    timer.start()
    return timer


def register(request_id):
    """手工登记一个属于当前连接、没有等待线程的在途条目，用来确定性地检查 resolve / 唤醒对条目本身做了什么。"""
    entry = {'event': threading.Event(), 'generation': hub_rpc._channel.generation, 'result': None}
    with hub_rpc._channel.lock:
        hub_rpc._channel.pending[request_id] = entry
    return entry


def call_async(timeout=5.0, fields=None):
    """在线程里发一次环境开关查询，返回 (线程, 装结果的 box)。"""
    frame_type, default_fields, feature = ENV_QUERY
    args = (frame_type, dict(fields or default_fields), feature, timeout)
    box = {}
    thread = threading.Thread(target=lambda: box.update(result=hub_rpc.call(*args)), daemon=True)
    thread.start()
    return thread, box


def finish(worker, timeout=2.0):
    thread, box = worker
    thread.join(timeout)
    assert not thread.is_alive(), 'call 没有按预期返回'
    return box['result']


def wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def result_frame(request_id, **body):
    return {'type': ENV_RESULT, 'requestId': request_id, **body}


def test_interface_constants_match_the_contract():
    assert hub_rpc.MAX_IN_FLIGHT == 8
    assert hub_rpc.NEGOTIATE_SECONDS == 5.0
    assert hub_rpc.RESULT_FRAMES == {'env_config_result', 'instance_snapshot_result'}


# ---- 往返与映射 ----

def test_call_returns_env_from_result_frame(rpc):
    rpc.connect(features=['env_config'])
    reply_with(rpc, {'type': 'env_config_result', 'env': {'allowUi': False, 'allowDs': True}})
    assert hub_rpc.call('env_config_query', {'service': 'k3f9qm'}, 'env_config', 1.0) == (200, {'allowUi': False, 'allowDs': True})
    assert hub_rpc._channel.pending == {}


def test_call_returns_snapshot_from_result_frame(rpc):
    rpc.connect()
    reply_with(rpc, {'type': 'instance_snapshot_result', 'snapshot': SNAPSHOT})
    assert hub_rpc.call(*SNAPSHOT_QUERY, 1.0) == (200, SNAPSHOT)
    frame = rpc.sent[0]
    assert frame == {'type': 'instance_snapshot_query', 'requestId': frame['requestId'], 'service': 'k3f9qm'}


def test_request_frame_shape_and_envelope_wins_over_fields(rpc):
    rpc.connect()
    reply_with(rpc, {'type': ENV_RESULT, 'env': ENV})
    assert hub_rpc.call(*ENV_QUERY, 1.0) == (200, ENV)
    frame = rpc.sent[0]
    assert frame == {'type': 'env_config_query', 'requestId': frame['requestId'], 'service': 'k3f9qm'}
    assert isinstance(frame['requestId'], str) and frame['requestId']
    # fields 里夹带 type / requestId 也盖不掉信封：否则永远等不到同 requestId 的结果
    sneaky = {'service': 's', 'type': 'x', 'requestId': 'y'}
    assert hub_rpc.call('env_config_query', sneaky, 'env_config', 1.0) == (200, ENV)
    second = rpc.sent[1]
    assert second['type'] == 'env_config_query' and second['requestId'] not in ('y', frame['requestId'])


def test_not_connected_is_503_hub_disconnected():
    assert hub_rpc.call(*ENV_QUERY, 1.0) == HUB_DISCONNECTED


@pytest.mark.parametrize('error,status', [('not_found', 404), ('not_bound', 403), ('unavailable', 502)])
def test_error_codes(rpc, error, status):
    rpc.connect(features=['env_config'])
    reply_with(rpc, {'type': 'env_config_result', 'error': error})
    assert hub_rpc.call('env_config_query', {'service': 'x'}, 'env_config', 1.0) == (status, {'reason': error})


@pytest.mark.parametrize('frame', [
    {},                                        # 既没有 error 也没有 env
    {'error': 'brand_new_error'},              # 认不得的错误码（中枢以后可能新增）
    {'error': ['not_found']},                  # error 不是字符串（不可哈希，不能当字典键直接查）
    {'error': ''},
    {'env': 'oops'},                           # env 不是对象
    {'env': None},
    {'snapshot': [1, 2]},
])
def test_malformed_result_frames_are_502_unavailable(rpc, frame):
    rpc.connect()
    reply_with(rpc, {'type': ENV_RESULT, **frame})
    assert hub_rpc.call(*ENV_QUERY, 1.0) == UNAVAILABLE
    assert hub_rpc._channel.pending == {}


# ---- hub_hello 协商 ----

def test_hub_without_feature_gets_501(rpc):
    rpc.connect(features=['header_auth'])
    assert hub_rpc.call('env_config_query', {'service': 'x'}, 'env_config', 1.0) == (501, {'reason': 'hub_unsupported'})
    assert rpc.sent == [] and hub_rpc._channel.pending == {}


@pytest.mark.parametrize('features', [None, 'env_config', {'env_config': True}, []])
def test_hello_with_malformed_features_means_no_features(rpc, features):
    rpc.connect(features=None)
    hub_rpc.set_features(features)
    assert hub_rpc.call(*ENV_QUERY, 1.0) == HUB_UNSUPPORTED


def test_waits_for_hello_before_deciding(rpc):
    rpc.connect(features=None)              # 还没收到 hub_hello
    later(0.1, lambda: rpc.hello(['env_config']))
    reply_with(rpc, {'type': 'env_config_result', 'env': {'allowUi': True, 'allowDs': True}})
    assert hub_rpc.call('env_config_query', {'service': 'x'}, 'env_config', 1.0) == (200, {'allowUi': True, 'allowDs': True})
    assert rpc.events == ['hello', 'frame']   # 请求是等到 hub_hello 之后才发出的


def test_hello_without_the_feature_ends_the_wait_at_once(rpc):
    rpc.connect(features=None)
    later(0.1, lambda: rpc.hello(['header_auth']))
    started = time.monotonic()
    assert hub_rpc.call(*ENV_QUERY, 5.0) == HUB_UNSUPPORTED
    assert time.monotonic() - started < 2       # 不用把 5 秒窗口等满
    assert rpc.sent == []


def test_no_hello_within_the_window_is_501(rpc, monkeypatch):
    monkeypatch.setattr(hub_rpc, 'NEGOTIATE_SECONDS', 0.2)
    rpc.connect(features=None)
    started = time.monotonic()
    assert hub_rpc.call(*ENV_QUERY, 5.0) == HUB_UNSUPPORTED
    assert 0.1 <= time.monotonic() - started < 2     # 等了窗口那么久，而不是等满调用方的 5 秒
    assert rpc.sent == [] and hub_rpc._channel.pending == {}


def test_window_already_closed_is_501_without_waiting_and_a_late_hello_still_works(rpc, clock):
    rpc.connect(features=None)
    clock[0] += hub_rpc.NEGOTIATE_SECONDS                # 连上已满窗口，仍没有 hub_hello
    started = time.monotonic()
    assert hub_rpc.call(*ENV_QUERY, 5.0) == HUB_UNSUPPORTED
    assert time.monotonic() - started < 1
    rpc.hello(['env_config'])                            # 中枢慢，hello 晚到：之后的请求照常
    reply_with(rpc, {'type': ENV_RESULT, 'env': ENV})
    assert hub_rpc.call(*ENV_QUERY, 1.0) == (200, ENV)


def test_call_timeout_shorter_than_the_window_is_504(rpc):
    rpc.connect(features=None)
    started = time.monotonic()
    assert hub_rpc.call(*ENV_QUERY, 0.2) == TIMEOUT      # 窗口 5 秒，但调用方只肯等 0.2 秒：不是「中枢太旧」
    assert time.monotonic() - started < 2
    assert hub_rpc._channel.pending == {}


def test_timeout_is_the_total_budget_including_negotiation(rpc):
    rpc.connect(features=None)
    later(0.4, lambda: rpc.hello(['env_config']))         # 无人回复；hello 用掉 0.4 秒，之后只剩 0.4 秒
    started = time.monotonic()
    assert hub_rpc.call(*ENV_QUERY, 0.8) == TIMEOUT
    assert rpc.events == ['hello', 'frame']
    assert time.monotonic() - started < 1.15             # 若等完协商再重新计 0.8 秒，会到 1.2 秒以上


def test_reconnect_clears_features_and_restarts_negotiation(rpc):
    rpc.connect()
    hub_rpc.on_disconnected()
    hub_rpc.set_features(['env_config'])                  # 断线后迟到的 hub_hello 不生效
    assert hub_rpc.call(*ENV_QUERY, 1.0) == HUB_DISCONNECTED
    fresh = Rpc()
    fresh.connect(features=None)                          # 重连：特性回到未知，请求要等本连接的 hub_hello
    waiting = call_async(timeout=3)
    assert wait_for(lambda: len(hub_rpc._channel.pending) == 1)
    time.sleep(0.1)
    assert fresh.sent == []
    fresh.hello(['env_config'])
    assert wait_for(lambda: fresh.sent)
    hub_rpc.resolve(result_frame(fresh.sent[0]['requestId'], env=ENV))
    assert finish(waiting) == (200, ENV)


# ---- 断线 ----

def test_disconnect_wakes_pending_call(rpc):
    rpc.connect()
    workers = [call_async(timeout=5) for _ in range(3)]
    assert wait_for(lambda: len(rpc.sent) == 3)           # 三个请求都已发出，正在等结果
    started = time.monotonic()
    hub_rpc.on_disconnected()
    for worker in workers:
        assert finish(worker) == HUB_DISCONNECTED          # 等待中断线 → 503，不干等超时
    assert time.monotonic() - started < 1
    assert hub_rpc._channel.pending == {}
    assert hub_rpc.call(*ENV_QUERY, 1.0) == HUB_DISCONNECTED


def test_disconnect_during_negotiation_wakes_the_waiting_call(rpc):
    rpc.connect(features=None)
    waiting = call_async(timeout=5)
    assert wait_for(lambda: len(hub_rpc._channel.pending) == 1)
    started = time.monotonic()
    hub_rpc.on_disconnected()
    assert finish(waiting) == HUB_DISCONNECTED
    assert time.monotonic() - started < 1
    assert rpc.sent == [] and hub_rpc._channel.pending == {}


def test_reconnect_without_close_wakes_waiters_of_the_old_connection(rpc):
    rpc.connect()
    old = call_async(timeout=5)
    assert wait_for(lambda: rpc.sent)                     # 请求已发往旧连接
    old_request_id = rpc.sent[0]['requestId']
    fresh = Rpc().connect()                               # 旧连接没来得及 on_disconnected 就换了新连接
    assert finish(old) == HUB_DISCONNECTED
    waiting = call_async(timeout=5)
    assert wait_for(lambda: fresh.sent)
    fresh_id = fresh.sent[0]['requestId']
    hub_rpc.resolve(result_frame(old_request_id, env={'allowUi': True, 'allowDs': True}))   # 旧连接的迟到结果
    assert not hub_rpc._channel.pending[fresh_id]['event'].is_set()
    hub_rpc.resolve(result_frame(fresh_id, env=ENV))
    assert finish(waiting) == (200, ENV)


# ---- 迟到的、旧代次的、断线同时到达的结果 ----

def test_stale_generation_result_is_dropped(rpc):
    rpc.connect()
    worker = call_async()
    assert wait_for(lambda: rpc.sent)
    request_id = rpc.sent[0]['requestId']
    entry = hub_rpc._channel.pending[request_id]
    with hub_rpc._channel.lock:                                   # 模拟：结果到达前连接已换代，等待方还没来得及清理
        hub_rpc._channel.generation += 1
    hub_rpc.resolve(result_frame(request_id, env=ENV))
    assert not entry['event'].is_set() and entry['result'] is None
    hub_rpc.on_disconnected()
    assert finish(worker) == HUB_DISCONNECTED


def test_late_result_after_timeout_is_dropped(rpc):
    rpc.connect()
    assert hub_rpc.call(*ENV_QUERY, 0.1) == TIMEOUT
    hub_rpc.resolve(result_frame(rpc.sent[0]['requestId'], env=ENV))
    assert hub_rpc._channel.pending == {}


def test_stale_result_never_reaches_a_later_call(rpc):
    rpc.connect()
    assert hub_rpc.call(*ENV_QUERY, 0.1) == TIMEOUT
    stale_id = rpc.sent[0]['requestId']
    worker = call_async()
    assert wait_for(lambda: len(rpc.sent) == 2)
    fresh_id = rpc.sent[1]['requestId']
    hub_rpc.resolve(result_frame(stale_id, env={'allowUi': False, 'allowDs': False}))   # 第一次请求迟到的结果
    entry = hub_rpc._channel.pending[fresh_id]
    assert not entry['event'].is_set()
    hub_rpc.resolve(result_frame(fresh_id, env=ENV))
    assert finish(worker) == (200, ENV)


def test_resolve_ignores_unknown_or_malformed_ids(rpc):
    rpc.connect()
    worker = call_async()
    assert wait_for(lambda: rpc.sent)
    entry = hub_rpc._channel.pending[rpc.sent[0]['requestId']]
    for data in ({'requestId': 'nope', 'env': ENV}, {'requestId': None, 'env': ENV},
                 {'requestId': 123, 'env': ENV}, {'requestId': ['x'], 'env': ENV}, {}):
        hub_rpc.resolve(data)
    assert not entry['event'].is_set()
    hub_rpc.on_disconnected()
    assert finish(worker) == HUB_DISCONNECTED


def delivered_env(entry):
    """条目里记下的是中枢送来的结果帧（而不是 DISCONNECTED），返回其中的 env。"""
    assert entry['event'].is_set() and entry['result'] is not DISCONNECTED
    return entry['result']['env']


def test_result_received_before_disconnect_is_not_overwritten(rpc):
    rpc.connect()
    entry = register('r1')
    hub_rpc.resolve(result_frame('r1', env=ENV))
    hub_rpc.on_disconnected()                             # 结果刚到，连接紧接着断：先到的说了算
    assert delivered_env(entry) == ENV
    hub_rpc.on_connected(lambda frame: None)              # 换连接时的唤醒同样不改写已有结果
    assert delivered_env(entry) == ENV


def test_result_received_just_before_disconnect_is_still_returned():
    def reply_then_drop(frame):
        hub_rpc.resolve(result_frame(frame['requestId'], env=ENV))
        hub_rpc.on_disconnected()

    hub_rpc.on_connected(reply_then_drop)
    hub_rpc.set_features(['env_config'])
    assert hub_rpc.call(*ENV_QUERY, 2.0) == (200, ENV)


def test_disconnect_before_result_wins_and_the_late_result_is_dropped(rpc):
    rpc.connect()
    entry = register('r1')
    hub_rpc.on_disconnected()
    hub_rpc.resolve(result_frame('r1', env=ENV))
    assert entry['result'] is DISCONNECTED


def test_second_result_for_the_same_request_is_ignored(rpc):
    rpc.connect()
    entry = register('r1')
    hub_rpc.resolve(result_frame('r1', env=ENV))
    hub_rpc.resolve(result_frame('r1', error='unavailable'))   # 重复或矛盾的第二帧不改写
    assert delivered_env(entry) == ENV
    assert 'error' not in entry['result']


def test_concurrent_calls_each_get_their_own_result(rpc):
    rpc.connect()
    first = call_async(fields={'service': 'a'})
    second = call_async(fields={'service': 'b'})
    assert wait_for(lambda: len(rpc.sent) == 2)
    by_service = {frame['service']: frame['requestId'] for frame in rpc.sent}
    hub_rpc.resolve(result_frame(by_service['b'], env={'who': 'b'}))     # 先回后发的那个
    hub_rpc.resolve(result_frame(by_service['a'], env={'who': 'a'}))
    assert finish(first) == (200, {'who': 'a'})
    assert finish(second) == (200, {'who': 'b'})


# ---- 在途上限与超时 ----

def test_busy_when_in_flight_full(rpc):
    rpc.connect()
    workers = [call_async(timeout=5) for _ in range(hub_rpc.MAX_IN_FLIGHT)]
    assert wait_for(lambda: len(hub_rpc._channel.pending) == hub_rpc.MAX_IN_FLIGHT)
    assert hub_rpc.call(*ENV_QUERY, 1.0) == BUSY          # 第 MAX_IN_FLIGHT + 1 个
    assert len(hub_rpc._channel.pending) == hub_rpc.MAX_IN_FLIGHT
    hub_rpc.on_disconnected()
    for worker in workers:
        assert finish(worker) == HUB_DISCONNECTED


def test_finished_call_frees_its_slot(rpc):
    rpc.connect()
    workers = [call_async(timeout=5) for _ in range(hub_rpc.MAX_IN_FLIGHT)]
    assert wait_for(lambda: len(rpc.sent) == hub_rpc.MAX_IN_FLIGHT)
    hub_rpc.resolve(result_frame(rpc.sent[0]['requestId'], env=ENV))
    assert wait_for(lambda: len(hub_rpc._channel.pending) == hub_rpc.MAX_IN_FLIGHT - 1)    # 有一个调用拿到结果走了
    replacement = call_async(timeout=5)
    assert wait_for(lambda: len(rpc.sent) == hub_rpc.MAX_IN_FLIGHT + 1)            # 腾出的名额被新请求用上，没有判 busy
    hub_rpc.on_disconnected()
    results = [finish(worker) for worker in workers] + [finish(replacement)]
    assert results.count((200, ENV)) == 1 and results.count(HUB_DISCONNECTED) == hub_rpc.MAX_IN_FLIGHT


def test_calls_waiting_for_hello_count_as_in_flight(rpc):
    rpc.connect(features=None)                            # 刚连上的几秒内涌进来的请求也不能无限堆积
    workers = [call_async(timeout=5) for _ in range(hub_rpc.MAX_IN_FLIGHT)]
    assert wait_for(lambda: len(hub_rpc._channel.pending) == hub_rpc.MAX_IN_FLIGHT)
    assert hub_rpc.call(*ENV_QUERY, 1.0) == BUSY
    rpc.hello(['env_config'])                             # hub_hello 到了：8 个都发出请求
    assert wait_for(lambda: len(rpc.sent) == hub_rpc.MAX_IN_FLIGHT)
    hub_rpc.on_disconnected()
    for worker in workers:
        assert finish(worker) == HUB_DISCONNECTED


def test_timeout_returns_504(rpc):
    rpc.connect()
    started = time.monotonic()
    assert hub_rpc.call(*ENV_QUERY, 0.3) == (504, {'reason': 'timeout'})
    assert 0.2 <= time.monotonic() - started < 2
    assert hub_rpc._channel.pending == {}


# ---- 发送线程 ----

def test_blocked_sender_keeps_threads_and_queue_bounded():
    release = threading.Event()
    calls = []

    def blocking_send(frame):
        calls.append(frame)
        release.wait(5)

    hub_rpc.on_connected(blocking_send)
    hub_rpc.set_features(['env_config'])
    baseline = threading.active_count()
    try:
        results = [hub_rpc.call(*ENV_QUERY, 0.05) for _ in range(30)]
        assert len(calls) == 1                                   # 只有一个发送线程卡在 send 上
        assert hub_rpc._channel.queue.qsize() <= hub_rpc.SEND_QUEUE_SIZE
        assert threading.active_count() <= baseline + 1          # 至多多出这一个发送线程
        assert all(result in (TIMEOUT, BUSY) for result in results)
        assert BUSY in results                                   # 队列满后直接 busy，不再排队
        assert hub_rpc._channel.pending == {}                            # 放弃的调用都已清账
    finally:
        release.set()
    assert wait_for(lambda: hub_rpc._channel.queue.qsize() == 0)
    assert len(calls) == 1                                       # 调用方都已放弃，排队的帧全部丢弃


def test_frames_queued_on_old_connection_are_not_sent_after_reconnect():
    release = threading.Event()
    old_calls = []

    def blocking_send(frame):
        old_calls.append(frame)
        release.wait(5)

    hub_rpc.on_connected(blocking_send)
    hub_rpc.set_features(['env_config'])
    first = call_async(timeout=5)
    assert wait_for(lambda: old_calls)                           # 发送线程卡在旧连接的 send 上
    queued = call_async(timeout=5)
    assert wait_for(lambda: hub_rpc._channel.queue.qsize() == 1)
    hub_rpc.on_disconnected()                                    # 断线：两个在途请求都立即返回
    assert finish(first) == HUB_DISCONNECTED
    assert finish(queued) == HUB_DISCONNECTED
    new = Rpc().connect()
    fresh = call_async(timeout=5)
    release.set()                                                # 旧 send 返回：旧帧丢弃，新帧照常发出
    assert wait_for(lambda: new.sent)
    assert len(old_calls) == 1 and len(new.sent) == 1
    assert new.sent[0]['requestId'] != old_calls[0]['requestId']
    hub_rpc.resolve(result_frame(new.sent[0]['requestId'], env=ENV))
    assert finish(fresh) == (200, ENV)


def test_send_one_drops_old_generation_or_abandoned_frames(rpc):
    rpc.connect()
    generation = hub_rpc._channel.generation
    register('live')
    frame = {'type': 'env_config_query', 'requestId': 'live'}
    now = hub_rpc._monotonic()
    assert hub_rpc._channel.send_one((now, generation - 1, 'live', frame)) is False    # 旧连接的帧
    assert hub_rpc._channel.send_one((now, generation, 'gone', frame)) is False        # 调用方已放弃的帧
    assert rpc.sent == []
    assert hub_rpc._channel.send_one((now, generation, 'live', frame)) is True
    assert rpc.sent == [frame]


def test_old_frame_of_a_caller_still_waiting_is_sent(rpc):
    rpc.connect()
    register('live')
    frame = {'type': 'env_config_query', 'requestId': 'live'}
    way_back = hub_rpc._monotonic() - 3600                    # 在队列里躺了一小时，调用方却还在等
    assert hub_rpc._channel.send_one((way_back, hub_rpc._channel.generation, 'live', frame)) is True
    assert rpc.sent == [frame]


def test_queued_frame_older_than_ops_code_stale_limit_is_still_sent_when_caller_waits(rpc, clock):
    gate = threading.Event()
    rpc.gate = gate                                           # 第一个请求卡在 send 上，第二个请求的帧在队列里排着
    rpc.connect()
    reply_with(rpc, {'type': ENV_RESULT, 'env': ENV})
    try:
        first = call_async(timeout=30)
        assert wait_for(lambda: rpc.arrived)
        second = call_async(timeout=30)
        assert wait_for(lambda: hub_rpc._channel.queue.qsize() == 1)
        clock[0] += ops_code.SEND_STALE_SECONDS + 2            # 排队时间超过 ops_code 的 8 秒上限，但没超过本次调用的 30 秒
    finally:
        gate.set()
    assert finish(first) == (200, ENV)
    assert finish(second) == (200, ENV)                       # 若套用 8 秒的过期规则，第二帧会被丢掉，调用只能超时
    assert len(rpc.sent) == 2


def test_send_failure_wakes_request_and_never_logs_the_frame(caplog):
    def broken_send(frame):
        raise RuntimeError(json.dumps(frame))                    # 异常信息里带着整帧

    caplog.set_level(logging.DEBUG)
    hub_rpc.on_connected(broken_send)
    hub_rpc.set_features(['env_config'])
    started = time.monotonic()
    assert hub_rpc.call(*ENV_QUERY, 5.0) == HUB_DISCONNECTED     # 发不出去按断连处理，不干等超时
    assert time.monotonic() - started < 2
    assert 'k3f9qm' not in caplog.text
    assert 'RuntimeError' in caplog.text and 'env_config_query' in caplog.text
