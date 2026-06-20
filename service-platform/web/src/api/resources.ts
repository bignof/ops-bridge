import client from './client';

// 泛型 CRUD 资源层:统一封装 service-platform 控制台对 P1a FastAPI 后端的资源访问。
// - 列表统一信封(全量 camelCase,与 P1a `*Out` 模型 to_camel 输出对齐):
//     { count, rows, page, pageSize, totalPage }
// - 资源路径约定:/api/<resource>(如 'namespaces' → /api/namespaces)。
// - 写操作(create/update/remove)返回后端响应体(create 可能含 show-once 明文,如 agentKey)。
// 业务页一律走本层,不在组件里散调 client / 写死 URL。

/** 列表响应统一信封(后端 list 端点契约,字段全 camelCase)。 */
export interface ListEnvelope<T> {
  count: number;
  rows: T[];
  page: number;
  pageSize: number;
  totalPage: number;
}

/** 列表查询参数:page/pageSize 为分页,其余键平铺为过滤条件(如 namespaceId)。 */
export interface ListParams {
  page?: number;
  pageSize?: number;
  [filterKey: string]: unknown;
}

const base = (resource: string) => `/api/${resource}`;

/** 列表(服务端分页):GET /api/<resource>?page=&pageSize=&<filter>。 */
export async function list<T>(resource: string, params: ListParams = {}): Promise<ListEnvelope<T>> {
  const r = await client.get<ListEnvelope<T>>(base(resource), { params });
  return r.data;
}

/** 详情:GET /api/<resource>/{id}。 */
export async function get<T>(resource: string, id: string | number): Promise<T> {
  const r = await client.get<T>(`${base(resource)}/${id}`);
  return r.data;
}

/**
 * 新建:POST /api/<resource>(201)。返回响应体(可能含 show-once 明文)。
 * `suppressGlobalError`:CrudTable 对 409「编码已存在」本地精确提示,故 opt-out 全局兜底防双 toast。
 */
export async function create<T = unknown>(
  resource: string,
  values: Record<string, unknown>,
): Promise<T> {
  const r = await client.post<T>(base(resource), values, { suppressGlobalError: true });
  return r.data;
}

/**
 * 更新:PATCH /api/<resource>/{id}。
 * `suppressGlobalError`:同 create,CrudTable 对 409 本地提示,opt-out 全局兜底防双 toast。
 */
export async function update<T = unknown>(
  resource: string,
  id: string | number,
  values: Record<string, unknown>,
): Promise<T> {
  const r = await client.patch<T>(`${base(resource)}/${id}`, values, {
    suppressGlobalError: true,
  });
  return r.data;
}

/** 删除:DELETE /api/<resource>/{id}(204)。 */
export async function remove(resource: string, id: string | number): Promise<void> {
  await client.delete(`${base(resource)}/${id}`);
}

/** 轮换密钥(命名空间):POST /api/namespaces/{id}/rotate-key → { agentKey }(show-once)。 */
export async function rotateKey(id: string | number): Promise<{ agentKey: string }> {
  const r = await client.post<{ agentKey: string }>(`/api/namespaces/${id}/rotate-key`);
  return r.data;
}

/** 轮换 pull token(命名空间):POST /api/namespaces/{id}/rotate-pull-token → { pullToken }(show-once)。 */
export async function rotatePullToken(id: string | number): Promise<{ pullToken: string }> {
  const r = await client.post<{ pullToken: string }>(`/api/namespaces/${id}/rotate-pull-token`);
  return r.data;
}

/** 插件版本上传响应(P1a 解析 .tgz 内 package.json 取 version + 落库)。 */
export interface UploadPluginVersionResult {
  pluginVersionId: string | number;
  attachmentId: string | number;
  /** 包内 package.json.version,上传成功后回显给用户。 */
  version: string;
}

/**
 * 上传插件版本:multipart POST /api/plugin-versions/upload,字段名固定 `file`(.tgz)。
 * 成功响应含解析出的 `version`(供页面成功提示回显)。
 * 失败由调用方按 `e.response.status` 区分:400(未匹配/匹配多个插件)、409(版本已存在)、413(超限)。
 */
