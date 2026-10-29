"""共用通道本身的用例：用裸的 HubChannel 实例，不经 ops_code / hub_rpc，检查两边共享的并发语义和各自的配置。"""
import threading
import time

import pytest

from core import hub_rpc, ops_code
from core.hub_channel import DISCONNECTED, HubChannel


def wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def make_channel(stale_seconds=None):
    sent = []
    channel = HubChannel('test', 8, stale_seconds=stale_seconds)
    channel.on_connected(sent.append)
    return channel, sent


def register(channel, request_id, limit=8):
    with channel.lock:
        return channel.register(request_id, limit)


def test_each_module_configures_its_own_channel():
    """排队过期的规则按实例配置：ops_code 保留原来的 8 秒；hub_rpc 有 10 秒的快照调用，不能套这条。"""
    assert ops_code._channel.stale_seconds == ops_code.SEND_STALE_SECONDS == 8.0
    assert hub_rpc._channel.stale_seconds is None
    assert ops_code._channel is not hub_rpc._channel


@pytest.mark.parametrize('stale_seconds,old_frame_is_sent', [(8.0, False), (None, True)])
def test_stale_rule_applies_only_to_a_channel_configured_with_it(stale_seconds, old_frame_is_sent):
    channel, sent = make_channel(stale_seconds)
    register(channel, 'live')
    frame = {'type': 'x_query', 'requestId': 'live'}
    now = channel.clock()
    assert channel.send_one((now - 100, channel.generation, 'live', frame)) is old_frame_is_sent
    assert sent == ([frame] if old_frame_is_sent else [])
    assert channel.send_one((now, channel.generation, 'live', frame)) is True     # 新鲜的帧两边都发
    assert sent[-1] == frame


def test_register_stops_at_the_limit_and_release_frees_the_slot():
    channel, _ = make_channel()
    assert register(channel, 'a', limit=2) is not None
    assert register(channel, 'b', limit=2) is not None
    assert register(channel, 'c', limit=2) is None
    assert sorted(channel.pending) == ['a', 'b']
    channel.release('a')
    channel.release('a')                                   # 重复释放无妨
    assert register(channel, 'c', limit=2) is not None


def test_resolve_delivers_the_frame_without_request_id_and_says_whether_it_was_delivered():
    channel, _ = make_channel()
    entry = register(channel, 'r1')
    assert channel.resolve({'type': 'x_result', 'requestId': 'r1', 'value': 1}) is True
    assert entry['event'].is_set() and entry['result'] == {'type': 'x_result', 'value': 1}
    assert channel.resolve({'type': 'x_result', 'requestId': 'r1', 'value': 2}) is False   # 已有结果，不改写
    assert entry['result'] == {'type': 'x_result', 'value': 1}
    for data in ({'requestId': 'nope'}, {'requestId': None}, {'requestId': 7}, {'requestId': ['r1']}, {}):
        assert channel.resolve(data) is False


def test_resolve_drops_results_of_an_older_generation():
    channel, _ = make_channel()
    entry = register(channel, 'r1')
    with channel.lock:                                     # 模拟：结果到达前连接已换代，等待方还没来得及清理
        channel.generation += 1
    assert channel.resolve({'requestId': 'r1', 'value': 1}) is False
    assert not entry['event'].is_set() and entry['result'] is None


@pytest.mark.parametrize('event', ['disconnect', 'reconnect'])
def test_connection_events_wake_the_rest_but_never_overwrite_a_delivered_result(event):
    channel, _ = make_channel()
    delivered = register(channel, 'r1')
    waiting = register(channel, 'r2')
    channel.resolve({'type': 'x_result', 'requestId': 'r1', 'value': 1})
    if event == 'disconnect':
        channel.on_disconnected()
    else:
        channel.on_connected(lambda frame: None)           # 旧连接没走到 on_disconnected 就换了新连接
    assert delivered['result'] == {'type': 'x_result', 'value': 1}      # 先到的说了算
    assert waiting['event'].is_set() and waiting['result'] is DISCONNECTED
    assert channel.pending.keys() == {'r1', 'r2'}          # 条目由各自的调用方 release，通道不替它们清


def test_features_are_unknown_until_hello_and_cleared_on_disconnect():
    channel, _ = make_channel()
    assert channel.connected and channel.features is None
    channel.set_features(['a', 3, 'b'])
    assert channel.features == {'a', 'b'}
    channel.set_features('a')                              # 不是列表：按没有任何特性处理
    assert channel.features == frozenset()
    channel.on_disconnected()
    assert not channel.connected and channel.features is None
    channel.set_features(['a'])                            # 断线后迟到的 hub_hello 忽略
    assert channel.features is None


def test_wait_features_returns_on_hello_on_wake_up_or_when_time_is_up():
    channel, _ = make_channel()
    entry = register(channel, 'r1')

    def wait(seconds):
        with channel.lock:
            channel.wait_features(entry, channel.clock() + seconds)
            return channel.features, entry['event'].is_set()

    assert wait(0.05) == (None, False)                     # 时间到：特性仍未知、条目没结果
    timer = threading.Timer(0.05, lambda: channel.set_features(['a']))
    timer.start()
    assert wait(5) == (frozenset({'a'}), False)            # hub_hello 到了就返回，不等满 5 秒
    channel.on_connected(lambda frame: None)               # 换连接：特性回到未知，条目被唤醒
    assert wait(5) == (None, True)


def test_enqueue_on_a_full_queue_returns_false_and_takes_the_registration_back():
    release = threading.Event()
    taken = []

    def blocking_send(frame):
        taken.append(frame)
        release.wait(5)

    channel = HubChannel('test', 1)                        # 队列只装得下 1 帧
    channel.on_connected(blocking_send)

    def submit(request_id):
        with channel.lock:
            entry = channel.register(request_id, 8)
            return channel.enqueue(request_id, entry, {'type': 'x_query', 'requestId': request_id})

    try:
        assert submit('a') is True
        assert wait_for(lambda: taken)                     # a 被发送线程取走，卡在 send 上
        assert submit('b') is True                         # b 占满队列
        assert submit('c') is False                        # 队列满：拒绝，并撤掉登记，不能留在在途表里占名额
        assert sorted(channel.pending) == ['a', 'b']
    finally:
        release.set()
