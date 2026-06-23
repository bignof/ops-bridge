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

// ── 命名空间(namespaces,P1a NamespaceOut 子集)──────────────────────────────────
// 顶栏命名空间切换器(P3-10)与各页关联选择共用。注意两套过滤口径:
//  - services / fetch-records 等台账页按 **namespaceId**(数值 id)服务端过滤(?namespaceId=)。
//  - instances 发现页按 **namespace = agentId**(自由串)过滤,而 agentId == 命名空间 `code`
//    (见对账页注释「发现侧 agentId 即命名空间 code」)。
// 故切换器需同时持有 id 与 code,下游各取所需(见 NamespaceContext)。

/** 命名空间行(P1a NamespaceOut 子集,全 camelCase)。`code` 即发现侧 agentId。 */
export interface NamespaceRow {
  id: string | number;
  /** 命名空间编码;== 发现实例的 agentId(instances 页按它过滤)。 */
  code: string;
  /** 命名空间别名(可空)。 */
  name?: string;
}

/**
 * 命名空间列表(服务端分页,统一信封):GET /api/namespaces?page=&pageSize=。
 * 切换器一次性拉 pageSize=200(后端各 list 端点硬卡 le=200,>200 需远程搜索,属后续增强)。
 */
export async function listNamespaces<T = NamespaceRow>(
  params: ListParams = {},
): Promise<ListEnvelope<T>> {
  return list<T>('namespaces', params);
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

// ── 镜像台账(service images,P4-4) ────────────────────────────────────────────
// 某 service 的镜像历史 + 「设为当前」。后端(app/routers/services.py)契约:
//  - GET  /api/services/{serviceId}/images          → ServiceImageListOut(数据量小,后端不分页,
//    一次回全集:page=1, pageSize=count, totalPage=1)。
//  - POST /api/services/{serviceId}/images/set-current  body {image} → 置该 image 为当前(同 service
//    单活:其它行 isCurrent 清空),返回置后的当前镜像行。**无纯追加端点**,故「新增镜像」= set-current。

/** 镜像台账行(对齐后端 ServiceImageOut,全 camelCase)。isCurrent 同 service 单活。 */
export interface ServiceImageRow {
  id: number;
  serviceId: number;
  image: string;
  /** 是否当前镜像(同 service 仅一行为 true)。 */
  isCurrent: boolean;
  createdAt: string;
}

/** 镜像台账列表:GET /api/services/{serviceId}/images(后端不分页,信封一次回全集)。 */
export async function listServiceImages<T = ServiceImageRow>(
  serviceId: string | number,
): Promise<ListEnvelope<T>> {
  const r = await client.get<ListEnvelope<T>>(`/api/services/${serviceId}/images`);
  return r.data;
}

/**
 * 设为当前镜像:POST /api/services/{serviceId}/images/set-current(body `{image}`)。
 * 同 service 单活(其它行 isCurrent 清空)。「新增镜像」也走本端点(后端无纯追加端点)→ 新增即置当前。
 * 返回置后的当前镜像行。
 */
export async function setCurrentImage<T = ServiceImageRow>(
  serviceId: string | number,
  image: string,
): Promise<T> {
  const r = await client.post<T>(`/api/services/${serviceId}/images/set-current`, { image });
  return r.data;
}

// ── 投放(Rollout,P4-2/P4-3/P4-5) ─────────────────────────────────────────────
// 投放运行记录 + 逐实例进度 + 失败处置(重试/回滚)+ 发起投放(P4-5 发布弹窗)。
// 后端(app/hub/routers/rollouts.py)走平台 JWT。本层覆盖「记录列表 + 详情进度 + retry/rollback +
// createRollout(发起)」。

/** 投放状态(后端状态机):running 进行中 / done 完成 / degraded 降级完成 / failed 失败。 */
export type RolloutStatus = 'running' | 'done' | 'degraded' | 'failed';

/** 投放记录行(对齐后端 RolloutOut,全 camelCase)。frozen=失败即停的半迁移态(可重试/回滚)。 */
export interface RolloutRow {
  id: string;
  /** 命名空间;可空。 */
  namespace: string | null;
  serviceName: string;
  /** 投放模式:restart / pull-redeploy(后者本期后端 422 占位)。 */
  mode: string;
  /** 触发来源:manual / publish / retry / rollback。 */
  trigger: string;
  /** 本次投放 desired-state 人读摘要;可空。 */
  target: string | null;
  /** 上一版摘要(回滚参考);可空 → 回滚按钮不可用。 */
  previousTarget: string | null;
  status: RolloutStatus;
  /** 失败即停冻结(半迁移态):需人工重试或回滚。 */
  frozen: boolean;
  /** 关联底层滚动任务 id;可空。 */
  rollingTaskId: string | null;
  /** 失败原因摘要;可空。 */
  error: string | null;
  force: boolean;
  createdAt: string;
  finishedAt: string | null;
}

/**
 * 底层滚动任务逐实例进度的单个实例(对齐 hub `_rolling_to_dict` 的 nodes 项)。
 * 跨机滚动每实例一项:`address` 为实例寻址(host:port),`containerId` 容器 id;`agentId` 仅部分场景带。
 * `status`:pending(待滚)/ in-progress(滚动中)/ done(完成)/ failed(失败)/ skipped(跳过)。
 */
export interface RollingTaskNode {
  /** 部分场景带的 agent 标识;跨机滚动多以 address 为准,可空。 */
  agentId?: string | null;
  /** 实例寻址(host:port)。 */
  address: string;
  /** 容器 id;可空。 */
  containerId: string | null;
  status: 'pending' | 'in-progress' | 'done' | 'failed' | 'skipped';
  /** 该实例失败原因;可空。 */
  error?: string | null;
}

/**
 * 底层滚动任务(对齐 hub `_rolling_to_dict`,camelCase)。详情页据 `nodes` 呈现逐实例进度。
 * 与 RolloutOut 形态独立(来自 hub 侧),故宽松透传(后端 RolloutDetailOut.rollingTask 即此结构或 null)。
 */
export interface RollingTask {
  taskId: string;
  agentId: string;
  serviceName: string;
  /** 整体滚动状态(running/done/degraded/failed/interrupted)。 */
  status: string;
  degraded: boolean;
  /** 逐实例进度。 */
  nodes: RollingTaskNode[];
  error: string | null;
  createdAt: string | null;
  updatedAt: string | null;
  finishedAt?: string | null;
}

/** 投放详情(对齐后端 RolloutDetailOut):投放行字段 + 嵌入底层滚动逐实例进度(无关联为 null)。 */
export interface RolloutDetail extends RolloutRow {
  /** 底层滚动逐实例进度;无关联 task 或查不到为 null。 */
  rollingTask: RollingTask | null;
}

/** retry/rollback 响应:新建一条投放,返回 {rolloutId, taskId}。 */
export interface RolloutActionOut {
  rolloutId: string;
  taskId: string;
}

/**
 * 发起投放请求体(对齐后端 RolloutCreateIn,camelCase)。P4-5 发布弹窗组装:
 * - `serviceName`:**必填**,= 该服务的 nacosServiceName(后端按它抢 rolling 锁 + 跨机寻址滚动)。
 * - `namespace`:可空;审计 + 列表过滤用(传该服务的命名空间 code)。
 * - `mode`:'restart'(逐实例 graceful-restart 原地重启,插件变更场景)|
 *   'pull-redeploy'(逐实例 graceful-redeploy 滚动重拉镜像,镜像变更场景)。
 * - `image`:**仅 pull-redeploy 必填**(缺 → 后端 422);restart 时省略(后端忽略)。
 * - `instances`:灰度子集(containerId 列表),与 mode 正交;省略 = 全量滚(集群健康门仍按全集判定)。
 * - `force`:集群健康实例 <2 仍强滚(可能瞬时中断)。
 * - `target`:本次投放 desired-state 人读摘要(审计);**pull-redeploy 的 image 后端不落库**,
 *   故建议 pull-redeploy 时把镜像 tag 填进 target 便于记录页识别。
 * - `trigger`:缺省由后端置 'manual';retry/rollback 两值由后端内部入口覆盖,前端不传。
 */
export interface CreateRolloutParams {
  serviceName: string;
  namespace?: string | null;
  mode: 'restart' | 'pull-redeploy';
  image?: string;
  instances?: string[];
  force?: boolean;
  target?: string;
  trigger?: string;
}

/** 投放记录列表(服务端分页,统一信封):GET /api/rollouts?namespace=&serviceName=&status=&page=&pageSize=。 */
export async function listRollouts<T = RolloutRow>(
  params: ListParams = {},
): Promise<ListEnvelope<T>> {
  return list<T>('rollouts', params);
}

/** 投放详情(含逐实例滚动进度):GET /api/rollouts/{id} → RolloutDetailOut。 */
export async function getRollout<T = RolloutDetail>(id: string): Promise<T> {
  const r = await client.get<T>(`/api/rollouts/${id}`);
  return r.data;
}

/**
 * 发起投放:POST /api/rollouts(body {@link CreateRolloutParams})→ {rolloutId, taskId}。
 * `suppressGlobalError`:发布弹窗对 422(缺 image / 非法 mode)、409(同服务投放进行中)本地精确提示,
 * 故 opt-out 全局兜底防双 toast(401 仍由拦截器统一处理)。
 * ⚠️ opt-out 后非预期失败的可见性由弹窗 catch 自己兜底(generic fallback),不可静默吞。
 */
export async function createRollout(params: CreateRolloutParams): Promise<RolloutActionOut> {
  const r = await client.post<RolloutActionOut>('/api/rollouts', params, {
    suppressGlobalError: true,
  });
  return r.data;
}

/**
 * 重试投放:POST /api/rollouts/{id}/retry → {rolloutId, taskId}。
 * 仅原记录 status=failed 可,否则后端 409(前端按钮只在 failed 行显示;非预期 409 走全局兜底 toast)。
 */
export async function retryRollout(id: string): Promise<RolloutActionOut> {
  const r = await client.post<RolloutActionOut>(`/api/rollouts/${id}/retry`);
  return r.data;
}

/**
 * 回滚投放:POST /api/rollouts/{id}/rollback → {rolloutId, taskId}。
 * 仅 failed 且有 previousTarget 可,否则后端 409(前端按钮只在 failed+previousTarget 非空 时显示)。
 */
export async function rollbackRollout(id: string): Promise<RolloutActionOut> {
  const r = await client.post<RolloutActionOut>(`/api/rollouts/${id}/rollback`);
  return r.data;
}