export async function uploadPluginVersion(file: File): Promise<UploadPluginVersionResult> {
  const form = new FormData();
  form.append('file', file);
  // 上传页对 400/409/413 本地精确提示,opt-out 全局兜底防双 toast(401 仍统一处理)。
  const r = await client.post<UploadPluginVersionResult>('/api/plugin-versions/upload', form, {
    suppressGlobalError: true,
  });
  return r.data;
}

/**
 * 已上传插件版本列表(服务端分页,统一信封 `{count, rows, …}`):
 * GET /api/plugin-versions?page=&pageSize=&pluginId=。可选 `pluginId` 过滤按插件查其版本。
 */
export async function listPluginVersions<T>(params: ListParams = {}): Promise<ListEnvelope<T>> {
  return list<T>('plugin-versions', params);
}

// ── 插件发布(releases,旧 t_service_plugin_version) ────────────────────────────
// 发布主表 + 历史版本 + 重新激活 + 回滚。P1a `GET /api/releases` 单端点服务两种视图:
//  - 不传 filter   → 每绑定当前 active 行(主表视图,后端按 isActive=yes 聚合)。
//  - 传 serviceId+pluginId → 该 service+plugin 的全部 spv 历史(历史抽屉视图)。
// 写动作走 /api/releases/{publish,reactivate,rollback}。

/** 发布请求体(三 id 定位:服务 + 插件 + 该插件的某版本)。 */
export interface PublishReleaseParams {
  serviceId: string | number;
  pluginId: string | number;
  pluginVersionId: string | number;
}

/**
 * 发布:POST /api/releases/publish(body `{serviceId, pluginId, pluginVersionId}`)。
 * 旧 `pluginPublisher`:校验绑定存在 + 版本未发过(已发过提示去历史版本),全灭活后新建 isActive 行。
 */
export async function publish<T = unknown>(params: PublishReleaseParams): Promise<T> {
  // 发布页对 409「该版本已发布过」本地精确提示,故 opt-out 全局兜底防双 toast。
  // ⚠️ opt-out 后非 409 失败的可见性由发布页 handlePublish 自己兜底(generic fallback),不可静默吞。
  const r = await client.post<T>('/api/releases/publish', params, { suppressGlobalError: true });
  return r.data;
}

/**
 * 重新激活历史版本:POST /api/releases/reactivate(body `{spvId}`)。
 * 旧 `updateVersion`(实读=把目标历史 spv 置为唯一 active + 刷新 publishTime),非编辑。
 */
export async function reactivate<T = unknown>(params: { spvId: string | number }): Promise<T> {
  const r = await client.post<T>('/api/releases/reactivate', params);
  return r.data;
}

/**
 * 回滚:POST /api/releases/rollback(body `{spvId}`)。
 * 旧 `pluginRollback`:当前 active 版回滚到上一未回滚版本;非 active 版报「无需回滚」。
 */
export async function rollback<T = unknown>(params: { spvId: string | number }): Promise<T> {
  const r = await client.post<T>('/api/releases/rollback', params);
  return r.data;
}

/**
 * 发布主表(服务端分页,统一信封):GET /api/releases(**不传 filter**)。
 * 后端按 isActive=yes 回每个 service+plugin 绑定当前激活的那一行。
 */
export async function listReleases<T>(params: ListParams = {}): Promise<ListEnvelope<T>> {
  return list<T>('releases', params);
}

/**
 * 历史版本(服务端过滤):GET /api/releases?serviceId=&pluginId=。
 * 取该 service+plugin 的**全部 spv 历史**(不止 active),供历史抽屉展示 + 重新激活。
 */
export async function listReleaseHistory<T>(
  params: { serviceId: string | number; pluginId: string | number } & ListParams,
): Promise<ListEnvelope<T>> {
  return list<T>('releases', params);
}
