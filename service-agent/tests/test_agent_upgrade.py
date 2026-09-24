import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from services import agent_upgrade as u

RID = '11111111-2222-3333-4444-555555555555'


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(u, '_runtime', None)
    monkeypatch.setattr(u, '_runtime_checked_at', 0.0)
    monkeypatch.setattr(u, '_active', 0)
    monkeypatch.setattr(u, '_starting', False)
    monkeypatch.setenv('AGENT_ID', 'test-agent')
    monkeypatch.setitem(sys.modules, 'fcntl', SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=lambda *a: None))


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    project.mkdir()
    source = project / 'compose.yml'
    source.write_text('services:\n  agent:\n    image: registry/agent:v1\n  business:\n    image: business:1\n', encoding='utf-8')
    monkeypatch.setenv('AGENT_UPGRADE_DIR', str(tmp_path / 'upgrades'))
    info = {'Id': 'container-old', 'Name': '/my-agent', 'Image': 'sha256:old',
            'Config': {'Image': 'registry/agent:v1', 'Env': ['AGENT_ID=test-agent'], 'Labels': {
                'com.docker.compose.project.config_files': '/host/project/compose.yml',
                'com.docker.compose.project.working_dir': '/host/project',
                'com.docker.compose.service': 'agent', 'com.docker.compose.project': 'test',
            }}, 'Mounts': [
                {'Type': 'bind', 'Source': '/host', 'Destination': str(tmp_path), 'RW': True},
                {'Type': 'bind', 'Source': '/var/run/docker.sock', 'Destination': '/var/run/docker.sock', 'RW': True},
            ]}
    monkeypatch.setattr(u, 'inspect_container', lambda name: info)
    return info, source


@pytest.mark.parametrize('image', ['registry/agent:v2', 'registry/agent@sha256:abcd', 'registry/agent:latest'])
def test_image_accept(image):
    assert u.validate_image(image, 'registry/agent:v1') == image


@pytest.mark.parametrize('image', [None, '', '-evil', 'registry/agent:v2;ls', 'different/agent:1', 'registry/agent', 'r' * 501])
def test_image_reject(image):
    with pytest.raises(ValueError):
        u.validate_image(image, 'registry/agent:v1')


def test_registry_port_repository():
    assert u.image_repository('localhost:5000/agent:v2') == 'localhost:5000/agent'
    assert u.image_repository('localhost:5000/agent') == 'localhost:5000/agent'


def test_atomic_and_missing(tmp_path):
    path = tmp_path / 'nested/value.json'
    assert u.read_json(path) == {}
    u.atomic_json(path, {'test': '中文'})
    assert u.read_json(path) == {'test': '中文'}
    assert list(path.parent.glob('*.tmp-*')) == []


def test_atomic_failed_replace_cleans_temp(tmp_path, monkeypatch):
    monkeypatch.setattr(u.os, 'replace', Mock(side_effect=OSError('disk')))
    with pytest.raises(OSError):
        u.atomic_text(tmp_path / 'x', 'value')
    assert list(tmp_path.iterdir()) == []


def test_docker_uses_argv_and_redacts_error(monkeypatch):
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=' ok '))
    monkeypatch.setattr(u.subprocess, 'run', run)
    assert u.docker('inspect', 'my-agent') == 'ok'
    assert run.call_args.args[0] == ['docker', 'inspect', 'my-agent']
    run.return_value = SimpleNamespace(returncode=1, stdout='secret', stderr='secret')
    with pytest.raises(RuntimeError, match='exit=1') as exc:
        u.docker('pull', 'repo:1')
    assert 'secret' not in str(exc.value)


def test_inspect(monkeypatch):
    monkeypatch.setattr(u, 'docker', lambda *a: '[{"Id":"a"}]')
    assert u.inspect_container('a')['Id'] == 'a'


def test_deployment_info_and_overrides(deployment, monkeypatch):
    info, source = deployment
    dep = u.deployment_info()
    assert dep['imageId'] == 'sha256:old'
    assert Path(dep['localComposeFile']) == source
    override = source.with_name('override.yml')
    override.write_text('services:\n  agent:\n    image: registry/agent:override\n', encoding='utf-8')
    info['Config']['Labels']['com.docker.compose.project.config_files'] += ',/host/project/override.yml'
    assert u.deployment_info()['composeFile'].endswith('override.yml')
    info['Config']['Image'] = 'orchidea-agent-rollback:abc'
    u.save_job(u.state_root(), {'previousImage': 'registry/agent:v1', 'previousImageId': 'sha256:old'}, 'rolled_back')
    assert u.deployment_info()['image'] == 'registry/agent:v1'


