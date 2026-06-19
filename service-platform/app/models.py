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
