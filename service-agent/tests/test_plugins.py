import copy
import json
from pathlib import Path
from types import SimpleNamespace
import time
import uuid

import pytest

from services import plugin_files as files, plugins


@pytest.fixture
def setup(tmp_path, monkeypatch):
    project = tmp_path / 'app'
    root = project / 'plugins'
    directory = root / '@business/plugin-test'
    directory.mkdir(parents=True)
    (project / 'docker-compose.yaml').write_text('services: {}')
    (directory / 'package.json').write_text('{"name":"@business/plugin-test","version":"1.0.0"}')
    files.atomic_json(root / files.REPORT_FILE, {'schemaVersion': 1, 'lockProtocol': 'flock-v1', 'runId': 'r1',
                    'startedAt': files.utc_stamp(time.time()-20), 'finishedAt': files.utc_stamp(time.time()-10), 'status': 'success', 'items': []})
    info = {'Id': 'a'*64, 'Name': '/app', 'Config': {'WorkingDir': '/app/nocobase', 'Env': []},
            'State': {'Running': True, 'StartedAt': files.utc_stamp(time.time()-60)},
            'Mounts': [{'Type': 'bind', 'Source': str(root), 'Destination': '/app/nocobase/storage/plugins', 'RW': True}]}
    services = [{'name': 'app', 'containerId': info['Id'], 'containerName': 'app', 'state': 'running', 'startedAt': info['State']['StartedAt']}]
    monkeypatch.setattr(plugins, 'PROJECTS_ROOT', str(tmp_path))
    monkeypatch.setattr(plugins, 'all_containers', lambda: [info])
    monkeypatch.setattr(plugins, 'collect_service_statuses', lambda p: services)
    return SimpleNamespace(root=root, project=project, info=info, services=services)


def test_inventory_resolves_mount_and_current_boot(setup):
    result = plugins.collect_inventory(setup.info, [setup.info])
    assert result['root'] == str(setup.root)
    assert result['entries'][0]['version'] == '1.0.0'
    assert result['syncCurrent'] is True and result['mutable'] is True
    assert plugins.enrich_statuses(setup.services)[0]['plugins']['containerId'] == setup.info['Id']


def test_stale_report_is_not_current_attempt(setup):
    setup.info['State']['StartedAt'] = files.utc_stamp()
    result = plugins.collect_inventory(setup.info, [setup.info])
    assert not result['syncCurrent'] and not result['mutable']
    assert result['entries']


def test_missing_report_keeps_inventory_readonly(setup):
    (setup.root / files.REPORT_FILE).unlink()
    result = plugins.collect_inventory(setup.info, [setup.info])
    assert result['status'] == 'ok' and result['entries']
    assert not result['mutable']


def test_shared_storage_cannot_be_deleted_as_one_instance(setup):
    other = copy.deepcopy(setup.info)
    other.update(Id='b'*64, Name='/other')
    result = plugins.collect_inventory(setup.info, [setup.info, other])
    assert result['sharedWith'] == ['other']
    assert not result['mutable']


def test_shared_node_modules_also_blocks_mutation(setup, tmp_path):
    shared = tmp_path / 'shared-modules'
    shared.mkdir()
    module_mount = {'Source': str(shared), 'Destination': '/app/nocobase/node_modules', 'RW': True}
    setup.info['Mounts'].append(module_mount)
    other = copy.deepcopy(setup.info)
    other_root = tmp_path / 'other-plugins'
    other_root.mkdir()
    other.update(Id='b'*64, Name='/other')
    other['Mounts'][0]['Source'] = str(other_root)
    result = plugins.collect_inventory(setup.info, [setup.info, other])
    assert result['sharedWith'] == ['other'] and not result['mutable']


def test_readonly_mount_blocks_delete(setup):
    setup.info['Mounts'][0]['RW'] = False
    result = plugins.collect_inventory(setup.info, [setup.info])
    assert not result['mutable'] and '只读' in result['mutationReason']