@pytest.mark.parametrize('problem', ['identity', 'labels', 'missing', 'noimage', 'outside', 'mount'])
def test_deployment_rejects_unsupported(deployment, problem):
    info, source = deployment
    if problem == 'identity': info['Config']['Env'] = []
    if problem == 'labels': info['Config']['Labels'] = {}
    if problem == 'missing': source.unlink()
    if problem == 'noimage': source.write_text('services: {}', encoding='utf-8')
    if problem == 'outside': info['Config']['Labels']['com.docker.compose.project.working_dir'] = '/host/another'
    if problem == 'mount': info['Mounts'] = []
    with pytest.raises(ValueError): u.deployment_info()


def test_runtime_report_and_confirm(deployment, monkeypatch):
    assert u.runtime_info()['selfUpgrade']
    ws = Mock()
    root = u.state_root()
    u.save_job(root, {'requestId': RID, 'targetImageId': 'sha256:old'}, 'verifying')
    u.report(ws)
    frame = json.loads(ws.send.call_args.args[0])
    assert frame['runtime']['imageId'] == 'sha256:old'
    u.confirm({'requestId': RID, 'imageId': 'sha256:bad'})
    assert not (root / 'confirmed.json').exists()
    u.confirm({'requestId': RID, 'imageId': 'sha256:old'})
    assert u.read_json(root / 'confirmed.json')['requestId'] == RID
    ws.send.side_effect = RuntimeError('disconnected')
    u.report(ws)


def test_unsupported_runtime_cached(monkeypatch):
    get = Mock(side_effect=ValueError('unsupported'))
    monkeypatch.setattr(u, 'deployment_info', get)
    assert not u.runtime_info()['selfUpgrade']
    u.runtime_info()
    assert get.call_count == 1


def test_command_gate():
    with u.command_slot(): assert u._active == 1
    assert u._active == 0
    u.save_job(u.state_root(), {}, 'waiting')
    with pytest.raises(RuntimeError, match='正在升级'):
        with u.command_slot(): pass
    u.save_job(u.state_root(), {}, 'success')
    with pytest.raises(ValueError):
        with u.command_slot(): raise ValueError()
    assert u._active == 0


def test_start_spawns_independent_executor(deployment, monkeypatch):
    calls = []
    monkeypatch.setattr(u, 'docker', lambda *args, **kw: calls.append(args) or '')
    u.start_upgrade(Mock(), {'requestId': RID, 'image': 'registry/agent:v2'})
    assert u.job_state()['status'] == 'waiting'
    run = next(c for c in calls if c[0] == 'run')
    assert '-d' in run and 'on-failure:3' in run and 'sha256:old' in run
    assert 'services.agent_upgrade' in run
    assert not any(c[0] in ('stop', 'rm') for c in calls)
    count = len(calls)
    u.start_upgrade(Mock(), {'requestId': RID, 'image': 'registry/agent:v2'})
    assert len(calls) == count  # 重复命令不能起第二个执行器
    u.start_upgrade(Mock(), {'requestId': 'bad'})
    u.start_upgrade(Mock(), {'requestId': RID.replace('1', 'a'), 'image': 'registry/agent:v2'})
    assert len(calls) == count


def test_start_waits_for_active_tasks(deployment, monkeypatch):
    monkeypatch.setattr(u, '_active', 1)
    slept = []
    def sleep(n):
        slept.append(n)
        u._active = 0
    monkeypatch.setattr(u.time, 'sleep', sleep)
    monkeypatch.setattr(u, 'docker', lambda *a, **k: '')
    u.start_upgrade(Mock(), {'requestId': RID, 'image': 'registry/agent:v2'})
    assert slept == [1]


def test_start_wait_timeout(deployment, monkeypatch):
    monkeypatch.setattr(u, '_active', 1)
    times = iter([0, 601])
    monkeypatch.setattr(u.time, 'monotonic', lambda: next(times))
    u.start_upgrade(Mock(), {'requestId': RID, 'image': 'registry/agent:v2'})
    assert u.job_state()['status'] == 'failed'


