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

from datetime import datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict


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


class NamespaceUpdate(BaseModel):
    """PATCH 局部更新:全字段可选;未传字段保持原值(路由按 `exclude_unset` 取增量)。"""

    model_config = MODEL_CONFIG

    code: str | None = None
    name: str | None = None


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
    """版本台账响应:id + pluginId + version(= package.json.version)+ name。"""

    model_config = MODEL_CONFIG

    id: int
    plugin_id: int
    version: str
    name: str | None = None


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
