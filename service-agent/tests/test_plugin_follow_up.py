"""restart/update 后的插件同步跟踪、重连补采、hub 拒绝拉取时的错误码。"""
import importlib
import io
import json
import os
import sys
from types import SimpleNamespace

import pytest

from core import handlers, status_reporter


class ImmediateThread:
    def __init__(self, target, daemon=None, args=()):
        self.target = target
        self.args = args

    def start(self):
        self.target(*self.args)


def _inventory(current=True, finished='2026-09-23T10:00:05Z', status='ok'):
    return {'status': status, 'syncCurrent': current, 'sync': {'finishedAt': finished} if finished else None}


def _report(*inventories, plain=False):
    services = [{'name': f's{i}', 'plugins': inv} for i, inv in enumerate(inventories)]
    if plain:
        services.append({'name': 'nginx'})  # 没有插件目录的容器不影响判定
    return [{'deploymentId': 1, 'services': services}]


def test_sync_settled_requires_every_inventory_current_and_finished():
    settled = status_reporter._sync_settled
    assert settled(_report(_inventory(), plain=True), 1)
    assert settled(_report(plain=True), 1)  # 部署没有插件目录：无需跟踪
    assert not settled(_report(_inventory(), _inventory(current=False)), 1)
    assert not settled(_report(_inventory(finished=None)), 1)  # 同步进行中
    assert not settled(_report(_inventory(status='error')), 1)
    assert not settled(_report({'status': 'ok', 'syncCurrent': False, 'sync': None}), 1)


def test_empty_or_unmatched_collection_is_not_settled():
    settled = status_reporter._sync_settled
    # compose ps 超时/非 0 时采集结果为空，不能当成同步已结束
    assert not settled([], 1)
    # 目标还没出现在 watch_targets
    assert not settled([], 0)
    assert not settled(_report(_inventory()), 0)
    # 两个同目录部署只采到一个
    assert not settled(_report(_inventory()), 2)


def test_no_receipt_after_grace_stops_tracking():
    started = '2026-09-24T00:00:00Z'
    start = status_reporter._epoch(started)
    no_receipt = {'status': 'ok', 'syncCurrent': False, 'sync': None, 'containerStartedAt': started}
    unreadable = {'status': 'error', 'containerStartedAt': started}
    stale_receipt = {'status': 'ok', 'syncCurrent': False, 'sync': {'finishedAt': 'x'}, 'containerStartedAt': started}
    running = {'status': 'ok', 'syncCurrent': True, 'sync': {'finishedAt': None}, 'containerStartedAt': started}
    grace = status_reporter.NO_RECEIPT_GRACE
    for item in (no_receipt, unreadable, stale_receipt):
        # 刚启动：同步脚本可能还没写回执，继续跟踪
        assert not status_reporter._sync_settled(_report(item), 1, start + grace - 1)
        # 启动已久仍没有本次回执：不走插件同步，停止跟踪（不兼容旧镜像）
        assert status_reporter._sync_settled(_report(item), 1, start + grace)
    # 本次同步正在进行：不受宽限期影响，继续跟踪到结束或截止
    assert not status_reporter._sync_settled(_report(running), 1, start + 10 * grace)


@pytest.fixture
def follow(monkeypatch):
    monkeypatch.setattr(status_reporter, 'PLUGIN_FOLLOW_UP_TIMEOUT', 300)
    monkeypatch.setattr(status_reporter.threading, 'Thread', ImmediateThread)
    sleeps = []
    monkeypatch.setattr(status_reporter.time, 'sleep', lambda seconds: sleeps.append(seconds))
    ws = SimpleNamespace(keep_running=True)
    monkeypatch.setattr(status_reporter, '_active_ws', ws)
    status_reporter.set_watch_targets([{'deploymentId': 1, 'dir': '/data/a'}])
    return ws, sleeps


def test_follow_up_keeps_going_when_collection_comes_back_empty(follow, monkeypatch):
    ws, sleeps = follow
    results = iter([[], _report(_inventory())])  # 第一轮 compose ps 超时返回空
    monkeypatch.setattr(status_reporter, '_collect_and_send', lambda *a, **k: next(results))
    status_reporter.follow_up('/data/a')
    assert sleeps == [status_reporter.PLUGIN_FOLLOW_UP_INTERVAL]
    assert status_reporter._follow_ups == {}