def test_start_rejects_different_repository(deployment):
    u.start_upgrade(Mock(), {'requestId': RID, 'image': 'evil/agent:v2'})
    assert u.job_state()['status'] == 'failed'


@pytest.mark.parametrize('running,owned', [(True, True), (False, False), (False, True)])
def test_previous_executor(deployment, monkeypatch, running, owned):
    dep = u.deployment_info()
    monkeypatch.setattr(u, 'deployment_info', lambda: dep)
    monkeypatch.setattr(u, 'inspect_container', lambda n: {'State': {'Running': running}, 'Config': {'Labels': {'orchidea.agent-upgrader': u.state_root().name if owned else 'foreign'}}})
    calls = []
    monkeypatch.setattr(u, 'docker', lambda *a, **k: calls.append(a) or ('old' if a[0] == 'ps' else ''))
    u.start_upgrade(Mock(), {'requestId': RID, 'image': 'registry/agent:v2'})
    assert u.job_state()['status'] == ('waiting' if owned and not running else 'failed')
    assert any(c[0] == 'rm' for c in calls) == (owned and not running)


@pytest.fixture
def execution(tmp_path, monkeypatch):
    root = tmp_path / 'task'
    root.mkdir()
    source = tmp_path / 'compose.yml'
    source.write_text('services:\n  agent:\n    image: registry/agent:v1\n  business:\n    image: business:1\n', encoding='utf-8')
    manifest = {'root': str(root), 'requestId': RID, 'targetImage': 'registry/agent:v2',
                'composeHash': hashlib.sha256(source.read_bytes()).hexdigest(),
                'imageId': 'sha256:old', 'composeFile': str(source), 'composeFiles': [str(source)],
                'service': 'agent', 'projectDir': str(tmp_path), 'project': 'test', 'containerName': 'agent'}
    u.atomic_json(root / 'manifest.json', manifest)
    u.save_job(root, {'requestId': RID}, 'waiting')
    calls = []
    def docker(*args, **kwargs):
        calls.append(args)
        if args[:2] == ('image', 'inspect'):
            return json.dumps([{'Id': 'sha256:new', 'RepoDigests': ['registry/agent@sha256:abcd']}])
        return ''
    monkeypatch.setattr(u, 'docker', docker)
    monkeypatch.setattr(u, 'wait_ready', lambda *a, **k: True)
    return manifest, root, source, calls


def test_executor_success_only_touches_agent(execution):
    manifest, root, source, calls = execution
    u.run_upgrade(root / 'manifest.json')
    assert u.read_json(root / 'job.json')['status'] == 'success'
    compose_calls = [c for c in calls if c[0] == 'compose']
    assert len(compose_calls) == 1
    assert compose_calls[0][-1] == 'agent'
    assert '--no-deps' in compose_calls[0] and '--pull' in compose_calls[0]
    content = yaml.safe_load(source.read_text())
    assert content['services']['business']['image'] == 'business:1'
    assert content['services']['agent']['image'] == 'registry/agent@sha256:abcd'
    u.run_upgrade(root / 'manifest.json')
    assert len([c for c in calls if c[0] == 'compose']) == 1


@pytest.mark.parametrize('phase', ['waiting', 'switching', 'verifying', 'rolling_back'])
def test_executor_failure_and_interruption_restore_old(execution, monkeypatch, phase):
    manifest, root, source, calls = execution
    if phase != 'waiting':
        u.atomic_text(root / 'compose.before.yml', source.read_text())
        u.save_job(root, {'requestId': RID}, phase)
    monkeypatch.setattr(u, 'wait_ready', lambda m, image, *a, **k: image == 'sha256:old')
    u.run_upgrade(root / 'manifest.json')
    assert u.read_json(root / 'job.json')['status'] == 'rolled_back'
    assert yaml.safe_load(source.read_text())['services']['agent']['image'].startswith('orchidea-agent-rollback:')
    assert any(c[:2] == ('tag', 'sha256:old') for c in calls)


