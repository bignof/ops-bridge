"""发布/历史激活/回滚 + releases 列表路由(Task 10)。

`POST /api/releases/publish`、`/api/releases/reactivate`、`/api/releases/rollback`、
`GET /api/releases`。绑定约束:

- **单活 + 状态机**:三个写端点委派 `store.publish/reactivate/rollback`(with_for_update
  锁行 + 清 key 与置 key 之间 `s.flush()` 分隔 + IntegrityError→Conflict)。语义见 store。
- **错误映射**:`store.NotFound` → 404(绑定未建 / spv 不存在 / 无可回滚候选);
  `store.Conflict` → 409(非当前 active 回滚 / 并发置活撞 UNIQUE)。
- **H4 releases list 两视图(同端点,不新建聚合端点)**:不传 filter 或 `isActive=true`
  → **主表**(每 (service,plugin) 一行 active);传 `serviceId`+`pluginId` → 该绑定
  **版本历史**(全部版本,按 versionOrder 升序)。两视图均 LEFT JOIN 回
  `serviceCode`/`pluginCode`/`version`(+`namespaceCode`)只读名称(评审 H3),信封分页(评审 M2)。
- **响应全 camelCase**(评审 H2):一律 `response_model=*Out`,不手搓 dict。
- **纵深防御**:逐路由 `Depends(require_session)`;`/api/` 前缀下 default-deny 中间件已先挡无/坏 JWT。
"""

from __future__ import annotations

import math

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app import store
from app.auth import require_session
from app.db_models import Namespace, Plugin, PluginVersion, Service, ServicePluginVersion
from app.models import (
    ReleaseListOut,
    ReleaseOut,
    ReleasePublishIn,
    ReleaseSpvIn,
)


router = APIRouter(prefix="/api/releases", tags=["发布/历史激活/回滚"])

# SELECT 列:service_plugin_version 本行状态字段 + LEFT JOIN 回 serviceCode/pluginCode/version(+namespaceCode)。
_LIST_COLUMNS = (
    ServicePluginVersion.id,
    ServicePluginVersion.service_plugin_id,
    ServicePluginVersion.service_id,
    ServicePluginVersion.plugin_id,
    ServicePluginVersion.plugin_version_id,
    ServicePluginVersion.version_order,
    ServicePluginVersion.is_active,
    ServicePluginVersion.is_rolled_back,
    ServicePluginVersion.spv_active_key,
    Namespace.code.label("namespace_code"),
    Service.service_code.label("service_code"),
    Plugin.code.label("plugin_code"),
    PluginVersion.version.label("version"),
)

# LEFT JOIN 链:spv → service → namespace(两跳)、spv → plugin、spv → plugin_version(回版本号)。
_OUTER_JOINS = (
    (Service, Service.id == ServicePluginVersion.service_id),
    (Namespace, Namespace.id == Service.namespace_id),
    (Plugin, Plugin.id == ServicePluginVersion.plugin_id),
    (PluginVersion, PluginVersion.id == ServicePluginVersion.plugin_version_id),
)


def _to_out(record: ServicePluginVersion) -> ReleaseOut:
    """写端点回 ORM 行 → ReleaseOut(名称列写端点不回,留 None;列表端点经 JOIN 才填)。"""
    return ReleaseOut.model_validate(record)


@router.get(
    "",
    response_model=ReleaseListOut,
    summary="发布列表(主表 / 历史两视图)",
    description=(
        "评审 H4 两视图(同端点,不新建聚合端点):不传 filter 或 isActive=true → 主表"
        "(每 (serviceId,pluginId) 一行 active);传 serviceId+pluginId → 该绑定版本历史"
        "(全部版本,versionOrder 升序)。两视图均 LEFT JOIN 回 serviceCode/pluginCode/version"
        "(+namespaceCode),信封分页。"
    ),
)
async def list_releases(
    _: str = Depends(require_session),
    service_id: int | None = Query(default=None, alias="serviceId", title="按服务过滤(配 pluginId 取历史)"),
    plugin_id: int | None = Query(default=None, alias="pluginId", title="按插件过滤(配 serviceId 取历史)"),
    is_active: bool | None = Query(default=None, alias="isActive", title="主表过滤(true=每绑定一行 active)"),
    page: int = Query(default=1, ge=1, title="页码"),
    page_size: int = Query(default=20, ge=1, le=200, alias="pageSize", title="每页条数"),
) -> ReleaseListOut:
    # 视图判定:serviceId+pluginId 同时给 → 历史视图(该绑定全部版本,versionOrder 升序);
    # 否则 → 主表视图(只取 is_active=True,每绑定一行)。isActive=true 与「不传 filter」均落主表。
    if service_id is not None and plugin_id is not None:
        filters = [
            ServicePluginVersion.service_id == service_id,
            ServicePluginVersion.plugin_id == plugin_id,
        ]
        order_by = ServicePluginVersion.version_order.asc()
    else:
        filters = [ServicePluginVersion.is_active.is_(True)]
        order_by = ServicePluginVersion.id.asc()

    rows, count = store.list_rows_joined(
        ServicePluginVersion,
        columns=_LIST_COLUMNS,
        outer_joins=_OUTER_JOINS,
        page=page,
        page_size=page_size,
        filters=filters,
        order_by=order_by,
    )
    return ReleaseListOut(
        count=count,
        rows=[ReleaseOut.model_validate(row) for row in rows],
        page=page,
        page_size=page_size,
        total_page=math.ceil(count / page_size) if page_size else 0,
    )


@router.post(
    "/publish",
    response_model=ReleaseOut,
    status_code=status.HTTP_201_CREATED,
    summary="发布新版本",
    description="为已绑定的 (serviceId,pluginId) 追加一版并置唯一 active;未绑定 → 404,并发置活冲突 → 409。",
)
async def publish_release(
    body: ReleasePublishIn,
    _: str = Depends(require_session),
) -> ReleaseOut:
    try:
        record = store.publish(body.service_id, body.plugin_id, body.plugin_version_id)
    except store.NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    except store.Conflict:
        raise HTTPException(status.HTTP_409_CONFLICT, "并发发布冲突,请重试")
    return _to_out(record)


@router.post(
    "/reactivate",
    response_model=ReleaseOut,
    summary="历史版本重新激活",
    description="把指定历史版本置为唯一 active(同时清 is_rolled_back);spv 不存在 → 404,并发冲突 → 409。",
)
async def reactivate_release(
    body: ReleaseSpvIn,
    _: str = Depends(require_session),
) -> ReleaseOut:
    try:
        record = store.reactivate(body.spv_id)
    except store.NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    except store.Conflict:
        raise HTTPException(status.HTTP_409_CONFLICT, "并发激活冲突,请重试")
    return _to_out(record)


@router.post(
    "/rollback",
    response_model=ReleaseOut,
    summary="回滚到前一可用版本",
    description=(
        "把当前 active 行标记 is_rolled_back 并激活其前一未回滚历史版本;spv 不存在 / 无候选 → 404,"
        "spv 非当前 active 或并发冲突 → 409。返回新激活的候选行。"
    ),
)
async def rollback_release(
    body: ReleaseSpvIn,
    _: str = Depends(require_session),
) -> ReleaseOut:
    try:
        record = store.rollback(body.spv_id)
    except store.NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    except store.Conflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return _to_out(record)
