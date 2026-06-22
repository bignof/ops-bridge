import client from './client';

// 泛型 CRUD 资源层:统一封装 service-console 控制台对 P1a FastAPI 后端的资源访问。
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

// ── 节点(nodes,Service 台账 × agent 派生)────────────────────────────────────
// 节点页 = 平台 Service 表驱动的 (agent×service) 列表 + 行级运维操作。

/** 节点行(对齐后端 NodeOut,全 camelCase)。online/degraded 为 bool;lastSeen/healthyCount 可空。 */
export interface NodeRow {
  agentId: string;
  serviceCode: string;
  /** 命名空间可读名(= agentId,列直接用)。 */
  namespaceCode: string;
  dir: string;
  defaultImage: string;
  nacosServiceName: string;
  online: boolean;
  lastSeen: string | null;
  /** 健康实例数;degraded 或后端未知时为 null(列显「-」)。 */
  healthyCount: number | null;
  /** 降级:健康计数不可信,列以「-」展示。 */
  degraded: boolean;
}

/** 行级运维动作。stop/redeploy 须传 mode;restart 缺省 graceful;start 无 mode。 */
export type NodeAction = 'start' | 'stop' | 'restart' | 'redeploy';

/** 动作请求体:stop/restart/redeploy 带 mode;force stop 可带 allowLastInstance=false。 */
export interface NodeActionBody {
  mode?: 'graceful' | 'force';
  allowLastInstance?: boolean;
}

/** 动作响应(对齐后端 NodeActionOut):同步命令(requestId)或滚动任务(taskId)。 */
export interface NodeActionOut {
  kind: 'command' | 'rolling';
  requestId?: string;
  taskId?: string;
  accepted?: boolean;
}

/** 节点列表(服务端分页,统一信封):GET /api/nodes?page=&pageSize=。 */
export async function listNodes<T = NodeRow>(params: ListParams = {}): Promise<ListEnvelope<T>> {
  return list<T>('nodes', params);
}

/**
 * 操作审计行(对齐后端 NodeOperationOut,全 camelCase)。
 * hub dispatch 命令(start/stop/force-restart/redeploy)的审计快照子集;**纯只读展示**。
 * output/error 后端已截尾(≤~1000 字符);mode/requestedBy/requestSource/dir/image/output/error
 * /createdAt/updatedAt 均可空(列对空值显「-」)。优雅 restart 走 rolling,不在此列表(已知缺口)。
 */
export interface NodeOperationRow {
  requestId: string;
  agentId: string;
  action: string;
  mode: string | null;
  status: string;
  /** 派生身份(谁下发);可空。 */
  requestedBy: string | null;
  requestSource: string | null;
  dir: string | null;
  image: string | null;
  /** 命令输出摘要(后端已截尾);可空。 */
  output: string | null;
  error: string | null;
  createdAt: string | null;
  updatedAt: string | null;
}

/** 操作审计列表(服务端分页,统一信封):GET /api/node-operations?page=&pageSize=。 */
export async function listNodeOperations<T = NodeOperationRow>(
  params: ListParams = {},
): Promise<ListEnvelope<T>> {
  return list<T>('node-operations', params);
}

/**
 * 行级运维动作:POST /api/nodes/{agentId}/{serviceCode}/{action}(body `{mode?, allowLastInstance?}`)。
 * `suppressGlobalError`:节点页对 400/404/409/502 本地精确提示(各文案不同),opt-out 全局兜底防双 toast。
 * ⚠️ opt-out 后非预期状态失败的可见性由节点页 catch 自己兜底(不可静默吞,A2);401 仍由拦截器统一处理。
 * agentId/serviceCode 经 encodeURIComponent 防含特殊字符时路径破裂。返回 NodeActionOut。
 */
export async function dispatchNodeAction(
  agentId: string,
  serviceCode: string,
  action: NodeAction,
  body: NodeActionBody = {},
): Promise<NodeActionOut> {
  const r = await client.post<NodeActionOut>(
    `/api/nodes/${encodeURIComponent(agentId)}/${encodeURIComponent(serviceCode)}/${action}`,
    body,
    { suppressGlobalError: true },
  );
  return r.data;
}

// ── 实例(instances,DiscoveredNode = agent 自动发现的容器)──────────────────────
// 实例页 = agent 周期性从 nacos + docker labels 发现上报的物理容器列表(发现权威)。
// 与上面「节点(nodes)」不同:nodes 是平台 Service 台账 ×(agent×service) 的逻辑视图;
// instances 是 agent 真实发现的**每个容器一行**(同 nacos 名的多容器各算一个实例)。