def test_failed_pull_does_not_stop_old(execution, monkeypatch):
    manifest, root, source, calls = execution
    def fail(*a, **k):
        calls.append(a)
        if a[0] == 'pull': raise RuntimeError('pull failed')
        return ''
    monkeypatch.setattr(u, 'docker', fail)
    u.run_upgrade(root / 'manifest.json')
    assert u.read_json(root / 'job.json')['status'] == 'failed'
    assert not any(c[0] == 'compose' for c in calls)


def test_missing_digest_and_failed_recovery(execution, monkeypatch):
    manifest, root, source, calls = execution
    monkeypatch.setattr(u, 'docker', lambda *a, **k: '[{"Id":"new"}]')
    u.run_upgrade(root / 'manifest.json')
    assert u.read_json(root / 'job.json')['status'] == 'failed'
    u.save_job(root, {'requestId': RID}, 'switching')
    u.run_upgrade(root / 'manifest.json')
    assert u.read_json(root / 'job.json')['status'] == 'unknown'  # 缺备份不能冒充恢复成功


def test_wait_ready_requires_both_image_and_hub_confirmation(execution, monkeypatch):
    manifest, root, source, calls = execution
    wait = u.wait_ready  # fixture 替换过，取原函数在下方通过保存引用
    monkeypatch.setattr(u, 'inspect_container', lambda n: {'Image': 'sha256:new', 'State': {'Running': True, 'Health': {'Status': 'healthy'}}})
    u.atomic_json(root / 'confirmed.json', {'requestId': RID, 'imageId': 'sha256:new'})
    assert REAL_WAIT(manifest, 'sha256:new', root, True, timeout=1)
    assert REAL_WAIT(manifest, 'sha256:new', root, False, timeout=1)
    times = iter([0, 0, 3])
    monkeypatch.setattr(u.time, 'monotonic', lambda: next(times))
    monkeypatch.setattr(u.time, 'sleep', lambda n: None)
    assert not REAL_WAIT(manifest, 'sha256:wrong', root, True, timeout=1)


def test_wait_ready_without_healthcheck(execution, monkeypatch):
    manifest, root, source, calls = execution
    monkeypatch.setattr(u, 'inspect_container', lambda n: {'Image': 'sha256:new', 'State': {'Running': True}})
    assert REAL_WAIT(manifest, 'sha256:new', root, False, timeout=1)
    assert any(c[0] == 'exec' for c in calls)
    monkeypatch.setattr(u, 'inspect_container', Mock(side_effect=RuntimeError('not created')))
    times = iter([0, 0, 3])
    monkeypatch.setattr(u.time, 'monotonic', lambda: next(times))
    monkeypatch.setattr(u.time, 'sleep', lambda n: None)
    assert not REAL_WAIT(manifest, 'sha256:new', root, False, timeout=1)


REAL_WAIT = u.wait_ready


def test_start_with_registry_credentials(deployment, monkeypatch):
    info, source = deployment
    auth = source.parent / 'docker-auth'
    auth.mkdir()
    monkeypatch.setenv('DOCKER_CONFIG', str(auth))
    calls = []
    monkeypatch.setattr(u, 'docker', lambda *a, **k: calls.append(a) or '')
    u.start_upgrade(Mock(), {'requestId': RID, 'image': 'registry/agent:v2'})
    assert any('/root/.docker:ro' in value for call in calls for value in call)


def test_rollback_health_unconfirmed(execution, monkeypatch):
    manifest, root, source, calls = execution
    monkeypatch.setattr(u, 'wait_ready', lambda *a, **k: False)
    u.run_upgrade(root / 'manifest.json')
    assert u.read_json(root / 'job.json')['status'] == 'unknown'


def test_config_changed_during_download(execution):
    manifest, root, source, calls = execution
    source.write_text('services: {}', encoding='utf-8')
    u.run_upgrade(root / 'manifest.json')
    assert u.read_json(root / 'job.json')['status'] == 'failed'
    assert not any(c[0] == 'compose' for c in calls)


def test_main(monkeypatch):
    run = Mock()
    monkeypatch.setattr(sys, 'argv', ['updater', '--run', 'manifest.json'])
    monkeypatch.setattr(u, 'run_upgrade', run)
    u.main()
    run.assert_called_once_with('manifest.json')


def test_readonly_credentials_mount():
    mounts = [{'Type': 'bind', 'Source': '/host/auth', 'Destination': '/auth', 'RW': False}]
    with pytest.raises(ValueError): u.mapped_path('/auth', mounts, to_host=True)
    assert str(u.mapped_path('/auth', mounts, to_host=True, require_write=False)) == str(Path('/host/auth'))


