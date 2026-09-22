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
