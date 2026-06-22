"""service 台账 CRUD 路由(Task 6b)。

`/api/services`(GET 列表信封 / POST 201)、`/api/services/{id}`(GET / PATCH /
DELETE 204)。约束:
- **响应全 camelCase**(评审 H2):一律 `response_model=*Out`,**不手搓 dict**。
- **列表回名(评审 H3)**:全字段 + LEFT JOIN `namespaceCode`(namespace 不存在时为 None)。
- **级联过滤(评审 M3)**:list 支持 `?namespaceId=` 真过滤(WHERE service.namespace_id=...)。
- **唯一冲突 → 409**:`UNIQUE(namespace_id, service_code)` 冲突 → `store.Conflict` → 409。
- **纵深防御**:逐路由 `Depends(require_session)`;`/api/` 前缀下中间件已先挡无/坏 JWT。
"""

from __future__ import annotations

import math

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app import store
from app.auth import require_session
from app.db_models import Namespace, Service
from app.models import (
    ServiceImageListOut,
    ServiceImageOut,
    ServiceImageSetCurrentIn,
    ServiceIn,
    ServiceListOut,
    ServiceOut,
    ServiceUpdate,
)


router = APIRouter(prefix="/api/services", tags=["服务台账"])

# SELECT 列:本行全字段 + LEFT JOIN 回 namespace.code（label=namespace_code → camel namespaceCode）。
_LIST_COLUMNS = (
    Service.id,
    Service.namespace_id,
    Service.service_code,
    Service.name,
    Service.dir,
    Service.default_image,
    Service.nacos_service_name,
    Namespace.code.label("namespace_code"),
)


@router.get("", response_model=ServiceListOut, summary="服务列表", description="分页返回服务台账;LEFT JOIN 回 namespaceCode;支持 ?namespaceId= 级联过滤。")
async def list_services(
    _: str = Depends(require_session),
    namespace_id: int | None = Query(default=None, alias="namespaceId", title="按命名空间过滤"),
    page: int = Query(default=1, ge=1, title="页码"),
    page_size: int = Query(default=20, ge=1, le=200, alias="pageSize", title="每页条数"),
) -> ServiceListOut:
    filters = [Service.namespace_id == namespace_id] if namespace_id is not None else []
    rows, count = store.list_rows_joined(
        Service,
        columns=_LIST_COLUMNS,
        outer_joins=[(Namespace, Namespace.id == Service.namespace_id)],
        page=page,
        page_size=page_size,
        filters=filters,
    )
    return ServiceListOut(
        count=count,
        rows=[ServiceOut.model_validate(row) for row in rows],
        page=page,
        page_size=page_size,
        total_page=math.ceil(count / page_size) if page_size else 0,
    )


@router.post("", response_model=ServiceOut, status_code=status.HTTP_201_CREATED, summary="创建服务", description="新增一条服务台账;UNIQUE(namespaceId, serviceCode) 重复 → 409。")
async def create_service(
    body: ServiceIn,
    _: str = Depends(require_session),
) -> ServiceOut:
    # by_alias=False:取 snake 字段名（ORM 列名）；MODEL_CONFIG 默认 serialize_by_alias=True 会出 camel，需显式覆盖。
    try:
        record = store.create_row(Service, body.model_dump(by_alias=False))
    except store.Conflict:
        raise HTTPException(status.HTTP_409_CONFLICT, "service code already exists in namespace")
    return ServiceOut.model_validate(record)


@router.get("/{service_id}", response_model=ServiceOut, summary="查询单个服务", description="按 id 查询;不存在 → 404。")
async def get_service(
    service_id: int,
    _: str = Depends(require_session),
) -> ServiceOut:
    record = store.get_row(Service, service_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "service not found")
    return ServiceOut.model_validate(record)


@router.patch("/{service_id}", response_model=ServiceOut, summary="更新服务", description="局部更新(只覆盖传入字段);不存在 → 404,UNIQUE 冲突 → 409。")
async def update_service(
    service_id: int,
    body: ServiceUpdate,
    _: str = Depends(require_session),
) -> ServiceOut:
    values = body.model_dump(exclude_unset=True, by_alias=False)
    try:
        record = store.update_row(Service, service_id, values)
    except store.Conflict:
        raise HTTPException(status.HTTP_409_CONFLICT, "service code already exists in namespace")
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "service not found")
    return ServiceOut.model_validate(record)


@router.delete("/{service_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response, summary="删除服务", description="按 id 删除;不存在 → 404,成功 → 204(无响应体)。")
async def delete_service(
    service_id: int,
    _: str = Depends(require_session),
) -> Response:
    if not store.delete_row(Service, service_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "service not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- 镜像台账(P4-4,纯增量,本期不接 redeploy 寻址) -----------------------


@router.get(
    "/{service_id}/images",
    response_model=ServiceImageListOut,
    summary="服务镜像台账列表",
    description="返回该 service 的镜像历史(createdAt 倒序);信封含 count/rows/page/pageSize/totalPage。",
)
async def list_service_images(
    service_id: int,
    _: str = Depends(require_session),
) -> ServiceImageListOut:
    # 镜像历史数据量小(单 service 通常寥寥数行),不分页:一次取全集装进信封(page=1, pageSize=count)。
    rows = store.list_service_images(service_id)
    count = len(rows)
    return ServiceImageListOut(
        count=count,
        rows=[ServiceImageOut.model_validate(row) for row in rows],
        page=1,
        page_size=count,
        total_page=1 if count else 0,
    )


@router.post(
    "/{service_id}/images/set-current",
    response_model=ServiceImageOut,
    summary="设置当前镜像",
    description="把指定 image 置为该 service 的当前镜像(单活:同 service 其它行 isCurrent 清空);返回置后的当前镜像行。",
)
async def set_current_service_image(
    service_id: int,
    body: ServiceImageSetCurrentIn,
    _: str = Depends(require_session),
) -> ServiceImageOut:
    record = store.set_current_image(service_id, body.image)
    return ServiceImageOut.model_validate(record)