@pytest.mark.parametrize('existing', [False, True])
def test_reconnect_recovers_waiting_without_duplicate_executor(deployment, monkeypatch, existing):
    dep = u.deployment_info()
    monkeypatch.setattr(u, 'deployment_info', lambda: dep)
    u.save_job(u.state_root(), {'requestId': RID, 'targetImage': 'registry/agent:v2'}, 'waiting')
    calls = []
    monkeypatch.setattr(u, 'docker', lambda *a, **k: calls.append(a) or ('helper' if existing and a[0] == 'ps' else ''))
    monkeypatch.setattr(u, 'inspect_container', lambda n: {'State': {'Running': True}})
    u.on_connected(Mock())
    assert any(c[0] == 'run' for c in calls) is (not existing)
    assert u.job_state()['seq'] == (1 if existing else 2)


def test_reconnect_docker_unavailable_preserves_task(monkeypatch):
    u.save_job(u.state_root(), {'requestId': RID, 'targetImage': 'registry/agent:v2'}, 'waiting')
    monkeypatch.setattr(u, 'runtime_info', lambda: {})
    monkeypatch.setattr(u, 'docker', Mock(side_effect=RuntimeError('offline')))
    u.on_connected(Mock())
    assert u.job_state()['status'] == 'waiting'


@pytest.mark.parametrize('failure', [subprocess.TimeoutExpired('docker run', 60), RuntimeError('socket disconnected')])
def test_lost_launch_response_preserves_executor_progress(deployment, monkeypatch, failure):
    """daemon 已启动 helper 后 CLI 报错，不能把真实进度覆盖为 failed 或释放服务操作。"""
    ws = Mock()
    def lost_response(*args, **kwargs):
        if args[0] == 'run':
            job = u.job_state()
            u.save_job(u.state_root(), job, 'pulling')
            u.save_job(u.state_root(), job, 'pulling', targetImageId='sha256:new')
            raise failure
        return ''
    monkeypatch.setattr(u, 'docker', lost_response)
    u.start_upgrade(ws, {'requestId': RID, 'image': 'registry/agent:v2'})
    job = u.job_state()
    assert (job['status'], job['seq'], job['targetImageId']) == ('pulling', 3, 'sha256:new')
    frames = [json.loads(call.args[0])['upgrade'] for call in ws.send.call_args_list]
    assert [frame['status'] for frame in frames] == ['waiting', 'pulling']
    assert frames[-1]['targetImageId'] == 'sha256:new'
    with pytest.raises(RuntimeError):
        with u.command_slot(): pass
    u.save_job(u.state_root(), job, 'success')
    u.report(ws)
    assert json.loads(ws.send.call_args.args[0])['upgrade']['status'] == 'success'


def test_unconfirmed_launch_retries_without_rewriting_job(deployment, monkeypatch):
    calls = []
    failed = True
    def docker(*args, **kwargs):
        calls.append(args)
        if args[0] == 'run' and failed:
            raise subprocess.TimeoutExpired('docker run', 60)
        return ''
    monkeypatch.setattr(u, 'docker', docker)
    u.start_upgrade(Mock(), {'requestId': RID, 'image': 'registry/agent:v2'})
    before = u.job_state()
    assert before['status'] == 'waiting'
    failed = False
    u.on_connected(Mock())
    assert u.job_state() == before
    runs = [call for call in calls if call[0] == 'run']
    assert len(runs) == 2 and runs[0] == runs[1]
    assert 'orchidea.agent-upgrade-request=' + RID in runs[0]


@pytest.mark.parametrize('status', ['running', 'restarting', 'created', 'exited'])
def test_existing_executor_is_confirmed_by_request_and_never_recreated(deployment, monkeypatch, status):
    monkeypatch.setattr(u, 'docker', lambda *a, **k: '')
    u.start_upgrade(Mock(), {'requestId': RID, 'image': 'registry/agent:v2'})
    before = u.job_state()
    calls = []
    monkeypatch.setattr(u, 'docker', lambda *a, **k: calls.append(a) or ('id' if a[0] == 'ps' else ''))
    info = {'Config': {'Labels': {'orchidea.agent-upgrader': u.state_root().name, 'orchidea.agent-upgrade-request': RID}},
            'State': {'Running': status in ('running', 'restarting'), 'Status': status}}
    monkeypatch.setattr(u, 'inspect_container', lambda name: info)
    assert u.recover_launch()
    assert u.job_state() == before
    assert not any(call[0] == 'run' for call in calls)
    assert any(call[0] == 'start' for call in calls) is (status in ('created', 'exited'))


