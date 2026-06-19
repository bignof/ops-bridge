"""plugin 台账 CRUD 路由(Task 6a)。

`/api/plugins`(GET 列表信封 / POST 201)、`/api/plugins/{id}`(GET / PATCH /
DELETE 204)。约束:
- **响应全 camelCase**(评审 H2):一律 `response_model=PluginOut` /
  `ListEnvelope[PluginOut]`,store 返回的 ORM 行经模型序列化,**不手搓 dict**。
- **唯一冲突 → 409**:`store.Conflict` 捕获后抛 `HTTPException(409)`。
- **纵深防御**:逐路由 `Depends(require_session)` 保留(default-deny 中间件之外的
  第二层);`/api/plugins` 在 `/api/` 前缀下,中间件已先行挡掉无/坏 JWT。
- 列表分页参数 `page` / `pageSize`(camel query),服务端分页。
"""

from __future__ import annotations

import math

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app import store
from app.auth import require_session
from app.db_models import Plugin
from app.models import PluginIn, PluginListOut, PluginOut, PluginUpdate


router = APIRouter(prefix="/api/plugins", tags=["插件台账"])


@router.get("", response_model=PluginListOut, summary="插件列表", description="分页返回插件台账(低基数配置表,前端 ProTable 客户端分页;服务端仍支持 page/pageSize)。")
async def list_plugins(
    _: str = Depends(require_session),
    page: int = Query(default=1, ge=1, title="页码"),
    page_size: int = Query(default=20, ge=1, le=200, alias="pageSize", title="每页条数"),
) -> PluginListOut:
    rows, count = store.list_rows(Plugin, page=page, page_size=page_size)
    return PluginListOut(
        count=count,
        rows=[PluginOut.model_validate(row) for row in rows],
        page=page,
        page_size=page_size,
        total_page=math.ceil(count / page_size) if page_size else 0,
    )


@router.post("", response_model=PluginOut, status_code=status.HTTP_201_CREATED, summary="创建插件", description="新增一条插件台账;code(npm 包名)唯一,重复 → 409。")
async def create_plugin(
    body: PluginIn,
    _: str = Depends(require_session),
) -> PluginOut:
    try:
        record = store.create_row(Plugin, body.model_dump())
    except store.Conflict:
        raise HTTPException(status.HTTP_409_CONFLICT, "plugin code already exists")
    return PluginOut.model_validate(record)


@router.get("/{plugin_id}", response_model=PluginOut, summary="查询单个插件", description="按 id 查询;不存在 → 404。")
async def get_plugin(
    plugin_id: int,
    _: str = Depends(require_session),
) -> PluginOut:
    record = store.get_row(Plugin, plugin_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "plugin not found")
    return PluginOut.model_validate(record)


@router.patch("/{plugin_id}", response_model=PluginOut, summary="更新插件", description="局部更新(只覆盖传入字段);不存在 → 404,code 冲突 → 409。")
async def update_plugin(
    plugin_id: int,
    body: PluginUpdate,
    _: str = Depends(require_session),
) -> PluginOut:
    values = body.model_dump(exclude_unset=True)
    try:
        record = store.update_row(Plugin, plugin_id, values)
    except store.Conflict:
        raise HTTPException(status.HTTP_409_CONFLICT, "plugin code already exists")
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "plugin not found")
    return PluginOut.model_validate(record)


@router.delete("/{plugin_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response, summary="删除插件", description="按 id 删除;不存在 → 404,成功 → 204(无响应体)。")
async def delete_plugin(
    plugin_id: int,
    _: str = Depends(require_session),
) -> Response:
    if not store.delete_row(Plugin, plugin_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "plugin not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
