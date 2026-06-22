"""Pydantic 请求/响应模型基座(全计划共用)。

绑定约束(评审 H2 / 跨计划契约):**所有**请求/响应模型统一 camelCase——
`ConfigDict(alias_generator=to_camel, populate_by_name=True, serialize_by_alias=True)`
(照搬 `service-hub/app/models.py` 的 `to_camel` + `MODEL_CONFIG`)。响应**禁止**
手搓 snake dict:store 返回 ORM/snake,路由一律经 `*Out` 模型(`response_model=` +
`model_validate(...)`)序列化成 camelCase。`populate_by_name=True` 使入参既接受
camel(alias)也接受 snake(字段名)。

> Pydantic v2 默认 `extra='ignore'`:前端送错 key 的可选字段会被**静默丢弃**
> (列静默 NULL 且逃过 smoke),故契约不容漂移——新增字段务必三份计划同步。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, field_validator


# namespace.code 白名单(评审 A3):code 即 agentId,会拼进 hub URL 路径段,严禁路径分隔符
# / 段穿越 / query / fragment 等。限定字母数字 . _ - 共 1~255 字符;非法 code create 即 422 拒,
# 杜绝存储型路径注入(code='x/../../dispatch' 实测打到 hub dispatch=全机群 RCE)。
_NAMESPACE_CODE_RE = re.compile(r"^[A-Za-z0-9._-]{1,255}$")
# 纯点段(`.` / `..`)虽落在字符集内,却是路径穿越段,必须额外排除(评审 A3 用例显式拒 `..`)。
_NAMESPACE_CODE_FORBIDDEN = frozenset({".", ".."})


def _validate_namespace_code(value: str) -> str:
    """namespace.code 白名单校验(评审 A3 + 复审 R2,create/PATCH 共用)。

    code(=agentId)会拼进 hub URL 路径段,非法 code 入库即为存储型路径注入隐患;故在入 store
    之前就挡住路径分隔符/穿越/query/fragment/空格/空串。**create 端必填**(NamespaceIn)与
    **PATCH 端可选**(NamespaceUpdate,None 由各自 validator 放行后不会调到这里)共用同一规则,
    避免纵深退化为单闸(R2:此前 PATCH 无校验,可种入 `x/../../dispatch`)。
    """
    if not _NAMESPACE_CODE_RE.fullmatch(value) or value in _NAMESPACE_CODE_FORBIDDEN:
        raise ValueError("code 仅允许字母数字与 . _ -,长度 1~255(不得含 / # ? 空格,亦不得为 . 或 ..)")
    return value


def to_camel(value: str) -> str:
    parts = value.split("_")
    return parts[0] + "".join(part.capitalize() for part in parts[1:])


MODEL_CONFIG = ConfigDict(
    alias_generator=to_camel,
    populate_by_name=True,
    serialize_by_alias=True,
    from_attributes=True,  # 支持 model_validate(ORM 实例),store 返回 ORM 行直接转模型
)


T = TypeVar("T")


class ListEnvelope(BaseModel, Generic[T]):
    """列表信封(评审 M2):`{count, rows, page, pageSize, totalPage}` + 服务端分页。

    全计划列表端点统一用它;字段经 `to_camel` 后即 camelCase(count/rows/page 不变,
    `page_size`→`pageSize`,`total_page`→`totalPage`)。

    注:作为 FastAPI `response_model` 用时,**优先派生具体子类**(如
    `PluginListOut(ListEnvelope[PluginOut])`)而非直接 `ListEnvelope[X]` 参数化——
    Pydantic v2 在「泛型 + alias_generator 生成的 alias 恰等于字段名(单词字段
    如 name/id/code)」叠加 FastAPI 响应模型重建时会抛 `UnsupportedFieldAttributeWarning`
    噪声(不影响序列化正确性,但污染输出);具体子类避开该重建路径。
    """

    model_config = MODEL_CONFIG

    count: int
    rows: list[T]
    page: int
    page_size: int
    total_page: int


# --- plugin 资源(Task 6a) -------------------------------------------------


class PluginIn(BaseModel):
    model_config = MODEL_CONFIG

    code: str
    name: str | None = None


class PluginUpdate(BaseModel):
    """PATCH 局部更新:全字段可选;未传字段保持原值(路由按 `exclude_unset` 取增量)。"""

    model_config = MODEL_CONFIG

    code: str | None = None
    name: str | None = None


class PluginOut(BaseModel):
    model_config = MODEL_CONFIG

    id: int
    code: str
    name: str | None = None


class PluginListOut(ListEnvelope[PluginOut]):
    """plugin 列表响应(具体子类,避开泛型 response_model 的 Pydantic 警告路径)。"""


# --- namespace 资源(Task 6b) ----------------------------------------------


class NamespaceIn(BaseModel):
    model_config = MODEL_CONFIG

    code: str
    name: str | None = None

    @field_validator("code")
    @classmethod
    def _validate_code(cls, value: str) -> str:
        # 评审 A3:code(=agentId)会拼进 hub URL 路径段,非法 code create 即拒(Pydantic → 422),
        # 在入 store 之前就挡住路径分隔符/穿越/query/fragment/空格/空串,杜绝存储型路径注入。
        return _validate_namespace_code(value)


class NamespaceUpdate(BaseModel):
    """PATCH 局部更新:全字段可选;未传字段保持原值(路由按 `exclude_unset` 取增量)。"""

    model_config = MODEL_CONFIG

    code: str | None = None
    name: str | None = None

    @field_validator("code")
    @classmethod
    def _validate_code(cls, value: str | None) -> str | None:
        # 复审 R2:PATCH 端 code 同样套白名单,杜绝经 PATCH 种入 `x/../../dispatch`(纵深第一道闸
        # 不退化为单闸)。**None 放行**——PATCH 不传 code 是合法的局部更新,不报错;非 None 才校验。
        if value is None:
            return None
        return _validate_namespace_code(value)


class NamespaceOut(BaseModel):
    """列表/查询响应:`name` 空回退 `code`(评审 H3 由路由层填);在线/心跳 P2 实时读 hub 填。"""

    model_config = MODEL_CONFIG

    id: int
    code: str
    name: str | None = None


class NamespaceCreateOut(NamespaceOut):
    """create 专用响应:额外携带一次性 `agentKey`(show-once,不入库,仅本次返回)。"""

    agent_key: str


class NamespaceRotateKeyOut(BaseModel):
    """rotate-key 响应:仅一次性 `agentKey`(show-once,不入库;旧密钥 hub 侧已失效)。"""

    model_config = MODEL_CONFIG

    agent_key: str


class NamespaceRotatePullTokenOut(BaseModel):
    """rotate-pull-token 响应:仅一次性 `pullToken` 明文(show-once,库内只存哈希)。"""

    model_config = MODEL_CONFIG

    pull_token: str


class NamespaceListOut(ListEnvelope[NamespaceOut]):
    """namespace 列表响应(具体子类,避开泛型 response_model 的 Pydantic 警告路径)。"""


# --- service 资源(Task 6b) ------------------------------------------------


class ServiceIn(BaseModel):
    model_config = MODEL_CONFIG

    namespace_id: int
    service_code: str
    name: str | None = None
    dir: str | None = None
    default_image: str | None = None
    nacos_service_name: str | None = None


class ServiceUpdate(BaseModel):
    model_config = MODEL_CONFIG

    namespace_id: int | None = None
    service_code: str | None = None
    name: str | None = None
    dir: str | None = None
    default_image: str | None = None
    nacos_service_name: str | None = None


class ServiceOut(BaseModel):
    """全字段 + LEFT JOIN 回 `namespaceCode`(评审 H3,namespace 不存在时为 None)。"""

    model_config = MODEL_CONFIG

    id: int
    namespace_id: int
    service_code: str
    name: str | None = None
    dir: str | None = None
    default_image: str | None = None
    nacos_service_name: str | None = None
    namespace_code: str | None = None


class ServiceListOut(ListEnvelope[ServiceOut]):
    """service 列表响应(具体子类,避开泛型 response_model 的 Pydantic 警告路径)。"""


# --- service_image 镜像台账(P4-4) ----------------------------------------


class ServiceImageOut(BaseModel):
    """镜像台账行响应:id + serviceId + image + isCurrent + createdAt(camelCase)。"""

    model_config = MODEL_CONFIG

    id: int
    service_id: int
    image: str
    is_current: bool
    created_at: datetime


class ServiceImageListOut(ListEnvelope[ServiceImageOut]):
    """镜像台账列表响应(具体子类,避开泛型 response_model 的 Pydantic 警告路径)。"""


class ServiceImageSetCurrentIn(BaseModel):
    """set-current 请求 body:`{image}`(把该 image 置为本 service 当前镜像)。"""

    model_config = MODEL_CONFIG

    image: str


# --- service_plugin 资源(Task 6b) -----------------------------------------


class ServicePluginIn(BaseModel):
    model_config = MODEL_CONFIG

    service_id: int
    plugin_id: int


class ServicePluginOut(BaseModel):
    """id + serviceId/pluginId + LEFT JOIN 回 namespaceCode/serviceCode/pluginCode(评审 H3)。"""

    model_config = MODEL_CONFIG

    id: int
    service_id: int
    plugin_id: int
    namespace_code: str | None = None
    service_code: str | None = None
    plugin_code: str | None = None


class ServicePluginListOut(ListEnvelope[ServicePluginOut]):
    """service_plugin 列表响应(具体子类,避开泛型 response_model 的 Pydantic 警告路径)。"""


# --- plugin_version 资源 / 插件上传(Task 9) -------------------------------


class PluginVersionOut(BaseModel):
    """版本台账响应:id + pluginId + version(= package.json.version)+ name。

    评审 A3(契约漂移第 3 例):列表端点 LEFT JOIN 回只读 `pluginCode`(= plugin.code)
    与 `filename`(= plugin_attachment.filename,P1a UNIQUE(plugin_version_id) → 1:1),
    供上传页 ProTable 的「插件编码 / 文件名」列填充(此前 store.list_rows 无 JOIN → 两列恒空)。
    单条 get 端点不 JOIN,两字段留 None(无影响,前端列表页才依赖)。
    """

    model_config = MODEL_CONFIG

    id: int
    plugin_id: int
    version: str
    name: str | None = None
    plugin_code: str | None = None
    filename: str | None = None


class PluginVersionListOut(ListEnvelope[PluginVersionOut]):
    """plugin_version 列表响应(具体子类,避开泛型 response_model 的 Pydantic 警告路径)。"""


class PluginUploadOut(BaseModel):
    """上传响应:新建的 pluginVersionId / attachmentId + version(取自 package.json)。"""

    model_config = MODEL_CONFIG

    plugin_version_id: int
    attachment_id: int
    version: str


# --- releases 发布/历史激活/回滚(Task 10) ---------------------------------


class ReleasePublishIn(BaseModel):
    """发布请求:为 (serviceId, pluginId) 绑定追加一版并置为唯一 active。"""

    model_config = MODEL_CONFIG

    service_id: int
    plugin_id: int
    plugin_version_id: int


class ReleaseSpvIn(BaseModel):
    """reactivate / rollback 请求:仅 spvId(service_plugin_version 行 id)。"""

    model_config = MODEL_CONFIG

    spv_id: int


class ReleaseOut(BaseModel):
    """release(service_plugin_version)响应:本行状态字段 + LEFT JOIN 回
    `serviceCode` / `pluginCode` / `version`(+ `namespaceCode`)只读名称(评审 H3)。

    `spvActiveKey` 是 app 维护的单活兜底列(active 时非空,否则 None);随响应返回便于
    前端/审计核验「当前唯一 active」不变式。
    """

    model_config = MODEL_CONFIG

    id: int
    service_plugin_id: int
    service_id: int
    plugin_id: int
    plugin_version_id: int
    version_order: int
    is_active: bool
    is_rolled_back: bool
    spv_active_key: str | None = None
    publish_time: datetime | None = None
    namespace_code: str | None = None
    service_code: str | None = None
    plugin_code: str | None = None
    version: str | None = None


class ReleaseListOut(ListEnvelope[ReleaseOut]):
    """release 列表响应(具体子类,避开泛型 response_model 的 Pydantic 警告路径)。"""


# --- fetch_record 获取记录列表(Task 11.5,评审 H1) ------------------------


class FetchRecordOut(BaseModel):
    """获取记录响应:本行字段 + LEFT JOIN 回 namespaceCode/serviceCode/pluginCode/version
    只读名称(评审 H3,关联缺失时为 None)。审计只读,无写入模型。"""

    model_config = MODEL_CONFIG

    id: int
    namespace_id: int
    service_id: int
    plugin_id: int
    plugin_version_id: int
    fetch_date: datetime
    remark: str | None = None
    namespace_code: str | None = None
    service_code: str | None = None
    plugin_code: str | None = None
    version: str | None = None


class FetchRecordListOut(ListEnvelope[FetchRecordOut]):
    """获取记录列表响应(具体子类,避开泛型 response_model 的 Pydantic 警告路径)。"""


# --- 节点聚合(Task 9b,节点页) -------------------------------------------


class NodeOut(BaseModel):
    """节点行响应:权威源 = 平台 Service 表(每行 = (agent×service)),叠加 hub 实时态。

    - 静态(Service 表 + LEFT JOIN namespace.code → `agentId`/`namespaceCode`):
      `serviceCode` / `dir` / `defaultImage` / `nacosServiceName`。
    - 实时(hub `list_agents`):`online` / `lastSeen`(= AgentSnapshot.lastSeenAt)。
    - 健康(hub `list_instances`):`healthyCount`(健康实例数)。

    降级语义:某行的 `list_instances` 超时 / 失败 → `healthyCount=None` 且 `degraded=True`;
    离线行 / 无 `nacosServiceName` 的行不发 fan-out → `healthyCount=None`、`degraded=False`。
    """

    model_config = MODEL_CONFIG

    agent_id: str
    service_code: str
    namespace_code: str | None = None
    dir: str | None = None
    default_image: str | None = None
    nacos_service_name: str | None = None
    online: bool
    last_seen: datetime | None = None
    healthy_count: int | None = None
    degraded: bool


class NodeListOut(ListEnvelope[NodeOut]):
    """节点列表响应(具体子类,避开泛型 response_model 的 Pydantic 警告路径)。"""


class DiscoveredNodeOut(BaseModel):
    """实例(DiscoveredNode)行响应:agent 自动发现上报、console 落库的物理容器节点(P3-5)。

    一行 = 某 agent 名下一个 compose 容器(实例);`dir`/`image`/`composeProject` 为 agent 发现的
    **权威值**(发现权威,非手配)。`status`:`active`(本轮在报)/ `stale`(失联或本轮缺席,保留
    可定位,评审 M8);`healthy`/`nacosService` 来自 nacos 匹配(无匹配为 None)。
    """

    model_config = MODEL_CONFIG

    agent_id: str
    container_name: str
    container_id: str | None = None
    compose_project: str | None = None
    compose_service: str | None = None
    dir: str | None = None
    image: str | None = None
    running: bool
    nacos_service: str | None = None
    healthy: bool | None = None
    status: str
    heartbeat_at: datetime | None = None
    first_seen_at: datetime | None = None


class DiscoveredNodeListOut(ListEnvelope[DiscoveredNodeOut]):
    """实例列表响应(具体子类,避开泛型 response_model 的 Pydantic 警告路径)。"""


# --- 服务对账(P3-7,意图 Service ⋈ 现实 DiscoveredNode by nacosServiceName) ---
#
# 对账实时计算(不落表),把「意图」(Service 表)与「现实」(DiscoveredNode 发现实例)按
# `nacos_service_name` 关联,产出三态(设计 §3.4):
# - runningButUnmanaged:发现实例的 nacosService ∉ 任何 Service.nacos_service_name → 「已发现未纳管」收件箱。
# - managedButDown:Service.nacos_service_name 非空但无任何 active 发现实例 → 该起没起。
# - versionDrift:本期空(DiscoveredNode 暂无插件版本字段,见端点 TODO)。
# 纳管动作不新增端点:纳管 = 预填 namespace + nacosServiceName 后调既有 `POST /api/services` create。


class UnmanagedServiceOut(BaseModel):
    """「已发现未纳管」收件箱一项:在跑但无对应 Service 的 nacosService。

    同一 `nacosService` 跨多 agent 的实例聚成一项(评审 H-5):`agentIds` 汇总所有承载该服务的
    agent,`instanceCount` 为该 nacosService 下 active 发现实例总数(跨 agent 合计)。前端「纳管」时
    用 `nacosService` 预填 `POST /api/services` 的 nacosServiceName(namespace 由用户选 agent 决定)。
    """

    model_config = MODEL_CONFIG

    nacos_service: str
    agent_ids: list[str]
    instance_count: int


class ManagedDownServiceOut(BaseModel):
    """「纳管了但没实例」一项:Service.nacos_service_name 非空,却无任何 active 发现实例匹配。

    `serviceCode`(分发标识)/ `nacosServiceName`(对账 link key)/ `namespaceCode`(所属 agent)
    供前端定位「该起没起」的服务。
    """

    model_config = MODEL_CONFIG

    service_code: str
    nacos_service_name: str
    namespace_code: str | None = None


class ReconciliationOut(BaseModel):
    """服务对账响应:三态各一组。数据量 = 服务数(通常不大),**不分页**(直接返回全集)。

    `versionDrift` 本期恒为空数组(DiscoveredNode 暂无实例携带的插件版本字段,无从比对;镜像漂移另见
    设计 P4-4)——返回空而非硬凑,待实例上报插件版本后再实现。
    """

    model_config = MODEL_CONFIG

    running_but_unmanaged: list[UnmanagedServiceOut]
    managed_but_down: list[ManagedDownServiceOut]
    version_drift: list[dict] = []


# --- 节点操作下发 + 操作审计(Task 10b) -----------------------------------


class NodeActionIn(BaseModel):
    """节点操作请求 body:`{mode?, allowLastInstance?, composeProject?}`。

    寻址(agentId / serviceCode / action)走 URL 路径段;**dir / image / containerId 的权威源是
    agent 自动发现上报的 DiscoveredNode**(P3-6「发现权威」),客户端仍不传路径或 image(防越权操作
    非授权目录 / 拉非白名单镜像)——平台据 (agentId, Service.nacosServiceName) 反查 DiscoveredNode
    取权威值,仅在该 (agent, nacosService) 暂无 DiscoveredNode 时回退手配的 `Service.dir/defaultImage`。
    `mode`:restart 缺省按 graceful;stop / redeploy 必填(路由层校验,缺省 → 400)。
    `allowLastInstance` 仅 force stop 透传给 hub 护栏(允许停最后一个健康实例)。
    `composeProject`:**多实例消歧用**——同一 nacosService 名下存在多个 DiscoveredNode(不同
    compose 工程,如 admin / 2admin)时,必须用它指定要操作的那一个实例;单实例 / 回退场景可不传
    (寻址解析:多实例缺 composeProject → 409)。
    """

    model_config = MODEL_CONFIG

    mode: Literal["graceful", "force"] | None = None
    allow_last_instance: bool = False
    compose_project: str | None = None


class NodeActionOut(BaseModel):
    """节点操作下发响应:区分两条派发路径(评审冲突 3)。

    - `kind="rolling"`:优雅 restart 走 hub `/api/rolling-restart`,返回异步 `taskId`
      (`requestId=None`);SPA 后续可按 taskId 单查 `/api/rolling-restart/{taskId}` 看进度。
    - `kind="command"`:其余操作走 hub dispatch,返回 hub 生成的 `requestId`(`taskId=None`)
      + `accepted`;该命令会出现在 `/api/node-operations` 审计列表。
    """

    model_config = MODEL_CONFIG

    kind: Literal["command", "rolling"]
    request_id: str | None = None
    task_id: str | None = None
    accepted: bool = False


class NodeOperationOut(BaseModel):
    """操作审计行响应:hub `CommandSnapshot` 子集(评审冲突 2,用 `dir` 非 targetDir)。

    取审计页关心字段:谁(`requestedBy` 派生 + `requestSource`)/做了什么(`action` / `mode`)
    / 目标(`dir` / `image`)/ 结果(`status` / `output` / `error`)/ 时间。`output` 与 `error`
    超 1000 字符在路由层截尾(保留末尾,结果通常在尾部),避免审计列表 payload 膨胀。
    本期只代理 hub dispatch 命令;优雅 restart 走 rolling、不进此列表(已知缺口,见报告)。
    """

    model_config = MODEL_CONFIG

    request_id: str
    agent_id: str
    action: str
    mode: str | None = None
    status: str
    requested_by: str | None = None
    request_source: str | None = None
    dir: str | None = None
    image: str | None = None
    output: str | None = None
    error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class NodeOperationsListOut(ListEnvelope[NodeOperationOut]):
    """操作审计列表响应(平台标准信封;BFF 把 hub limit/offset 换算成 page/pageSize)。"""
