"""namespace 台账 CRUD 路由(Task 6b)。

`/api/namespaces`(GET 列表信封 / POST 201)、`/api/namespaces/{id}`(GET / PATCH /
DELETE 204)。约束:
- **响应全 camelCase**(评审 H2):一律 `response_model=*Out`,store 返回经模型序列化,
  **不手搓 dict**。
- **唯一冲突 → 409**:`store.Conflict` 捕获后抛 `HTTPException(409)`。
- **纵深防御**:逐路由 `Depends(require_session)` 保留;`/api/` 前缀下中间件已先挡无/坏 JWT。
- **列表回名(评审 H3)**:返回 `id/code/name`,`name` 空回退 `code`(在线/心跳 P2 实时读 hub 填,本任务留空)。
- **create 特例(评审 H7 / show-once)**:先 `hub_client.provision_agent(code)` 取 agentKey,
  **仅一次性放进 create 响应 `agentKey` 字段、不入库**;库里无该列,后续重查不含明文。
  > 这里**经模块引用** `hub_client.provision_agent(...)` 调用(而非 `from ... import provision_agent`),
  > 故测试 `monkeypatch.setattr(hub_client, "provision_agent", ...)` 能生效(评审 H7 同款打桩)。
"""

from __future__ import annotations

import math

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app import hub_client, store, tokens
from app.auth import require_session
from app.db_models import Namespace
from app.models import (
    NamespaceCreateOut,
    NamespaceIn,
    NamespaceListOut,
    NamespaceOut,
    NamespaceRotateKeyOut,
    NamespaceRotatePullTokenOut,
    NamespaceUpdate,
)


router = APIRouter(prefix="/api/namespaces", tags=["命名空间台账"])


def _raise_hub_unavailable(exc: Exception) -> None:
    """把 hub 调用异常统一映射成稳定的平台错误码(评审 A13,spec L103)。

    - `hub_client.HubError`(配置缺失 / hub 业务性失败,如未返回 agentKey)→ **502 Bad Gateway**。
    - `httpx.HTTPError`(连接 / 超时 / 非 2xx 等传输层错误)→ **503 Service Unavailable**。

    detail 用**固定中文文案**,绝不回显 hub URL 或底层异常细节(`str(exc)` 可能含
    内部地址 / token 痕迹);原始异常仅经 `raise ... from exc` 留在服务端堆栈,不出网。
    """
    if isinstance(exc, hub_client.HubError):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "服务编排中心(service-hub)暂不可用,请稍后重试") from exc
    raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "服务编排中心(service-hub)连接失败,请稍后重试") from exc


def _to_out(row: Namespace) -> NamespaceOut:
    """转响应模型;`name` 空回退 `code`(评审 H3)。"""
    return NamespaceOut(id=row.id, code=row.code, name=row.name or row.code)


@router.get("", response_model=NamespaceListOut, summary="命名空间列表", description="分页返回命名空间台账;name 空回退 code(在线/心跳 P2 实时读 hub 填)。")
async def list_namespaces(
    _: str = Depends(require_session),
    page: int = Query(default=1, ge=1, title="页码"),
    page_size: int = Query(default=20, ge=1, le=200, alias="pageSize", title="每页条数"),
) -> NamespaceListOut:
    rows, count = store.list_rows(Namespace, page=page, page_size=page_size)
    return NamespaceListOut(
        count=count,
        rows=[_to_out(row) for row in rows],
        page=page,
        page_size=page_size,
        total_page=math.ceil(count / page_size) if page_size else 0,
    )