def test_wrong_executor_or_unavailable_inspect_keeps_task_locked(deployment, monkeypatch):
    monkeypatch.setattr(u, 'docker', lambda *a, **k: '')
    u.start_upgrade(Mock(), {'requestId': RID, 'image': 'registry/agent:v2'})
    before = u.job_state()
    calls = []
    monkeypatch.setattr(u, 'docker', lambda *a, **k: calls.append(a) or 'existing')
    monkeypatch.setattr(u, 'inspect_container', lambda n: {'Config': {'Labels': {}}, 'State': {'Running': False, 'Status': 'created'}})
    u.recover_launch()
    monkeypatch.setattr(u, 'inspect_container', Mock(side_effect=RuntimeError('daemon unavailable')))
    u.recover_launch()
    assert u.job_state() == before
    assert all(call[0] == 'ps' for call in calls)


def test_handoff_fsync_error_does_not_clobber_started_executor(deployment, monkeypatch):
    atomic = u.atomic_json
    def fail_after_publish(path, value):
        atomic(path, value)
        if Path(path).name == 'launch.json':
            job = u.job_state()
            u.save_job(u.state_root(), job, 'pulling', targetImageId='sha256:new')
            raise OSError('directory fsync failed after publishing intent')
    monkeypatch.setattr(u, 'atomic_json', fail_after_publish)
    monkeypatch.setattr(u, 'docker', lambda *a, **k: '')
    u.start_upgrade(Mock(), {'requestId': RID, 'image': 'registry/agent:v2'})
    assert u.job_state()['status'] == 'pulling'
    assert u.job_state()['targetImageId'] == 'sha256:new'


def test_mismatched_manifest_never_starts_executor(deployment, monkeypatch):
    monkeypatch.setattr(u, 'docker', lambda *a, **k: '')
    u.start_upgrade(Mock(), {'requestId': RID, 'image': 'registry/agent:v2'})
    u.atomic_json(u.state_root() / 'manifest.json', {'requestId': 'different'})
    docker = Mock()
    monkeypatch.setattr(u, 'docker', docker)
    assert u.recover_launch()
    docker.assert_not_called()
    assert u.job_state()['status'] == 'waiting'


COMMENTED_COMPOSE = '''# 部署说明
services:
  agent:
    # 镜像由升级任务维护
    image: ${SERVICE_AGENT_IMAGE:?required}  # 行尾注释
    ports:
      - 22:22
    environment:
      FLAG: on
      UMASK: 0022
  business:
    image: business:1
'''


def test_replace_image_text_only_touches_agent_image_line():
    result = u.replace_image_text(COMMENTED_COMPOSE, 'agent', 'registry/agent@sha256:abcd')
    before, after = COMMENTED_COMPOSE.splitlines(), result.splitlines()
    changed = [(a, b) for a, b in zip(before, after) if a != b]
    assert changed == [('    image: ${SERVICE_AGENT_IMAGE:?required}  # 行尾注释', '    image: "registry/agent@sha256:abcd"')]
    # 未加引号的值保持原文：整份重新序列化会把 22:22、on、0022 按 YAML 1.1 改写
    assert '      - 22:22' in after and '      FLAG: on' in after and '      UMASK: 0022' in after
    assert yaml.safe_load(result)['services']['business']['image'] == 'business:1'


def test_replace_image_text_keeps_crlf_and_quoted_service_key():
    text = 'services:\r\n  "agent":\r\n    restart: always\r\n    image: old:1\r\n'
    result = u.replace_image_text(text, 'agent', 'registry/agent:v2')
    assert result == 'services:\r\n  "agent":\r\n    restart: always\r\n    image: "registry/agent:v2"\r\n'


