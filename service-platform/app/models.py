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
from typing import Generic, TypeVar

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