def test_running_report_requires_live_lock_to_be_in_progress(setup):
    report, _, _ = files.read_json(setup.root / files.REPORT_FILE)
    report.update(status='running', finishedAt=None)
    files.atomic_json(setup.root / files.REPORT_FILE, report)
    result = plugins.collect_inventory(setup.info, [setup.info])
    assert result['syncInterrupted'] and not result['busy']
    with files.storage_lock(setup.root):
        result = plugins.collect_inventory(setup.info, [setup.info])
        assert result['busy'] and not result['syncInterrupted']


def test_scan_delete_restore_uses_real_project_and_container(setup, monkeypatch):
    calls = []
    def run(args):
        calls.append(args)
        return '{"removed":true}'
    monkeypatch.setattr(plugins, '_run', run)
    result, services = plugins.execute_plugin_operation(str(setup.project), 'plugin_scan', {}, str(uuid.uuid4()))
    entry = services[0]['plugins']['entries'][0]
    payload = {'containerId': setup.info['Id'], 'pluginName': entry['name'], 'fingerprint': entry['fingerprint']}
    removed, report = plugins.execute_plugin_operation(str(setup.project), 'plugin_remove', payload, str(uuid.uuid4()))
    assert not report[0]['plugins']['entries']
    assert calls[0][0:2] == ['exec', setup.info['Id']]
    assert 'check-link' in calls[0]
    assert 'remove' in calls[1]
    restored, report = plugins.execute_plugin_operation(str(setup.project), 'plugin_restore', {'containerId': setup.info['Id'], 'trashId': removed['trashId']}, str(uuid.uuid4()))
    assert report[0]['plugins']['entries'][0]['name'] == entry['name']
    assert 'restore' in calls[-1]


def test_batch_reuses_docker_inspect_mounts_and_inventory(setup, monkeypatch):
    from unittest.mock import Mock
    import copy
    other = copy.deepcopy(setup.info)
    other['Id'] = 'b'*64
    other['Name'] = '/second'
    docker = Mock(return_value=[setup.info, other])
    mount = Mock(wraps=plugins.storage_mount)
    scan = Mock(wraps=plugins.collect_inventory)
    monkeypatch.setattr(plugins, 'all_containers', docker)
    monkeypatch.setattr(plugins, 'storage_mount', mount)
    monkeypatch.setattr(plugins, 'collect_inventory', scan)
    result = plugins.enrich_statuses(setup.services + setup.services)
    assert len(result) == 2
    assert docker.call_count == 1 and mount.call_count == 2 and scan.call_count == 1


def test_docker_timeout_does_not_expose_embedded_script(monkeypatch):
    def timeout(*args, **kwargs):
        raise plugins.subprocess.TimeoutExpired(['docker','exec','node','SECRET SCRIPT'],30)
    monkeypatch.setattr(plugins.subprocess,'run',timeout)
    with pytest.raises(files.PluginFileError, match='容器响应超时') as error:
        plugins._run(['ps'])
    assert 'SECRET' not in str(error.value)


@pytest.mark.parametrize('container_id', ['x', 'b'*64, '', None])
def test_foreign_or_replaced_container_is_rejected(setup, container_id):
    with pytest.raises(files.PluginFileError):
        plugins.execute_plugin_operation(str(setup.project), 'plugin_remove', {'containerId': container_id}, str(uuid.uuid4()))


def test_stopped_container_and_unmanaged_project_are_rejected(setup, tmp_path):
    setup.info['State']['Running'] = False
    with pytest.raises(files.PluginFileError, match='未运行'):
        plugins.execute_plugin_operation(str(setup.project), 'plugin_remove', {'containerId': setup.info['Id']}, str(uuid.uuid4()))
    with pytest.raises(files.PluginFileError):
        plugins.execute_plugin_operation(str(tmp_path.parent), 'plugin_scan', {}, str(uuid.uuid4()))


