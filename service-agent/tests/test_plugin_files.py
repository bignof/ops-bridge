import json
import os
from pathlib import Path
import time
import uuid

import pytest

from services import plugin_files as files


@pytest.fixture
def root(tmp_path):
    value = tmp_path / 'plugins'
    value.mkdir()
    return value


def put(root, name='@business/plugin-test', version='1.0.0'):
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'package.json').write_text(json.dumps({'name': name, 'version': version}), encoding='utf-8')
    (directory / 'keep.txt').write_text('payload', encoding='utf-8')
    return directory


def remove(root, name='@business/plugin-test', **kwargs):
    return files.remove_plugin(root, name, files.plugin_entry(root, name)['fingerprint'], str(uuid.uuid4()), **kwargs)


def test_scan_excludes_backups_and_reports_broken_packages(root):
    put(root)
    backup = root / '@business/.bak-plugin-test'
    backup.mkdir()
    (backup / 'package.json').write_text('{"name":"@business/plugin-test","version":"old"}')
    put(root, 'plain-plugin', '2')
    broken = root / '@business/broken'
    broken.mkdir()
    (broken / 'package.json').write_text('not json')
    result = files.scan_plugins(root)
    assert [(p['name'], p['version']) for p in result['entries']] == [('@business/plugin-test', '1.0.0'), ('plain-plugin', '2')]
    assert result['excludedCount'] == 1
    assert result['errors'][0]['name'] == '@business/broken'
    assert result['sync'] is None


def test_delete_restore_and_duplicate_requests_keep_payload(root):
    put(root)
    name = '@business/plugin-test'
    original = files.plugin_entry(root, name)
    request_id = str(uuid.uuid4())
    calls = []
    removed = files.remove_plugin(root, name, original['fingerprint'], request_id, on_remove=lambda: True)
    assert not (root / name).exists()
    assert files.list_trash(root)[0]['trashId'] == removed['trashId']
    assert files.remove_plugin(root, name, original['fingerprint'], request_id)['trashId'] == removed['trashId']
    files.restore_plugin(root, removed['trashId'], on_restore=calls.append)
    assert (root / name / 'keep.txt').read_text() == 'payload'
    assert calls == [name]
    assert files.list_trash(root) == []
    files.restore_plugin(root, removed['trashId'], on_restore=calls.append)
    assert calls == [name]


def test_stale_fingerprint_never_deletes_new_version(root):
    put(root)
    before = files.plugin_entry(root, '@business/plugin-test')['fingerprint']
    put(root, version='2.0.0')
    with pytest.raises(files.PluginFileError, match='已变化'):
        files.remove_plugin(root, '@business/plugin-test', before, str(uuid.uuid4()))
    assert files.plugin_entry(root, '@business/plugin-test')['version'] == '2.0.0'


def test_restore_refuses_overwriting_new_install(root):
    put(root)
    removed = remove(root)
    put(root, version='2.0.0')
    with pytest.raises(files.PluginFileError, match='不会覆盖'):
        files.restore_plugin(root, removed['trashId'])
    assert files.plugin_entry(root, '@business/plugin-test')['version'] == '2.0.0'
    assert len(files.list_trash(root)) == 1


@pytest.mark.parametrize('name', ['../outside', '/etc/passwd', '@x/../outside', '@x/.hidden', 'x/y/z', '', None, 'x' * 215])
def test_invalid_package_names_cannot_escape(root, name):
    with pytest.raises(files.PluginFileError):
        files.package_path(root, name, allow_missing=True)


