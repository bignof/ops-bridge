"""从 Docker 挂载发现插件目录；所有写操作在重新核对实例、目录和文件后执行。"""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess

from config import PROJECTS_ROOT
from services.compose import collect_service_statuses, find_compose_file
from services.plugin_files import (PluginFileError, checked_root, cleanup_expired, package_path,
                                   remove_plugin, restore_plugin, scan_plugins, storage_lock, utc_stamp)

CONTAINER_RE = re.compile(r'^[a-f0-9]{12,64}$')
MAX_CONTAINERS = 500

# 不执行应用代码，只检查并处理指向指定插件目录的符号链接。
LINK_SCRIPT = r"""
const fs=require('fs'),path=require('path');
const [mode,modules,name,target]=process.argv.slice(1);
const link=path.join(modules,name),expected=path.resolve(target);
const parent=path.dirname(link);
if(fs.existsSync(parent)&&fs.realpathSync(parent)!==path.resolve(parent))throw Error('node_modules 父目录包含符号链接，拒绝操作');
let info=null;try{info=fs.lstatSync(link)}catch(e){if(e.code!=='ENOENT')throw e}
const matches=!!info&&info.isSymbolicLink()&&path.resolve(path.dirname(link),fs.readlinkSync(link))===expected;
if(mode==='remove'){
  if(matches)fs.unlinkSync(link);
  process.stdout.write(JSON.stringify({removed:matches}));
}else if(mode==='restore'){
  if(info&&!matches)throw Error('node_modules 中存在其他文件，不会覆盖');
  if(!info){fs.mkdirSync(path.dirname(link),{recursive:true});fs.symlinkSync(target,link,'dir');}
  process.stdout.write('{}');
}else{throw Error('invalid mode')}
"""


def _run(args):
    result = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise PluginFileError('docker_error', '无法读取或操作目标容器')
    if len(result.stdout) > 8 * 1024 * 1024:
        raise PluginFileError('too_large', '容器信息超过限制')
    return result.stdout


def all_containers():
    ids = _run(['ps', '-aq']).split()
    if len(ids) > MAX_CONTAINERS or any(not CONTAINER_RE.fullmatch(value) for value in ids):
        raise PluginFileError('too_large', '容器数量或标识超出支持范围')
    if not ids:
        return []
    data = json.loads(_run(['inspect', *ids]))
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise PluginFileError('invalid', 'Docker inspect 响应非法')
    return data


def _env(info):
    return dict(value.split('=', 1) for value in info.get('Config', {}).get('Env', []) if '=' in value)


def _inside(path, root):
    try:
        return os.path.commonpath([str(path), str(root)]) == str(root)
    except ValueError:
        return False


def _host_mapping(info, destination):
    choices = []
    for mount in info.get('Mounts', []):
        mounted = PurePosixPath(mount.get('Destination') or '/')
        if destination == mounted or mounted in destination.parents:
            choices.append((len(mounted.parts), mount, destination.relative_to(mounted)))
    if not choices:
        return None
    _, mount, relative = max(choices, key=lambda item: item[0])
    return mount, Path(mount.get('Source') or '').joinpath(*relative.parts)