def test_unsupported_or_unknown_operation_fails_explicitly(setup):
    with pytest.raises(files.PluginFileError, match='不支持'):
        plugins.execute_plugin_operation(str(setup.project), 'unknown', {'containerId': setup.info['Id']}, str(uuid.uuid4()))
    setup.info['Mounts'] = []
    with pytest.raises(files.PluginFileError, match='不支持'):
        plugins.execute_plugin_operation(str(setup.project), 'plugin_remove', {'containerId': setup.info['Id']}, str(uuid.uuid4()))
    with pytest.raises(files.PluginFileError, match='未发现'):
        plugins.execute_plugin_operation(str(setup.project), 'plugin_scan', {}, str(uuid.uuid4()))


def test_parent_mount_and_configured_storage_path(setup):
    root = setup.project / 'custom/plugins'
    root.mkdir(parents=True)
    (setup.project / 'sync-plugins.config.json').write_text('{"storagePath":"custom/plugins","nodeModulesPath":"custom/modules","agentSecret":"DO-NOT-REPORT"}')
    setup.info['Mounts'] = [{'Source': str(setup.project), 'Destination': '/app/nocobase', 'RW': True}]
    result = plugins.collect_inventory(setup.info, [setup.info])
    assert result['root'] == str(root)
    assert result['nodeModulesPath'] == '/app/nocobase/custom/modules'
    assert 'DO-NOT-REPORT' not in json.dumps(result)


def test_environment_storage_override_and_path_rejection(setup):
    setup.info['Config']['Env'] = ['PLUGIN_STORAGE_PATH=/app/nocobase/storage/plugins', 'NODE_MODULES_PATH=/app/modules']
    assert plugins.storage_mount(setup.info)['nodeModulesPath'] == '/app/modules'
    setup.info['Config']['Env'] = ['PLUGIN_STORAGE_PATH=../escape']
    assert plugins.collect_inventory(setup.info, [setup.info])['status'] == 'error'
    setup.info['Config']['Env'] = []
    setup.info['Mounts'][0]['Source'] = '/outside-managed-root'
    assert plugins.collect_inventory(setup.info, [setup.info])['status'] == 'error'


def test_bad_config_is_visible_as_read_error(setup):
    setup.info['Mounts'] = [{'Source': str(setup.project), 'Destination': '/app/nocobase', 'RW': True}]
    (setup.project / 'sync-plugins.config.json').write_text('broken')
    assert plugins.collect_inventory(setup.info, [setup.info])['status'] == 'error'


def test_failed_docker_scan_is_not_reported_as_success(setup, monkeypatch):
    def fail():
        raise OSError('Docker down')
    monkeypatch.setattr(plugins, 'all_containers', fail)
    assert plugins.enrich_statuses(setup.services)[0]['plugins']['status'] == 'error'
    with pytest.raises(files.PluginFileError):
        plugins.execute_plugin_operation(str(setup.project), 'plugin_scan', {}, str(uuid.uuid4()))
    assert plugins.enrich_statuses([{'containerId': 'invalid'}]) == [{'containerId': 'invalid'}]


@pytest.mark.parametrize('response', [SimpleNamespace(returncode=1, stdout=''), SimpleNamespace(returncode=0, stdout='x'*(8*1024*1024+1))])
def test_docker_failure_and_oversized_output_are_bounded(monkeypatch, response):
    monkeypatch.setattr(plugins.subprocess, 'run', lambda *a, **k: response)
    with pytest.raises(files.PluginFileError):
        plugins._run(['ps'])


def test_all_container_listing_validates_ids_and_json(monkeypatch):
    monkeypatch.setattr(plugins, '_run', lambda args: '' if args[0]=='ps' else '[]')
    assert plugins.all_containers() == []
    monkeypatch.setattr(plugins, '_run', lambda args: 'invalid')
    with pytest.raises(files.PluginFileError):
        plugins.all_containers()
    monkeypatch.setattr(plugins, '_run', lambda args: 'a'*64 if args[0]=='ps' else '{}')
    with pytest.raises(files.PluginFileError):
        plugins.all_containers()
    monkeypatch.setattr(plugins, '_run', lambda args: 'a'*64 if args[0]=='ps' else '[{"Id":"a"}]')
    assert plugins.all_containers() == [{'Id':'a'}]