/**
 * 实例行(对齐后端 DiscoveredNodeOut,全 camelCase)。
 * `dir`/`image`/`composeProject` 为 agent 发现的**权威值**(非手配)。
 * `status`:`active`(本轮在报)/ `stale`(失联或本轮缺席,仍保留可定位、可被 start)。
 * `running`:容器是否在跑(docker 含 stopped 也上报);`healthy`:nacos 匹配的健康态(无匹配为 null)。
 * `nacosService`:nacos 匹配到的服务名(无匹配为 null)。
 */
export interface InstanceRow {
  agentId: string;
  containerName: string;
  /** 容器真实 id(可空)。 */
  containerId: string | null;
  /** compose 工程名(发现权威;可空)。 */
  composeProject: string | null;
  /** compose 服务名(发现权威;可空)。 */
  composeService: string | null;
  /** 目录(发现权威;可空)。 */
  dir: string | null;
  /** 镜像(发现权威;可空)。 */
  image: string | null;
  /** 容器是否在跑。 */
  running: boolean;
  /** nacos 匹配的服务名;无匹配为 null。 */
  nacosService: string | null;
  /** nacos 健康态;无匹配为 null(列显「-」)。 */
  healthy: boolean | null;
  /** active(本轮在报)/ stale(失联或缺席,仍保留)。 */
  status: string;
  /** 最近心跳时间(ISO8601;可空)。 */
  heartbeatAt: string | null;
  /** 首次发现时间(ISO8601;可空)。 */
  firstSeenAt: string | null;
}

/**
 * 实例列表(服务端分页,统一信封):GET /api/nodes/instances?page=&pageSize=&namespace=&status=。
 * - `namespace`:按 agentId 过滤(发现实例的命名空间)。
 * - `status`:`active` / `stale` 过滤;**省略 = 含 active+stale**(stale 实例也要可见,可被 start)。
 */
export async function listInstances<T = InstanceRow>(
  params: ListParams = {},
): Promise<ListEnvelope<T>> {
  return list<T>('nodes/instances', params);
}

// ── 服务对账(reconciliation,意图 Service ⋈ 现实发现实例 by nacosServiceName)────────────
// 对账页 = 把「意图」(平台 Service 台账)与「现实」(agent 发现的实例)按 nacosServiceName 关联,
// 实时算出三态(后端不落表、不分页,直接回全集)。纳管动作**无专用端点**:= 预填 namespace +
// nacosServiceName 后调既有 `create('services', ...)` 建 Service,成功后该项即从「已发现未纳管」消失。

/**
 * 「已发现未纳管」收件箱一项(对齐后端 UnmanagedServiceOut,全 camelCase)。
 * 在跑但其 `nacosService` ∉ 任何 Service.nacosServiceName → 待纳管。同一 nacosService 跨多 agent
 * 聚成一项:`agentIds` 汇总所有承载该服务的 agent(= 命名空间 code),`instanceCount` 为该服务
 * 跨 agent 的 active 发现实例合计。纳管时用 `nacosService` 预填新建 Service 的 serviceCode/nacosServiceName。
 */
export interface UnmanagedServiceRow {
  nacosService: string;
  /** 承载该服务的 agent 列表(= 命名空间 code);纳管表单据此预选命名空间。 */
  agentIds: string[];
  /** 该 nacosService 下 active 发现实例总数(跨 agent 合计)。 */
  instanceCount: number;
}

/**
 * 「纳管了但没实例」一项(对齐后端 ManagedDownServiceOut,全 camelCase)。
 * Service.nacosServiceName 非空,却无任何 active 发现实例匹配 → 「该起没起」。
 * `namespaceCode` 可空(无关联命名空间时,列显「-」)。
 */
export interface ManagedDownServiceRow {
  serviceCode: string;
  nacosServiceName: string;
  /** 所属命名空间 code;可空(列显「-」)。 */
  namespaceCode: string | null;
}

/**
 * 服务对账响应(对齐后端 ReconciliationOut,全 camelCase)。
 * - `runningButUnmanaged`:已发现未纳管(收件箱)。
 * - `managedButDown`:纳管了但没活跃实例(该起没起)。
 * - `versionDrift`:**本期恒空**(DiscoveredNode 暂无实例携带的插件版本字段,无从比对);
 *   类型留 `unknown[]` 占位,待后端实现后再细化(前端本期仅展示「暂无」占位)。
 */
export interface ReconciliationResult {
  runningButUnmanaged: UnmanagedServiceRow[];
  managedButDown: ManagedDownServiceRow[];
  versionDrift: unknown[];
}

/**
 * 服务对账:GET /api/nodes/reconciliation。后端实时计算、**不分页**(直接回三态全集)。
 * 失败由调用页自行兜底(本端点为只读拉取,全局兜底 toast 已由 client 拦截器统一处理)。
 */
export async function getReconciliation(): Promise<ReconciliationResult> {
  const r = await client.get<ReconciliationResult>('/api/nodes/reconciliation');
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