def storage_mount(info):
    env = _env(info)
    working = PurePosixPath(info.get('Config', {}).get('WorkingDir') or '/app/nocobase')
    # 只读取显式挂载且位于管理根内的配置，不输出其中凭据。
    config = {}
    config_file = PurePosixPath(env.get('ORCHISKY_CONFIG_FILE') or str(working / 'sync-plugins.config.json'))
    if not config_file.is_absolute():
        config_file = working / config_file
    mapped_config = _host_mapping(info, config_file)
    if mapped_config and _inside(mapped_config[1].resolve(), Path(PROJECTS_ROOT).resolve()) and mapped_config[1].is_file():
        from services.plugin_files import read_json
        try:
            config, _, _ = read_json(mapped_config[1], limit=65536)
        except (OSError, ValueError, PluginFileError):
            raise PluginFileError('invalid', '无法读取插件同步配置')
    configured = env.get('PLUGIN_STORAGE_PATH') or config.get('storagePath') or 'storage/plugins'
    destination = PurePosixPath(configured)
    if not destination.is_absolute():
        destination = working / destination
    if '..' in destination.parts:
        raise PluginFileError('unsafe_path', '插件容器路径包含上级目录')
    mapped = _host_mapping(info, destination)
    if not mapped:
        return None
    mount, root = mapped
    source = mount.get('Source')
    if not source:
        raise PluginFileError('unsupported', '插件目录未映射到 Agent 可访问的路径')
    managed = Path(PROJECTS_ROOT).resolve()
    if not _inside(root.resolve(), managed):
        raise PluginFileError('unsafe_path', '插件挂载不在 Agent 管理目录中')
    root = checked_root(root)
    modules = PurePosixPath(env.get('NODE_MODULES_PATH') or config.get('nodeModulesPath') or 'node_modules')
    if not modules.is_absolute():
        modules = working / modules
    modules_mount = _host_mapping(info, modules)
    return {'root': str(root), 'containerPath': str(destination), 'nodeModulesPath': str(modules),
            'modulesMount': str(modules_mount[1].resolve()) if modules_mount else None, 'writable': bool(mount.get('RW'))}


def _started_at(info):
    return info.get('State', {}).get('StartedAt')