def test_follow_up_collects_target_dir_until_sync_settles(follow, monkeypatch, tmp_path):
    ws, sleeps = follow
    target, other = tmp_path / 'a', tmp_path / 'b'
    status_reporter.set_watch_targets([{'deploymentId': 1, 'dir': str(target)}, {'deploymentId': 2, 'dir': str(other)}])
    results = iter([_report(_inventory(current=False)), _report(_inventory(finished=None)), _report(_inventory())])
    calls = []

    def collect(target_ws, services_filter=None, dirs=None):
        calls.append(dirs)
        return next(results)

    monkeypatch.setattr(status_reporter, '_collect_and_send', collect)
    assert status_reporter.follow_up(str(target)) is True
    assert calls == [{os.path.normcase(os.path.abspath(str(target)))}] * 3
    assert sleeps == [status_reporter.PLUGIN_FOLLOW_UP_INTERVAL] * 2
    assert status_reporter._follow_ups == {}


def test_collect_batch_filters_by_directory(monkeypatch, tmp_path):
    status_reporter.set_watch_targets([{'deploymentId': 1, 'dir': str(tmp_path / 'a')},
                                       {'deploymentId': 2, 'dir': str(tmp_path / 'b')}])
    collected, sent = [], []
    monkeypatch.setattr(status_reporter, 'collect_service_statuses', lambda d: collected.append(d) or [{'name': 'app'}])
    monkeypatch.setattr(status_reporter, 'enrich_statuses', lambda services: services)
    monkeypatch.setattr(status_reporter, 'send_message', lambda ws, payload: sent.append(payload))
    reports = status_reporter._collect_and_send(object(), dirs={status_reporter._dir_key(tmp_path / 'b')})
    assert collected == [str(tmp_path / 'b')]
    assert reports == [{'deploymentId': 2, 'services': [{'name': 'app'}]}]
    assert sent == [{'type': 'status_report', 'reports': reports}]