@router.post("", response_model=NamespaceCreateOut, status_code=status.HTTP_201_CREATED, summary="创建命名空间", description="先向 service-hub provision Agent 取 agentKey(仅本次响应返回、不入库 show-once);code(=agentId)唯一,重复 → 409。")
async def create_namespace(
    body: NamespaceIn,
    _: str = Depends(require_session),
) -> NamespaceCreateOut:
    # 先入库占住唯一 code(冲突即 409,避免无谓地向 hub provision);成功后再取一次性 agentKey。
    # by_alias=False:取 snake 字段名（ORM 列名）；MODEL_CONFIG 默认 serialize_by_alias=True 会出 camel，需显式覆盖。
    try:
        record = store.create_row(Namespace, body.model_dump(by_alias=False))
    except store.Conflict:
        raise HTTPException(status.HTTP_409_CONFLICT, "namespace code already exists")
    # 经模块引用调用,使测试 monkeypatch.setattr(hub_client, "provision_agent", ...) 生效(评审 H7)。
    # 评审 A14:provision 失败必须**补偿删除**刚建的台账行(整体原子失败),否则遗留无 agentKey
    # 的孤儿 namespace(show-once 永久丢)。评审 A13:hub 异常统一映射 502/503,不回显内部细节。
    try:
        agent_key = hub_client.provision_agent(record.code)
    except (hub_client.HubError, httpx.HTTPError) as exc:
        store.delete_row(Namespace, record.id)  # 补偿:回收刚建的孤儿行
        _raise_hub_unavailable(exc)
    return NamespaceCreateOut(
        id=record.id,
        code=record.code,
        name=record.name or record.code,
        agent_key=agent_key,
    )


@router.get("/{namespace_id}", response_model=NamespaceOut, summary="查询单个命名空间", description="按 id 查询;不存在 → 404。")
async def get_namespace(
    namespace_id: int,
    _: str = Depends(require_session),
) -> NamespaceOut:
    record = store.get_row(Namespace, namespace_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "namespace not found")
    return _to_out(record)


@router.patch("/{namespace_id}", response_model=NamespaceOut, summary="更新命名空间", description="局部更新(只覆盖传入字段);不存在 → 404,code 冲突 → 409。")
async def update_namespace(
    namespace_id: int,
    body: NamespaceUpdate,
    _: str = Depends(require_session),
) -> NamespaceOut:
    values = body.model_dump(exclude_unset=True, by_alias=False)
    try:
        record = store.update_row(Namespace, namespace_id, values)
    except store.Conflict:
        raise HTTPException(status.HTTP_409_CONFLICT, "namespace code already exists")
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "namespace not found")
    return _to_out(record)


@router.delete("/{namespace_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response, summary="删除命名空间", description="按 id 删除;不存在 → 404,成功 → 204(无响应体)。")
async def delete_namespace(
    namespace_id: int,
    _: str = Depends(require_session),
) -> Response:
    if not store.delete_row(Namespace, namespace_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "namespace not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{namespace_id}/rotate-key", response_model=NamespaceRotateKeyOut, summary="轮换 agentKey", description="向 service-hub 轮换该命名空间(Agent)的连接密钥,返回新 agentKey(仅本次响应、不入库 show-once;旧密钥 hub 侧立即失效)。namespace 不存在 → 404。")
async def rotate_namespace_key(
    namespace_id: int,
    _: str = Depends(require_session),
) -> NamespaceRotateKeyOut:
    # 先确认 namespace 存在(不存在则 404,且不调 hub);存在再用其 code(=agentId)轮换。
    record = store.get_row(Namespace, namespace_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "namespace not found")
    # 经模块引用调用,使测试 monkeypatch.setattr(hub_client, "rotate_agent_key", ...) 生效(同 create 的 H7 打桩)。
    # 新 agentKey 仅放进本次响应、不入库(库无该列),守 show-once 不变式。
    # 评审 A13:hub 异常统一映射 502/503,不回显 hub URL / 内部细节(rotate 无新建行,无需补偿)。
    try:
        agent_key = hub_client.rotate_agent_key(record.code)
    except (hub_client.HubError, httpx.HTTPError) as exc:
        _raise_hub_unavailable(exc)
    return NamespaceRotateKeyOut(agent_key=agent_key)


@router.post("/{namespace_id}/rotate-pull-token", response_model=NamespaceRotatePullTokenOut, summary="轮换 pull token", description="本地生成新 pull token,只把 sha256 哈希写入 namespace.pull_token_hash;明文仅本次响应返回(show-once,平台不落地明文)。namespace 不存在 → 404。")
async def rotate_namespace_pull_token(
    namespace_id: int,
    _: str = Depends(require_session),
) -> NamespaceRotatePullTokenOut:
    plain, hashed = tokens.new_pull_token()
    # 只把哈希落库;明文仅本次响应返回(show-once)。update_row 行不存在返回 None → 404。
    record = store.update_row(Namespace, namespace_id, {"pull_token_hash": hashed})
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "namespace not found")
    return NamespaceRotatePullTokenOut(pull_token=plain)