def test_symlinked_scope_or_package_is_never_followed(root, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    put(outside)
    try:
        (root / '@business').symlink_to(outside / '@business', target_is_directory=True)
    except OSError:
        pytest.skip('本机无符号链接权限，Linux 验收覆盖此分支')
    with pytest.raises(files.PluginFileError):
        files.plugin_entry(root, '@business/plugin-test')
    assert files.scan_plugins(root)['entries'] == []
    assert (outside / '@business/plugin-test/keep.txt').exists()


def test_symlinked_root_is_rejected(root, tmp_path):
    link = tmp_path / 'link'
    try:
        link.symlink_to(root, target_is_directory=True)
    except OSError:
        pytest.skip('本机无符号链接权限')
    with pytest.raises(files.PluginFileError):
        files.checked_root(link)


def test_storage_lock_excludes_other_file_operations(root):
    put(root)
    with files.storage_lock(root):
        with pytest.raises(files.PluginFileError, match='占用'):
            remove(root)
    assert files.plugin_entry(root, '@business/plugin-test')['version'] == '1.0.0'


def test_link_failure_rolls_files_back(root):
    put(root)
    def fail():
        raise RuntimeError('link failed')
    with pytest.raises(RuntimeError):
        remove(root, on_remove=fail)
    assert (root / '@business/plugin-test/keep.txt').exists()
    assert files.list_trash(root) == []


def test_restore_link_failure_keeps_recovery_copy(root):
    put(root)
    removed = remove(root, on_remove=lambda: True)
    def fail(name):
        raise RuntimeError(name)
    with pytest.raises(RuntimeError):
        files.restore_plugin(root, removed['trashId'], on_restore=fail)
    assert not (root / '@business/plugin-test').exists()
    assert len(files.list_trash(root)) == 1


def test_expiry_cleans_only_own_validated_trash(root):
    put(root)
    removed = remove(root)
    directory, meta = files.trash_record(root, removed['trashId'])
    meta['expiresAtEpoch'] = time.time() - 1
    files.atomic_json(directory / 'meta.json', meta)
    backup = root / '.bak-user-files'
    backup.mkdir()
    unknown = root / files.TRASH_DIR / 'do-not-touch'
    unknown.mkdir()
    with pytest.raises(files.PluginFileError, match='恢复期限'):
        files.restore_plugin(root, removed['trashId'])
    files.cleanup_expired(root)
    assert not directory.exists()
    assert backup.exists() and unknown.exists()


@pytest.mark.parametrize('value', [[], {'name': 'wrong', 'version': '1'}, {'name': '@business/plugin-test', 'version': ''}])
def test_invalid_package_metadata_is_not_an_installed_plugin(root, value):
    directory = put(root)
    (directory / 'package.json').write_text(json.dumps(value))
    assert not files.scan_plugins(root)['entries']


def test_json_size_and_special_files_are_rejected(root):
    path = root / 'big.json'
    path.write_bytes(b' ' * (files.MAX_JSON_BYTES + 1))
    with pytest.raises(files.PluginFileError):
        files.read_json(path)
    if hasattr(os, 'mkfifo'):
        fifo = root / 'fifo'
        os.mkfifo(fifo)
        with pytest.raises(files.PluginFileError):
            files.read_json(fifo)


def test_sync_report_and_truncation(root, monkeypatch):
    put(root)
    put(root, 'second')
    report = {'schemaVersion': 1, 'items': [], 'status': 'success'}
    files.atomic_json(root / files.REPORT_FILE, report)
    monkeypatch.setattr(files, 'MAX_PLUGINS', 1)
    result = files.scan_plugins(root)
    assert result['truncated'] is True
    assert result['sync']['status'] == 'success'
    files.atomic_json(root / files.REPORT_FILE, {'schemaVersion': 2})
    assert files.scan_plugins(root)['sync'] is None


def test_duplicate_delete_cannot_change_target(root):
    put(root)
    p = files.plugin_entry(root, '@business/plugin-test')
    request = str(uuid.uuid4())
    files.remove_plugin(root, p['name'], p['fingerprint'], request)
    with pytest.raises(files.PluginFileError, match='参数不一致'):
        files.remove_plugin(root, 'other', p['fingerprint'], request)
    with pytest.raises(files.PluginFileError):
        files.remove_plugin(root, p['name'], p['fingerprint'], 'invalid')


def test_invalid_and_changed_recovery_records_are_rejected(root):
    put(root)
    removed = remove(root)
    with pytest.raises(files.PluginFileError):
        files.restore_plugin(root, '../escape')
    directory, meta = files.trash_record(root, removed['trashId'])
    (directory / 'plugin/package.json').write_text('{"name":"other","version":"2"}')
    with pytest.raises(files.PluginFileError, match='内容已变化'):
        files.restore_plugin(root, removed['trashId'])


def test_atomic_json_failure_does_not_leave_temp_files(root, monkeypatch):
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr(files.os, 'replace', fail)
    with pytest.raises(OSError):
        files.atomic_json(root / 'report.json', {})
    assert not list(root.glob('*.tmp'))


def test_prepared_delete_can_retry_if_move_never_happened(root):
    put(root)
    entry = files.plugin_entry(root, '@business/plugin-test')
    request = uuid.uuid4()
    directory = root / files.TRASH_DIR / request.hex
    directory.mkdir(parents=True)
    files.atomic_json(directory / 'meta.json', {**entry, 'state': 'prepared', 'trashId': request.hex, 'expiresAtEpoch': time.time()+100})
    files.remove_plugin(root, entry['name'], entry['fingerprint'], str(request))
    assert not (root / entry['name']).exists()
    assert (directory / 'plugin/keep.txt').exists()


def test_root_parent_and_plain_file_are_not_plugin_directories(root):
    with pytest.raises(files.PluginFileError):
        files.checked_path(root, '.')
    with pytest.raises(files.PluginFileError):
        files.checked_path(root, '@business/../escape', allow_missing=True)
    (root / 'plain-file').write_text('not a plugin')
    with pytest.raises(files.PluginFileError):
        files.plugin_entry(root, 'plain-file')


def test_invalid_recovery_metadata_is_preserved_for_manual_inspection(root):
    put(root)
    removed = remove(root)
    directory, meta = files.trash_record(root, removed['trashId'])
    meta['expiresAtEpoch'] = 'invalid'
    files.atomic_json(directory / 'meta.json', meta)
    assert files.list_trash(root) == []
    files.cleanup_expired(root)
    assert (directory / 'plugin/keep.txt').exists()


def test_recovery_root_must_be_directory(root):
    (root / files.TRASH_DIR).write_text('not a directory')
    with pytest.raises(files.PluginFileError):
        files.list_trash(root)


def test_delete_receipt_failure_restores_files_and_module_link(root, monkeypatch):
    put(root)
    write = files.atomic_json
    links = []
    def fail_completion(path, value):
        if value.get('state') == 'deleted':
            raise OSError('disk full')
        write(path, value)
    monkeypatch.setattr(files, 'atomic_json', fail_completion)
    with pytest.raises(OSError):
        remove(root, on_remove=lambda: True, on_rollback=links.append)
    assert (root / '@business/plugin-test/keep.txt').exists()
    assert links == ['@business/plugin-test']
    assert not files.list_trash(root)


def test_restore_receipt_failure_restores_recovery_and_removes_link(root, monkeypatch):
    put(root)
    removed = remove(root, on_remove=lambda: True)
    links = []
    write = files.atomic_json
    def fail(path, meta):
        if meta.get('state') == 'restored':
            raise OSError('disk full')
        write(path, meta)
    monkeypatch.setattr(files, 'atomic_json', fail)
    with pytest.raises(OSError):
        files.restore_plugin(root, removed['trashId'], on_restore=lambda name: None, on_rollback=links.append)
    assert not (root / '@business/plugin-test').exists()
    assert links == ['@business/plugin-test']
    assert len(files.list_trash(root)) == 1


class ProcessCrash(BaseException):
    pass


def test_link_created_between_inspection_and_delete_is_recoverable(root):
    put(root)
    removed = remove(root, on_inspect=lambda: False, on_remove=lambda: True)
    links = []
    files.restore_plugin(root, removed['trashId'], on_restore=links.append)
    assert links == ['@business/plugin-test']


def test_interrupted_link_removal_can_retry_and_restore_link(root):
    put(root)
    entry = files.plugin_entry(root, '@business/plugin-test')
    request = str(uuid.uuid4())
    def crash():
        raise ProcessCrash()
    with pytest.raises(ProcessCrash):
        files.remove_plugin(root, entry['name'], entry['fingerprint'], request,
                            on_inspect=lambda: True, on_remove=crash)
    assert not (root / entry['name']).exists()
    retried = files.remove_plugin(root, entry['name'], entry['fingerprint'], request, on_remove=lambda: False)
    assert retried['state'] == 'deleted'
    links = []
    files.restore_plugin(root, retried['trashId'], on_restore=links.append)
    assert links == [entry['name']]
    assert files.plugin_entry(root, entry['name'])['fingerprint'] == entry['fingerprint']


def test_legacy_prepared_receipt_does_not_forget_link_on_retry(root):
    put(root)
    entry = files.plugin_entry(root, '@business/plugin-test')
    request = uuid.uuid4()
    directory = root / files.TRASH_DIR / request.hex
    directory.mkdir(parents=True)
    files.atomic_json(directory/'meta.json', {**entry, 'state':'prepared', 'trashId':request.hex, 'expiresAtEpoch':time.time()+100})
    os.rename(root / entry['name'], directory/'plugin')
    result = files.remove_plugin(root, entry['name'], entry['fingerprint'], str(request), on_remove=lambda: False)
    assert result['linkRequired'] is True
    links = []
    files.restore_plugin(root, request.hex, on_restore=links.append)
    assert links == [entry['name']]


@pytest.mark.parametrize('crash_after_link', [False, True])
def test_restore_crash_resumes_without_overwriting_new_files(root, monkeypatch, crash_after_link):
    put(root)
    removed = remove(root, on_remove=lambda: True)
    write = files.atomic_json
    def crash_write(path, meta):
        if meta.get('state') == 'restored':
            raise ProcessCrash()
        write(path, meta)
    def crash_link(name):
        raise ProcessCrash()
    with monkeypatch.context() as patch:
        if crash_after_link: patch.setattr(files, 'atomic_json', crash_write)
        with pytest.raises(ProcessCrash):
            files.restore_plugin(root, removed['trashId'], on_restore=(lambda name: None) if crash_after_link else crash_link)
    record = files.list_trash(root)[0]
    assert record['restorePending'] is True
    assert record['fingerprint'] == files.plugin_entry(root, removed['name'])['fingerprint']
    links = []
    result = files.restore_plugin(root, removed['trashId'], on_restore=links.append)
    assert result['state'] == 'restored' and links == [removed['name']]
    assert files.list_trash(root) == []


def test_interrupted_restore_rejects_replaced_target(root):
    put(root)
    removed = remove(root, on_remove=lambda: True)
    with pytest.raises(ProcessCrash):
        files.restore_plugin(root, removed['trashId'], on_restore=lambda name: (_ for _ in ()).throw(ProcessCrash()))
    (root / removed['name'] / 'package.json').write_text('{"name":"@business/plugin-test","version":"2"}')
    with pytest.raises(files.PluginFileError, match='不会覆盖'):
        files.restore_plugin(root, removed['trashId'])


def test_delete_crash_before_move_can_be_cancelled_by_restore(root, monkeypatch):
    put(root)
    entry = files.plugin_entry(root, '@business/plugin-test')
    request = uuid.uuid4()
    with monkeypatch.context() as patch:
        patch.setattr(files, '_move_plugin', lambda *args: (_ for _ in ()).throw(ProcessCrash()))
        with pytest.raises(ProcessCrash):
            files.remove_plugin(root, entry['name'], entry['fingerprint'], str(request), on_remove=lambda: True)
    assert files.list_trash(root)[0]['restorePending'] is True
    assert files.restore_plugin(root, request.hex, on_restore=lambda name: None)['state'] == 'restored'
    assert files.remove_plugin(root, entry['name'], entry['fingerprint'], str(request))['state'] == 'restored'


def test_prepared_delete_does_not_unlink_new_replacement(root):
    put(root)
    entry = files.plugin_entry(root, '@business/plugin-test')
    request = uuid.uuid4()
    def crash(): raise ProcessCrash()
    with pytest.raises(ProcessCrash):
        files.remove_plugin(root, entry['name'], entry['fingerprint'], str(request), on_remove=crash)
    put(root, version='2')
    with pytest.raises(files.PluginFileError, match='新文件'):
        files.remove_plugin(root, entry['name'], entry['fingerprint'], str(request), on_remove=lambda: pytest.fail('must not unlink'))