def test_follow_up_stops_at_deadline_and_skips_while_disconnected(follow, monkeypatch):
    ws, sleeps = follow
    monkeypatch.setattr(status_reporter, '_active_ws', None)
    now = [0.0]
    monkeypatch.setattr(status_reporter.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(status_reporter.time, 'sleep', lambda seconds: now.__setitem__(0, now[0] + seconds))
    collect = []
    monkeypatch.setattr(status_reporter, '_collect_and_send', lambda *a, **k: collect.append(a) or [])
    status_reporter.follow_up('/data/a')
    assert collect == []  # 断连期间不采集
    assert now[0] >= status_reporter.PLUGIN_FOLLOW_UP_TIMEOUT
    assert status_reporter._follow_ups == {}


def test_follow_up_keeps_collecting_after_collect_error(follow, monkeypatch, caplog):
    results = iter([RuntimeError('docker busy'), _report(_inventory())])

    def collect(*args, **kwargs):
        value = next(results)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(status_reporter, '_collect_and_send', collect)
    status_reporter.follow_up('/data/a')
    assert '插件同步跟踪采集失败' in caplog.text
    assert status_reporter._follow_ups == {}


def test_repeated_command_extends_existing_follow_up(follow, monkeypatch):
    ws, sleeps = follow
    rounds = []

    def collect(*args, **kwargs):
        rounds.append(len(rounds))
        if len(rounds) == 1:
            # 跟踪期间又来一次重启：只顺延，不另起线程；本轮「已结束」的判定早于新命令，不能据此停止
            assert status_reporter.follow_up('/data/a') is False
        return _report(_inventory())

    monkeypatch.setattr(status_reporter, '_collect_and_send', collect)
    status_reporter.follow_up('/data/a')
    assert rounds == [0, 1]
    assert status_reporter._follow_ups == {}


def test_follow_up_releases_directory_on_unexpected_error(follow, monkeypatch):
    monkeypatch.setattr(status_reporter, '_collect_and_send', lambda *a, **k: _report(_inventory(current=False)))
    monkeypatch.setattr(status_reporter.time, 'sleep', lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        status_reporter.follow_up('/data/a')
    assert status_reporter._follow_ups == {}


@pytest.mark.parametrize('action,expected', [('restart', 'follow'), ('update', 'follow'),
                                             ('plugin_remove', 'report'), ('plugin_restore', 'report'),
                                             ('plugin_scan', None)])
def test_dispatch_tracks_sync_after_restart_and_update(monkeypatch, tmp_path, action, expected):
    calls = []
    monkeypatch.setattr(status_reporter, 'follow_up', lambda project_dir: calls.append(('follow', project_dir)))
    monkeypatch.setattr(status_reporter, 'request_report', lambda service=None: calls.append(('report', service)))
    monkeypatch.setitem(handlers.HANDLERS, action, lambda *args: None)
    handlers.dispatch(SimpleNamespace(), {'requestId': 'r1', 'action': action, 'dir': str(tmp_path)})
    if expected == 'follow':
        assert calls == [('follow', str(tmp_path))]
    elif expected == 'report':
        assert calls == [('report', None)]
    else:
        assert calls == []


def _import_ws_client(monkeypatch):
    monkeypatch.setenv('WS_URL', 'ws://hub.example/ws/agent')
    monkeypatch.setenv('AGENT_KEY', 'secret-key')
    for name in ['config', 'core.ws_client']:
        sys.modules.pop(name, None)
    return importlib.import_module('core.ws_client')


def test_each_new_connection_reports_once_even_if_targets_unchanged(monkeypatch):
    module = _import_ws_client(monkeypatch)
    changed = iter([True, False, False, False])
    monkeypatch.setattr(module, 'set_watch_targets', lambda targets: next(changed))
    reports = []
    monkeypatch.setattr(module, 'request_report', lambda: reports.append(1))
    frame = json.dumps({'type': 'watch_targets', 'targets': [{'deploymentId': 1, 'dir': '/data/a'}]})
    first, second = SimpleNamespace(), SimpleNamespace()
    module._on_message(first, frame)   # 首连
    module._on_message(first, frame)   # 同一连接重复推送相同清单：不重复采集
    module._on_message(second, frame)  # 重连：清单相同也要补采断连期间丢失的变化
    module._on_message(second, frame)
    assert len(reports) == 2


def test_plugin_query_result_error_reaches_resolve(monkeypatch):
    module = _import_ws_client(monkeypatch)
    captured = []
    monkeypatch.setattr(module.plugin_query, 'resolve', lambda *args: captured.append(args))
    module._on_message(None, json.dumps({'type': 'plugin_query_result', 'requestId': 'r1', 'plugins': [],
                                         'error': 'not_found'}))
    assert captured == [('r1', [], 'not_found')]


@pytest.mark.parametrize('error,expected', [('not_found', 'not_found'), ('unavailable', 'unavailable'),
                                            ('something-else', None), (None, None)])
def test_request_surfaces_hub_rejection(monkeypatch, error, expected):
    # 独立模块实例：别的用例遗留的连接线程可能在断开时清掉共享模块的 sender
    sys.modules.pop('core.plugin_query', None)
    pq = importlib.import_module('core.plugin_query')

    def sender(message):
        pq.resolve(message['requestId'], [], error)

    pq.set_sender(sender)
    result = pq.request('svc', 1)
    if expected:
        assert isinstance(result, pq.QueryError) and result.reason == expected
    else:
        assert result == []


def _query_handler(monkeypatch, reason):
    monkeypatch.setenv('AGENT_LOCAL_SECRET', 'topsecret')
    for name in ['config', 'core.health_server']:
        sys.modules.pop(name, None)
    module = importlib.import_module('core.health_server')
    monkeypatch.setattr(module, 'get_connection_state', lambda: {'connected': True})
    # 用 health_server 当前绑定的模块构造，别的用例可能已重新导入 core.plugin_query
    reply = module.plugin_query.QueryError(reason)
    monkeypatch.setattr(module.plugin_query, 'request', lambda service, timeout: reply)
    handler = module._HealthHandler.__new__(module._HealthHandler)
    handler.path = '/queryPlugin?service=svc'
    handler.headers = {'X-Agent-Secret': 'topsecret'}
    handler.wfile = io.BytesIO()
    responses = []
    handler.send_response = responses.append
    handler.send_header = lambda *a: None
    handler.end_headers = lambda: None
    return handler, responses


@pytest.mark.parametrize('reason,status', [('not_found', 404), ('unavailable', 502)])
def test_query_plugin_maps_hub_rejection_to_error_status(monkeypatch, reason, status):
    handler, responses = _query_handler(monkeypatch, reason)
    handler.do_GET()
    # 非 200 让 sync-plugins 记为清单拉取失败并保留本地版本，而不是「插件列表为空，同步成功」
    assert responses == [status]
    assert handler.wfile.getvalue() == b''
