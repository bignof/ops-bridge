import importlib
import io
import json
import logging

import pytest

BODY = {
    'service': 'done-admin', 'code': '246802', 'operation': 'backups:upload', 'operationLabel': '上传备份并还原',
    'operator': {'userId': 7, 'name': '张' * 128, 'ip': '172.18.0.1'},
    'file': {'name': '备份.nbdata', 'size': 1024},
}


def _with(path, value):
    body = json.loads(json.dumps(BODY))
    target = body
    *parents, last = path.split('.')
    for key in parents:
        target = target[key]
    target[last] = value
    return body


def _post(monkeypatch, body, *, configured='s3cret', supplied='s3cret', length=None, result=(200, {'valid': True})):
    health = importlib.import_module('core.health_server')
    monkeypatch.setattr(health, 'AGENT_LOCAL_SECRET', configured)
    calls = []
    monkeypatch.setattr(health.ops_code, 'verify', lambda fields: calls.append(fields) or result)
    raw = body if isinstance(body, bytes) else json.dumps(body).encode('utf-8')
    handler = health._HealthHandler.__new__(health._HealthHandler)
    handler.path = '/verifyOpsCode'
    handler.headers = {'X-Agent-Secret': supplied, 'Content-Length': str(len(raw) if length is None else length)}
    handler.rfile = io.BytesIO(raw)
    handler.wfile = io.BytesIO()
    responses = []
    handler.send_response = responses.append
    handler.send_header = lambda key, value: None
    handler.end_headers = lambda: None
    handler.do_POST()
    payload = handler.wfile.getvalue()
    return responses[0], (json.loads(payload) if payload else None), calls


def test_unconfigured_secret_is_forbidden(monkeypatch):
    status, _, calls = _post(monkeypatch, BODY, configured='', supplied='')
    assert status == 403 and calls == []


def test_wrong_secret_is_unauthorized(monkeypatch):
    status, _, calls = _post(monkeypatch, BODY, supplied='nope')
    assert status == 401 and calls == []


@pytest.mark.parametrize('length', [0, 4097])
def test_body_length_is_bounded(monkeypatch, length):
    status, _, calls = _post(monkeypatch, BODY, length=length)
    assert status == 400 and calls == []


@pytest.mark.parametrize('body', [
    b'not json', b'[]', b'[' * 2000 + b']' * 2000,
    _with('service', ''), _with('service', 'x' * 256), _with('code', ''), _with('code', 'x' * 65),
    _with('operation', 'backups'), _with('operation', 'a:b:c'), _with('operation', 'bad name:x'),
    _with('operationLabel', 'x' * 65), _with('operator', None), _with('operator.userId', True),
    _with('operator.userId', 'x' * 65), _with('operator.userId', -1), _with('operator.name', '张' * 129),
    _with('operator.ip', 'x' * 65), _with('file', 'a.nbdata'), _with('file.size', -1), _with('file.size', '1'),
    _with('file.name', 'x' * 256),
])
def test_invalid_body_is_rejected(monkeypatch, body):
    status, _, calls = _post(monkeypatch, body)
    assert status == 400 and calls == []


def test_valid_body_is_forwarded_with_protocol_fields_only(monkeypatch):
    body = {**BODY, 'extra': 'dropped', 'operator': {**BODY['operator'], 'role': 'admin'}}
    result = (200, {'valid': True, 'namespace': {'code': 'prod', 'name': '生产'}})
    status, payload, calls = _post(monkeypatch, body, result=result)
    assert status == 200 and payload == {'valid': True, 'namespace': {'code': 'prod', 'name': '生产'}}
    assert calls == [{'service': 'done-admin', 'code': '246802', 'operation': 'backups:upload',
                      'operationLabel': '上传备份并还原',
                      'operator': {'userId': 7, 'name': '张' * 128, 'ip': '172.18.0.1'},
                      'file': {'name': '备份.nbdata', 'size': 1024}}]


def test_optional_fields_may_be_omitted(monkeypatch):
    body = {'service': 'a', 'code': '1', 'operation': 'a:b', 'operator': {'userId': 'u-1'}}
    status, _, calls = _post(monkeypatch, body)
    assert status == 200
    assert calls == [{'service': 'a', 'code': '1', 'operation': 'a:b', 'operator': {'userId': 'u-1', 'name': '', 'ip': ''}}]


@pytest.mark.parametrize('result', [
    (503, {'reason': 'unreachable'}), (503, {'reason': 'negotiating'}), (501, None), (503, {'reason': 'busy'}),
    (503, {'reason': 'disconnected'}), (504, None), (200, {'valid': False, 'reason': 'locked', 'retryAfter': 300}),
])
def test_outcomes_map_to_http(monkeypatch, result):
    status, payload, _ = _post(monkeypatch, BODY, result=result)
    assert (status, payload) == result


def test_code_never_reaches_logs(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    _post(monkeypatch, BODY, result=(200, {'valid': False, 'reason': 'invalid'}))
    _post(monkeypatch, BODY, supplied='nope')
    _post(monkeypatch, _with('service', ''))
    assert BODY['code'] not in caplog.text
    assert 'verifyOpsCode' in caplog.text