@pytest.mark.parametrize('text', [
    'services: {agent: {image: old:1}}\n',          # 流样式无法逐行定位
    'services:\n  other:\n    image: old:1\n',     # 找不到目标服务
    'services:\n  agent:\n    build: .\n',          # 服务没有 image 行
    'volumes:\n  agent:\n    image: old:1\n',       # 不在 services 下
])
def test_replace_image_text_rejects_unlocatable_image(text):
    with pytest.raises(ValueError, match='image'):
        u.replace_image_text(text, 'agent', 'registry/agent:v2')


def test_flow_style_compose_disables_self_upgrade(deployment, monkeypatch):
    info, source = deployment
    source.write_text('services: {agent: {image: "registry/agent:v1"}}\n', encoding='utf-8')
    runtime = u.runtime_info()
    assert runtime['selfUpgrade'] is False
    assert 'image' in runtime['reason']


def test_failed_runtime_probe_is_retried(monkeypatch):
    get = Mock(side_effect=[ValueError('docker busy'), {'image': 'registry/agent:v1', 'imageId': 'sha256:old'}])
    monkeypatch.setattr(u, 'deployment_info', get)
    now = [1000.0]
    monkeypatch.setattr(u.time, 'monotonic', lambda: now[0])
    assert u.runtime_info()['selfUpgrade'] is False
    now[0] += u.RUNTIME_RETRY_SECONDS - 1
    assert u.runtime_info()['selfUpgrade'] is False
    now[0] += 1
    assert u.runtime_info()['selfUpgrade'] is True
    now[0] += 3600
    u.runtime_info()
    assert get.call_count == 2  # 成功结果一直缓存


def _stale_job(status, **extra):
    job = {'requestId': RID, 'targetImage': 'registry/agent:v2', 'targetImageId': 'sha256:new',
           'previousImageId': 'sha256:old', **extra}
    u.save_job(u.state_root(), job, status)
    stored = u.job_state()
    stored['updatedAt'] -= u.STALE_JOB_SECONDS + 1
    u.atomic_json(u.state_root() / 'job.json', stored)


@pytest.mark.parametrize('image_id,expected', [
    ('sha256:new', 'success'), ('sha256:old', 'rolled_back'), ('sha256:other', 'failed'),
])
@pytest.mark.parametrize('status', ['unknown', 'verifying', 'switching', 'rolling_back', 'pulling'])
def test_reconcile_stale_job_converges_by_running_image(monkeypatch, status, image_id, expected):
    _stale_job(status)
    monkeypatch.setattr(u, 'runtime_info', lambda: {'imageId': image_id})
    monkeypatch.setattr(u, 'docker', lambda *a, **k: '')  # 执行器已不存在
    assert u.reconcile_stale_job() is True
    job = u.job_state()
    assert job['status'] == expected
    # seq 递增，Hub 按上报序号接受收敛结果
    assert job['seq'] == 2
    with u.command_slot():
        pass


@pytest.mark.parametrize('state', [{'Running': True}, {'Running': False, 'Restarting': True},
                                   {'Running': False, 'Status': 'restarting'}])
def test_reconcile_keeps_job_while_executor_alive(monkeypatch, state):
    _stale_job('verifying')
    monkeypatch.setattr(u, 'runtime_info', lambda: {'imageId': 'sha256:new'})
    monkeypatch.setattr(u, 'docker', lambda *a, **k: 'helper-id')
    monkeypatch.setattr(u, 'inspect_container', lambda name: {'State': state})
    assert u.reconcile_stale_job() is False
    assert u.job_state()['status'] == 'verifying'


def test_reconcile_exited_executor_and_skips_fresh_or_terminal(monkeypatch):
    _stale_job('unknown')
    monkeypatch.setattr(u, 'runtime_info', lambda: {'imageId': 'sha256:old'})
    monkeypatch.setattr(u, 'docker', lambda *a, **k: 'helper-id')
    monkeypatch.setattr(u, 'inspect_container', lambda name: {'State': {'Running': False, 'Status': 'exited'}})
    assert u.reconcile_stale_job() is True
    assert u.job_state()['status'] == 'rolled_back'
    assert u.reconcile_stale_job() is False  # 终态不再改写
    u.save_job(u.state_root(), {'requestId': RID}, 'verifying')  # 刚更新过：执行器可能仍在推进
    assert u.reconcile_stale_job() is False
    u.save_job(u.state_root(), {'requestId': RID}, 'waiting')  # waiting 由 recover_launch 负责
    assert u.reconcile_stale_job() is False


