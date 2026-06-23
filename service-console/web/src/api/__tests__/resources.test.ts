import { describe, it, expect, vi, beforeEach } from 'vitest';

// mock 底层 axios 实例(./client):resources 是其薄包装层,断言每个封装把方法/URL/参数/请求体
// 正确委派给 client,并原样回 r.data。覆盖此前 0% 的资源契约层(URL 拼接、信封透传、show-once 等)。
const get = vi.fn();
const post = vi.fn();
const patch = vi.fn();
const del = vi.fn();
vi.mock('../client', () => ({
  default: {
    get: (...a: unknown[]) => get(...a),
    post: (...a: unknown[]) => post(...a),
    patch: (...a: unknown[]) => patch(...a),
    delete: (...a: unknown[]) => del(...a),
  },
}));

import * as resources from '../resources';

const envelope = { count: 1, rows: [{ id: 1 }], page: 1, pageSize: 20, totalPage: 1 };

describe('resources 资源层(委派 + URL 契约)', () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
    patch.mockReset();
    del.mockReset();
  });

  it('list:GET /api/<resource>,params 透传,回 r.data(统一信封)', async () => {
    get.mockResolvedValue({ data: envelope });
    const r = await resources.list('foo', { page: 2, pageSize: 10, x: 'y' });
    expect(get).toHaveBeenCalledWith('/api/foo', { params: { page: 2, pageSize: 10, x: 'y' } });
    expect(r).toBe(envelope);
  });

  it('list:默认 params 为空对象', async () => {
    get.mockResolvedValue({ data: envelope });
    await resources.list('foo');
    expect(get).toHaveBeenCalledWith('/api/foo', { params: {} });
  });

  it('listNamespaces:走 GET /api/namespaces', async () => {
    get.mockResolvedValue({ data: envelope });
    const r = await resources.listNamespaces({ pageSize: 200 });
    expect(get).toHaveBeenCalledWith('/api/namespaces', { params: { pageSize: 200 } });
    expect(r).toBe(envelope);
  });

  it('get:GET /api/<resource>/{id}', async () => {
    get.mockResolvedValue({ data: { id: 9 } });
    const r = await resources.get('foo', 9);
    expect(get).toHaveBeenCalledWith('/api/foo/9');
    expect(r).toEqual({ id: 9 });
  });

  it('listNodes:GET /api/nodes', async () => {
    get.mockResolvedValue({ data: envelope });
    await resources.listNodes({ page: 1 });
    expect(get).toHaveBeenCalledWith('/api/nodes', { params: { page: 1 } });
  });

  it('listNodeOperations:GET /api/node-operations', async () => {
    get.mockResolvedValue({ data: envelope });
    await resources.listNodeOperations({ page: 1 });
    expect(get).toHaveBeenCalledWith('/api/node-operations', { params: { page: 1 } });
  });

  it('listInstances:GET /api/nodes/instances(namespace/status 透传)', async () => {
    get.mockResolvedValue({ data: envelope });
    await resources.listInstances({ page: 1, namespace: 'ns-a', status: 'active' });
    expect(get).toHaveBeenCalledWith('/api/nodes/instances', {
      params: { page: 1, namespace: 'ns-a', status: 'active' },
    });
  });

  it('dispatchNodeAction:POST /api/nodes/{agentId}/{serviceCode}/{action},body + suppressGlobalError,agentId/serviceCode 转义', async () => {
    post.mockResolvedValue({ data: { kind: 'command', requestId: 'r1' } });
    const r = await resources.dispatchNodeAction('ns a', 'svc/c', 'stop', { mode: 'force' });
    expect(post).toHaveBeenCalledWith(
      '/api/nodes/ns%20a/svc%2Fc/stop',
      { mode: 'force' },
      { suppressGlobalError: true },
    );
    expect(r).toEqual({ kind: 'command', requestId: 'r1' });
  });

  it('dispatchNodeAction:body 缺省为空对象', async () => {
    post.mockResolvedValue({ data: { kind: 'rolling', taskId: 't1' } });
    await resources.dispatchNodeAction('a', 'b', 'start');
    expect(post).toHaveBeenCalledWith('/api/nodes/a/b/start', {}, { suppressGlobalError: true });
  });

  it('getReconciliation:GET /api/nodes/reconciliation', async () => {
    const recon = { runningButUnmanaged: [], managedButDown: [], versionDrift: [] };
    get.mockResolvedValue({ data: recon });
    const r = await resources.getReconciliation();
    expect(get).toHaveBeenCalledWith('/api/nodes/reconciliation');
    expect(r).toBe(recon);
  });

  it('create:POST /api/<resource>,suppressGlobalError,回响应体(可能含 show-once)', async () => {
    post.mockResolvedValue({ data: { id: 2, agentKey: 'secret' } });
    const r = await resources.create('namespaces', { code: 'ns' });
    expect(post).toHaveBeenCalledWith('/api/namespaces', { code: 'ns' }, { suppressGlobalError: true });
    expect(r).toEqual({ id: 2, agentKey: 'secret' });
  });

  it('update:PATCH /api/<resource>/{id},suppressGlobalError', async () => {
    patch.mockResolvedValue({ data: { id: 3 } });
    const r = await resources.update('services', 3, { dir: '/x' });
    expect(patch).toHaveBeenCalledWith('/api/services/3', { dir: '/x' }, { suppressGlobalError: true });
    expect(r).toEqual({ id: 3 });
  });

  it('remove:DELETE /api/<resource>/{id}(204,无返回)', async () => {
    del.mockResolvedValue({});
    await resources.remove('foo', 4);
    expect(del).toHaveBeenCalledWith('/api/foo/4');
  });

  it('rotateKey:POST /api/namespaces/{id}/rotate-key → {agentKey}', async () => {
    post.mockResolvedValue({ data: { agentKey: 'k' } });
    const r = await resources.rotateKey(5);
    expect(post).toHaveBeenCalledWith('/api/namespaces/5/rotate-key');
    expect(r).toEqual({ agentKey: 'k' });
  });

  it('rotatePullToken:POST /api/namespaces/{id}/rotate-pull-token → {pullToken}', async () => {
    post.mockResolvedValue({ data: { pullToken: 'p' } });
    const r = await resources.rotatePullToken(6);
    expect(post).toHaveBeenCalledWith('/api/namespaces/6/rotate-pull-token');
    expect(r).toEqual({ pullToken: 'p' });
  });

  it('uploadPluginVersion:multipart POST /api/plugin-versions/upload(字段名 file)+ suppressGlobalError', async () => {
    post.mockResolvedValue({ data: { pluginVersionId: 1, attachmentId: 2, version: '1.0.0' } });
    const file = new File(['x'], 'p.tgz');
    const r = await resources.uploadPluginVersion(file);
    expect(post).toHaveBeenCalledTimes(1);
    const [url, form, cfg] = post.mock.calls[0];
    expect(url).toBe('/api/plugin-versions/upload');
    expect(form).toBeInstanceOf(FormData);
    expect((form as FormData).get('file')).toBe(file);
    expect(cfg).toEqual({ suppressGlobalError: true });
    expect(r).toEqual({ pluginVersionId: 1, attachmentId: 2, version: '1.0.0' });
  });

  it('listPluginVersions:GET /api/plugin-versions(pluginId 过滤透传)', async () => {
    get.mockResolvedValue({ data: envelope });
    await resources.listPluginVersions({ pluginId: 7 });
    expect(get).toHaveBeenCalledWith('/api/plugin-versions', { params: { pluginId: 7 } });
  });

  it('publish:POST /api/releases/publish(三 id)+ suppressGlobalError', async () => {
    post.mockResolvedValue({ data: { id: 1 } });
    await resources.publish({ serviceId: 1, pluginId: 2, pluginVersionId: 3 });
    expect(post).toHaveBeenCalledWith(
      '/api/releases/publish',
      { serviceId: 1, pluginId: 2, pluginVersionId: 3 },
      { suppressGlobalError: true },
    );
  });

  it('reactivate:POST /api/releases/reactivate({spvId})', async () => {
    post.mockResolvedValue({ data: { id: 1 } });
    await resources.reactivate({ spvId: 8 });
    expect(post).toHaveBeenCalledWith('/api/releases/reactivate', { spvId: 8 });
  });

  it('rollback:POST /api/releases/rollback({spvId})', async () => {
    post.mockResolvedValue({ data: { id: 1 } });
    await resources.rollback({ spvId: 9 });
    expect(post).toHaveBeenCalledWith('/api/releases/rollback', { spvId: 9 });
  });

  it('listReleases:GET /api/releases(不带 filter)', async () => {
    get.mockResolvedValue({ data: envelope });
    await resources.listReleases({ pageSize: 200 });
    expect(get).toHaveBeenCalledWith('/api/releases', { params: { pageSize: 200 } });
  });

  it('listReleaseHistory:GET /api/releases(serviceId+pluginId 过滤)', async () => {
    get.mockResolvedValue({ data: envelope });
    await resources.listReleaseHistory({ serviceId: 1, pluginId: 2 });
    expect(get).toHaveBeenCalledWith('/api/releases', { params: { serviceId: 1, pluginId: 2 } });
  });
});
