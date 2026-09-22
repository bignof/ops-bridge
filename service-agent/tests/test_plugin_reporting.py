import importlib
import io
import json
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('path,configured,provided,body,length,accepted,expected', [
    ('/other','secret','secret',b'{}',2,True,404),
    ('/pluginSyncChanged','secret','wrong',b'{}',2,True,401),
    ('/pluginSyncChanged','','',b'{}',2,True,403),
    ('/pluginSyncChanged','secret','secret',b'{}',99999,True,400),
    ('/pluginSyncChanged','secret','secret',b'[]',2,True,400),
    ('/pluginSyncChanged','secret','secret',b'{',1,True,400),
    ('/pluginSyncChanged','secret','secret',b'{}',2,True,400),
    ('/pluginSyncChanged','secret','secret',b'{"service":"app"}',17,True,202),
    ('/pluginSyncChanged','secret','secret',b'{"service":"app"}',17,False,503),
])
def test_sync_notification_requires_local_secret_and_bounded_service(monkeypatch, path, configured, provided, body, length, accepted, expected):
    health = importlib.import_module('core.health_server')
    reporter = importlib.import_module('core.status_reporter')
    monkeypatch.setattr(health, 'AGENT_LOCAL_SECRET', configured)
    calls = []
    monkeypatch.setattr(reporter, 'request_report', lambda service: calls.append(service) or accepted)
    handler = health._HealthHandler.__new__(health._HealthHandler)
    handler.path = path
    handler.headers = {'X-Agent-Secret': provided, 'Content-Length': str(length)}
    handler.rfile = io.BytesIO(body)
    responses = []
    handler.send_response = responses.append
    handler.end_headers = lambda: None
    handler.do_POST()
    assert responses == [expected]
    assert calls == (['app'] if expected in (202,503) else [])


@pytest.fixture
def reporter(monkeypatch):
    module = importlib.import_module('core.status_reporter')
    monkeypatch.setattr(module, '_active_ws', None)
    monkeypatch.setattr(module, '_refresh_running', False)
    monkeypatch.setattr(module, '_refresh_services', set())
    module.set_watch_targets([{'deploymentId':1,'dir':'/data/a','service':'app'}])
    return module


def test_notifications_merge_without_losing_new_connection(monkeypatch, reporter):
    pending_threads=[]
    class Thread:
        def __init__(self,target,daemon):
            self.target=target
        def start(self):
            pending_threads.append(self.target)
    monkeypatch.setattr(reporter.threading,'Thread',Thread)
    assert not reporter.request_report()
    old_ws=SimpleNamespace(keep_running=True)
    monkeypatch.setattr(reporter,'_active_ws',old_ws)
    assert not reporter.request_report('foreign')
    calls=[]
    monkeypatch.setattr(reporter,'_collect_and_send',lambda ws, services=None:calls.append((ws,services)))
    assert reporter.request_report('app')
    assert reporter.request_report('app')
    assert len(pending_threads)==1
    current=SimpleNamespace(keep_running=True)
    reporter._active_ws=current
    pending_threads[0]()
    assert calls==[(current,{'app'})]
    assert not reporter._refresh_running
    reporter.stop_status_reporting(old_ws)
    assert reporter._active_ws is current
    reporter.stop_status_reporting(current)
    assert reporter._active_ws is None


def test_notification_handles_legacy_targets_and_collection_failure(monkeypatch, reporter):
    class Thread:
        def __init__(self,target,daemon):self.target=target
        def start(self):self.target()
    monkeypatch.setattr(reporter.threading,'Thread',Thread)
    monkeypatch.setattr(reporter,'_active_ws',SimpleNamespace(keep_running=True))
    reporter.set_watch_targets([{'deploymentId':1,'dir':'/data/a'}])
    calls=[]
    def fail(ws,service=None):
        calls.append(service)
        raise OSError('unavailable')
    monkeypatch.setattr(reporter,'_collect_and_send',fail)
    assert reporter.request_report('app')
    assert calls==[None] and not reporter._refresh_running


def test_notification_after_disconnect_exits_without_sending(monkeypatch, reporter):
    callbacks=[]
    class Thread:
        def __init__(self,target,daemon):self.target=target
        def start(self):callbacks.append(self.target)
    monkeypatch.setattr(reporter.threading,'Thread',Thread)
    monkeypatch.setattr(reporter,'_active_ws',SimpleNamespace(keep_running=True))
    reporter.request_report('app')
    reporter._active_ws=None
    callbacks[0]()
    assert not reporter._refresh_running


def test_collection_filters_service_without_touching_other_deployments(monkeypatch, reporter):
    reporter.set_watch_targets([{'deploymentId':1,'dir':'/data/a','service':'a'},{'deploymentId':2,'dir':'/data/b','service':'b'}])
    calls=[]
    monkeypatch.setattr(reporter,'collect_service_statuses',lambda directory:calls.append(directory) or [])
    reporter._collect_and_send(SimpleNamespace(),{'a'})
    assert calls==['/data/a']


@pytest.mark.parametrize('payload,fail,status', [({},False,'success'),([],False,'failed'),({},True,'failed')])
def test_file_command_ack_result_and_report_share_outbox(monkeypatch,payload,fail,status):
    handlers=importlib.import_module('core.handlers')
    plugins=importlib.import_module('services.plugins')
    messages=[]
    ws=SimpleNamespace(send=lambda value:messages.append(json.loads(value)))
    def execute(*args):
        if fail:raise OSError('read-only filesystem')
        return {'message':'已采集'},[{'containerId':'a'*64,'plugins':{'schemaVersion':1}}]
    monkeypatch.setattr(plugins,'execute_plugin_operation',execute)
    handlers.handle_plugin_operation(ws,{'action':'plugin_scan','plugin':payload},'r-test','/data/test')
    assert messages[0]['type']=='ack'
    assert messages[-1]['type']=='result' and messages[-1]['status']==status
    if status=='success':assert messages[-1]['pluginReport']['services'][0]['plugins']['schemaVersion']==1


def test_new_file_actions_use_existing_project_queue(monkeypatch,tmp_path):
    handlers=importlib.import_module('core.handlers')
    calls=[]
    monkeypatch.setitem(handlers.HANDLERS,'plugin_scan',lambda *args:calls.append(args))
    ws=SimpleNamespace(send=lambda payload:None)
    handlers.dispatch(ws,{'requestId':'r-scan','action':'plugin_scan','dir':str(tmp_path)})
    assert calls[0][2]=='r-scan'