def test_reconcile_waits_when_docker_unavailable(monkeypatch):
    _stale_job('unknown')
    monkeypatch.setattr(u, 'docker', Mock(side_effect=RuntimeError('daemon unavailable')))
    assert u.reconcile_stale_job() is False
    assert u.job_state()['status'] == 'unknown'


def test_reconnect_reconciles_before_reporting(monkeypatch):
    _stale_job('unknown')
    monkeypatch.setattr(u, 'runtime_info', lambda: {'imageId': 'sha256:new', 'protocol': 1})
    monkeypatch.setattr(u, 'docker', lambda *a, **k: '')
    ws = Mock()
    u.on_connected(ws)
    frame = json.loads(ws.send.call_args_list[0].args[0])
    assert frame['upgrade']['status'] == 'success'


def test_launch_intent_write_failure_marks_job_failed(deployment, monkeypatch):
    atomic = u.atomic_json
    def fail_launch(path, value):
        if Path(path).name == 'launch.json':
            raise OSError('disk full')
        atomic(path, value)
    monkeypatch.setattr(u, 'atomic_json', fail_launch)
    calls = []
    monkeypatch.setattr(u, 'docker', lambda *a, **k: calls.append(a) or '')
    u.start_upgrade(Mock(), {'requestId': RID, 'image': 'registry/agent:v2'})
    job = u.job_state()
    assert job['status'] == 'failed' and 'disk full' in job['error']
    assert not any(c[0] == 'run' for c in calls)
    with u.command_slot():
        pass


def test_rollback_readiness_does_not_require_hub_connection(execution, monkeypatch):
    manifest, root, source, calls = execution
    # 旧 Agent 未连上 Hub：Docker 健康检查因 /health 503 判 unhealthy，但进程已在响应
    monkeypatch.setattr(u, 'inspect_container', lambda n: {'Image': 'sha256:old', 'State': {'Running': True, 'Health': {'Status': 'unhealthy'}}})
    assert REAL_WAIT(manifest, 'sha256:old', root, False, timeout=1)
    probe = next(c for c in calls if c[0] == 'exec')
    assert probe[-1] == u.LIVENESS_PROBE and 'HTTPError' in u.LIVENESS_PROBE
    times = iter([0, 0, 3])
    monkeypatch.setattr(u.time, 'monotonic', lambda: next(times))
    monkeypatch.setattr(u.time, 'sleep', lambda n: None)
    # 新版仍须通过 Docker 健康检查并获得 Hub 确认
    monkeypatch.setattr(u, 'inspect_container', lambda n: {'Image': 'sha256:new', 'State': {'Running': True, 'Health': {'Status': 'unhealthy'}}})
    u.atomic_json(root / 'confirmed.json', {'requestId': RID, 'imageId': 'sha256:new'})
    assert not REAL_WAIT(manifest, 'sha256:new', root, True, timeout=1)


def test_executor_rewrite_keeps_compose_comments(execution):
    manifest, root, source, calls = execution
    source.write_text(COMMENTED_COMPOSE, encoding='utf-8')
    manifest['composeHash'] = hashlib.sha256(source.read_bytes()).hexdigest()
    u.atomic_json(root / 'manifest.json', manifest)
    u.run_upgrade(root / 'manifest.json')
    assert u.read_json(root / 'job.json')['status'] == 'success'
    text = source.read_text(encoding='utf-8')
    assert '# 部署说明' in text and '      - 22:22' in text and '      FLAG: on' in text
    assert yaml.safe_load(text)['services']['agent']['image'] == 'registry/agent@sha256:abcd'


def test_replace_image_text_skips_non_service_lines_and_verifies_result():
    text = 'services:\n  x-note: >\n    image: not-a-service\n  agent:\n    image: old:1\n'
    result = u.replace_image_text(text, 'agent', 'registry/agent:v2')
    assert yaml.safe_load(result)['services']['agent']['image'] == 'registry/agent:v2'
    assert '    image: not-a-service\n' in result
    # 重复键时 YAML 取最后一个值：替换第一行不能生效，必须拒绝而不是假装成功
    with pytest.raises(ValueError):
        u.replace_image_text('services:\n  agent:\n    image: old:1\n    image: other:2\n', 'agent', 'registry/agent:v2')
