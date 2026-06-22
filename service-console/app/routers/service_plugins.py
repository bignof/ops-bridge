"""service_plugin 关联台账路由(Task 6b)。

`/api/service-plugins`(GET 列表信封 / POST 201 / DELETE 204)。**仅 list/create/destroy
(无 update)** —— 关联行是 (service_id, plugin_id) 多对多事实,改即删旧建新。约束:
- **响应全 camelCase**(评审 H2):一律 `response_model=*Out`,**不手搓 dict**。
- **列表回名(评审 H3)**:`id/serviceId/pluginId` + LEFT JOIN 回
  `namespaceCode`(经 service→namespace 两跳)/ `serviceCode` / `pluginCode`。
- **级联过滤(评审 M3)**:list 支持 `?serviceId=` 真过滤(WHERE service_plugin.service_id=...)。
- **唯一冲突 → 409**:`UNIQUE(service_id, plugin_id)` 冲突 → `store.Conflict` → 409。
- **纵深防御**:逐路由 `Depends(require_session)`;`/api/` 前缀下中间件已先挡无/坏 JWT。
"""

from __future__ import annotations

import math

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app import store
from app.auth import require_session
from app.db_models import Namespace, Plugin, Service, ServicePlugin
from app.models import ServicePluginIn, ServicePluginListOut, ServicePluginOut


router = APIRouter(prefix="/api/service-plugins", tags=["服务插件关联"])

# SELECT 列:本行字段 + 经 service→namespace 两跳回 namespaceCode、service 回 serviceCode、plugin 回 pluginCode。
_LIST_COLUMNS = (
    ServicePlugin.id,
    ServicePlugin.service_id,
    ServicePlugin.plugin_id,
    Namespace.code.label("namespace_code"),
    Service.service_code.label("service_code"),
    Plugin.code.label("plugin_code"),
)

# LEFT JOIN 链:service_plugin → service → namespace,service_plugin → plugin（均 outerjoin，缺关联回名为 None 不丢主行）。
_OUTER_JOINS = (
    (Service, Service.id == ServicePlugin.service_id),
    (Namespace, Namespace.id == Service.namespace_id),
    (Plugin, Plugin.id == ServicePlugin.plugin_id),
)


@router.get("", response_model=ServicePluginListOut, summary="服务插件关联列表", description="分页返回关联台账;LEFT JOIN 回 namespaceCode/serviceCode/pluginCode;支持 ?serviceId= 级联过滤。")
async def list_service_plugins(
    _: str = Depends(require_session),
    service_id: int | None = Query(default=None, alias="serviceId", title="按服务过滤"),
    page: int = Query(default=1, ge=1, title="页码"),
    page_size: int = Query(default=20, ge=1, le=200, alias="pageSize", title="每页条数"),
) -> ServicePluginListOut:
    filters = [ServicePlugin.service_id == service_id] if service_id is not None else []
    rows, count = store.list_rows_joined(
        ServicePlugin,
        columns=_LIST_COLUMNS,
        outer_joins=_OUTER_JOINS,
        page=page,
        page_size=page_size,
        filters=filters,
    )
    return ServicePluginListOut(
        count=count,
        rows=[ServicePluginOut.model_validate(row) for row in rows],
        page=page,
        page_size=page_size,
        total_page=math.ceil(count / page_size) if page_size else 0,
    )


@router.post("", response_model=ServicePluginOut, status_code=status.HTTP_201_CREATED, summary="创建服务插件关联", description="新增一条 (serviceId, pluginId) 关联;UNIQUE 重复 → 409。")
async def create_service_plugin(
    body: ServicePluginIn,
    _: str = Depends(require_session),
) -> ServicePluginOut:
    # by_alias=False:取 snake 字段名（ORM 列名）；MODEL_CONFIG 默认 serialize_by_alias=True 会出 camel，需显式覆盖。
    try:
        record = store.create_row(ServicePlugin, body.model_dump(by_alias=False))
    except store.Conflict:
        raise HTTPException(status.HTTP_409_CONFLICT, "service-plugin link already exists")
    return ServicePluginOut.model_validate(record)


@router.delete("/{service_plugin_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response, summary="删除服务插件关联", description="按 id 删除;不存在 → 404,成功 → 204(无响应体)。")
async def delete_service_plugin(
    service_plugin_id: int,
    _: str = Depends(require_session),
) -> Response:
    if not store.delete_row(ServicePlugin, service_plugin_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "service-plugin link not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