def _epoch(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def collect_inventory(info, containers):
    identity = {'containerId': info.get('Id'), 'containerName': str(info.get('Name', '')).lstrip('/'),
                'containerStartedAt': _started_at(info)}
    try:
        mount = storage_mount(info)
        if mount is None:
            return None
        inventory = scan_plugins(mount['root'])
        shared = []
        for other in containers:
            if other.get('Id') == info.get('Id'):
                continue
            try:
                other_mount = storage_mount(other)
                shared_modules = other_mount and mount['modulesMount'] and other_mount['modulesMount'] and (
                    _inside(mount['modulesMount'], other_mount['modulesMount']) or _inside(other_mount['modulesMount'], mount['modulesMount']))
                if other_mount and (shared_modules or _inside(mount['root'], other_mount['root']) or _inside(other_mount['root'], mount['root'])):
                    shared.append(str(other.get('Name', '')).lstrip('/'))
            except (PluginFileError, OSError):
                continue
        report = inventory.get('sync') or {}
        report_start, container_start = _epoch(report.get('startedAt')), _epoch(identity['containerStartedAt'])
        current = report_start is not None and container_start is not None and report_start >= container_start - 2
        inventory.update(identity)
        inventory.update(mount)
        inventory['sharedWith'] = shared
        inventory['syncCurrent'] = current
        inventory['mutable'] = bool(mount['writable'] and not shared and current and report.get('lockProtocol') == 'flock-v1' and not inventory['truncated'])
        inventory['busy'] = bool(current and report.get('status') == 'running')
        inventory['syncInterrupted'] = False
        if not inventory['mutable']:
            inventory['mutationReason'] = ('插件目录被多个实例共享' if shared else '插件挂载为只读' if not mount['writable'] else
                                           '当前服务尚未提供本次启动的插件操作锁，请升级服务镜像并重启')
        # 清理不抢占正在进行的安装/删除；过期隔离目录即使保留到下一轮也不能再恢复。
        try:
            with storage_lock(mount['root']):
                cleanup_expired(mount['root'])
                if inventory['busy']:
                    inventory['syncInterrupted'] = True
                    inventory['busy'] = False
        except (OSError, PluginFileError):
            pass
        return inventory
    except (PluginFileError, OSError, ValueError) as exc:
        return {**identity, 'schemaVersion': 1, 'status': 'error', 'scannedAt': utc_stamp(), 'entries': [], 'mutable': False,
                'error': str(exc)[:300]}


def enrich_statuses(services):
    ids = [s.get('containerId') for s in services if CONTAINER_RE.fullmatch(str(s.get('containerId', '')))]
    if not ids:
        return services
    try:
        containers = all_containers()
    except (OSError, ValueError, PluginFileError, subprocess.TimeoutExpired):
        return [{**s, 'plugins': {'schemaVersion': 1, 'status': 'error', 'containerId': s.get('containerId'),
                 'containerName': s.get('containerName'), 'scannedAt': utc_stamp(), 'error': '无法读取 Docker 插件挂载信息', 'entries': []}}
                if CONTAINER_RE.fullmatch(str(s.get('containerId', ''))) else s for s in services]
    enriched = []
    for service in services:
        row = dict(service)
        cid = str(row.get('containerId') or '')
        info = next((c for c in containers if cid and str(c.get('Id', '')).startswith(cid)), None)
        if info:
            inventory = collect_inventory(info, containers)
            if inventory is not None:
                row['plugins'] = inventory
        enriched.append(row)
    return enriched


def _project(project_dir):
    project = Path(project_dir).resolve()
    if not _inside(project, Path(PROJECTS_ROOT).resolve()) or not find_compose_file(str(project)):
        raise PluginFileError('unsafe_path', '目标不在 Agent 管理的 compose 目录中')
    return str(project)


def _link(info, mount, mode, name):
    package_path(mount['root'], name, allow_missing=True)
    target = str(PurePosixPath(mount['containerPath']) / name)
    response = json.loads(_run(['exec', info['Id'], 'node', '-e', LINK_SCRIPT, mode, mount['nodeModulesPath'], name, target]))
    return response.get('removed', False)


def execute_plugin_operation(project_dir, action, payload, request_id):
    project_dir = _project(project_dir)
    services = collect_service_statuses(project_dir)
    if action == 'plugin_scan':
        result = enrich_statuses(services)
        if not any(s.get('plugins', {}).get('status') == 'ok' for s in result):
            raise PluginFileError('scan_failed', '未发现可读取的插件目录，请检查 Agent 挂载和服务配置')
        return {'message': '插件状态已重新采集'}, result
    cid = payload.get('containerId')
    if not isinstance(cid, str) or not CONTAINER_RE.fullmatch(cid):
        raise PluginFileError('invalid', '容器标识非法')
    allowed_ids = [str(s.get('containerId', '')) for s in services]
    if not any(value and (cid.startswith(value) or value.startswith(cid)) for value in allowed_ids):
        raise PluginFileError('conflict', '实例容器已变化，请刷新后重试')
    containers = all_containers()
    info = next((c for c in containers if str(c.get('Id', '')).startswith(cid)), None)
    if not info or not info.get('State', {}).get('Running'):
        raise PluginFileError('not_running', '实例当前未运行，拒绝操作')
    inventory = collect_inventory(info, containers)
    if not inventory or not inventory.get('mutable'):
        raise PluginFileError('unsupported', (inventory or {}).get('mutationReason') or (inventory or {}).get('error') or '此实例不支持本地插件操作')
    mount = storage_mount(info)
    if action == 'plugin_remove':
        result = remove_plugin(mount['root'], payload.get('pluginName'), payload.get('fingerprint'), request_id,
                               on_remove=lambda: _link(info, mount, 'remove', payload.get('pluginName')),
                               on_rollback=lambda name: _link(info, mount, 'restore', name))
        message = f"本地文件已删除：{result['name']}（7 天内可恢复）"
    elif action == 'plugin_restore':
        result = restore_plugin(mount['root'], payload.get('trashId'),
                                on_restore=lambda name: _link(info, mount, 'restore', name),
                                on_rollback=lambda name: _link(info, mount, 'remove', name))
        message = f"本地文件已恢复：{result['name']}"
    else:
        raise PluginFileError('invalid', '不支持的本地插件操作')
    return {'message': message, 'name': result['name'], 'version': result['version'], 'trashId': result['trashId']}, enrich_statuses(collect_service_statuses(project_dir))
