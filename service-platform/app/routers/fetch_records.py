"""获取记录列表路由(Task 11.5,评审 H1)。

`GET /api/fetch-records`(`require_session`):服务端分页信封(评审 M2,审计表无界,
page/pageSize 必备);可选 `?namespaceId=`/`?serviceId=` 过滤;rows 经 LEFT JOIN 回
只读 `namespaceCode`/`serviceCode`/`pluginCode`/`version`(评审 H3),出 camelCase。

约束:
- **只读列表**:获取记录由 Task 11 的分发端点写入(节点拉包时),本端点只读;无写/删。
- **响应全 camelCase**(评审 H2):`response_model=FetchRecordListOut`,不手搓 dict。
- **纵深防御**:逐路由 `Depends(require_session)`;`/api/` 前缀下 default-deny 中间件
  已先挡无/坏 JWT(`/api/fetch-records` 非白名单)。
- 复用 `store.list_rows_joined`(LEFT JOIN + 级联过滤 + `.mappings()`),不新建聚合查询。
"""

from __future__ import annotations

import math

from fastapi import APIRouter, Depends, Query

from app import store
from app.auth import require_session
from app.db_models import FetchRecord, Namespace, Plugin, PluginVersion, Service
from app.models import FetchRecordListOut, FetchRecordOut


router = APIRouter(prefix="/api/fetch-records", tags=["获取记录(节点拉包审计)"])

# SELECT 列:fetch_record 本行字段 + LEFT JOIN 回 namespaceCode/serviceCode/pluginCode/version。
_LIST_COLUMNS = (
    FetchRecord.id,
    FetchRecord.namespace_id,
    FetchRecord.service_id,
    FetchRecord.plugin_id,
    FetchRecord.plugin_version_id,
    FetchRecord.fetch_date,
    FetchRecord.remark,
    Namespace.code.label("namespace_code"),
    Service.service_code.label("service_code"),
    Plugin.code.label("plugin_code"),
    PluginVersion.version.label("version"),
)

# LEFT JOIN 链:fetch_record →(namespace / service / plugin / plugin_version)各一跳。
_OUTER_JOINS = (
    (Namespace, Namespace.id == FetchRecord.namespace_id),
    (Service, Service.id == FetchRecord.service_id),
    (Plugin, Plugin.id == FetchRecord.plugin_id),
    (PluginVersion, PluginVersion.id == FetchRecord.plugin_version_id),
)


@router.get(
    "",
    response_model=FetchRecordListOut,
    summary="获取记录列表(服务端分页)",
    description=(
        "分页返回节点拉包审计记录;LEFT JOIN 回 namespaceCode/serviceCode/pluginCode/version;"
        "支持 ?namespaceId=/?serviceId= 级联过滤。审计表无界,page/pageSize 必备(评审 M2)。"
    ),
)
async def list_fetch_records(
    _: str = Depends(require_session),
    namespace_id: int | None = Query(default=None, alias="namespaceId", title="按命名空间过滤"),
    service_id: int | None = Query(default=None, alias="serviceId", title="按服务过滤"),
    page: int = Query(default=1, ge=1, title="页码"),
    page_size: int = Query(default=20, ge=1, le=200, alias="pageSize", title="每页条数"),
) -> FetchRecordListOut:
    filters = []
    if namespace_id is not None:
        filters.append(FetchRecord.namespace_id == namespace_id)
    if service_id is not None:
        filters.append(FetchRecord.service_id == service_id)

    rows, count = store.list_rows_joined(
        FetchRecord,
        columns=_LIST_COLUMNS,
        outer_joins=_OUTER_JOINS,
        page=page,
        page_size=page_size,
        filters=filters,
        order_by=FetchRecord.id.desc(),  # 审计列表最新优先
    )
    return FetchRecordListOut(
        count=count,
        rows=[FetchRecordOut.model_validate(row) for row in rows],
        page=page,
        page_size=page_size,
        total_page=math.ceil(count / page_size) if page_size else 0,
    )
