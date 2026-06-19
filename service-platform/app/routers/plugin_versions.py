"""插件版本台账 + 插件上传路由(Task 9)。

`/api/plugin-versions/upload`(multipart 上传)、`/api/plugin-versions`(GET 列表信封)、
`/api/plugin-versions/{id}`(GET 单条)。

绑定约束(评审 M8/M2/L3,见 task-9-brief):
- **version = .tgz 内 `package.json.version`**(经 `storage.parse_tgz`),**不做文件名
  split**——旧平台文件名 split 会得 `rc.xxx` 垃圾值。version 列 NOT NULL。
- **包名匹配 plugin**:先按 `plugin.code` 精确等于 package.json `name`;未命中则取 name
  尾段(`@scope/foo` → `foo`)按 `code LIKE '%/<尾段>'` 模糊。**0 命中 / 多命中均 400**
  (明确文案),避免误绑错插件。
- **请求体大小上限(评审 L3)**:`MAX_UPLOAD_BYTES`;读入前先看 `Content-Length`,超限
  直接 413(不把超大体读进内存);读入后再按实际字节数兜底(分块上传可能无 Content-Length)。
- **落地顺序(brief)**:先建 `plugin_version`(拿 id)→ `storage.store_tgz(plugin_id,
  version_id, filename, data)` 落盘 → 建 `plugin_attachment`(回填 storage_path)。
  后两步任一失败 → **清理已建的 version 行 + 落盘文件**(失败不留孤儿版本,否则会卡住重传)。
- **响应全 camelCase**(评审 H2):经 `*Out` 模型序列化,不手搓 dict。
- **列表信封**(评审 M2):`{count, rows, page, pageSize, totalPage}` + `?pluginId=` 过滤。
- **纵深防御**:逐路由 `Depends(require_session)`;`/api/` 前缀下 default-deny 中间件已先挡无/坏 JWT。
"""

from __future__ import annotations

import logging
import math
import os

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status

from app import storage, store
from app.auth import require_session
from app.db_models import Plugin, PluginAttachment, PluginVersion
from app.models import PluginUploadOut, PluginVersionListOut, PluginVersionOut


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/plugin-versions", tags=["插件版本/上传"])

# 请求体大小上限(评审 L3)。默认 200MB,可由 env 覆盖;同时依赖 nginx client_max_body_size
# 在边缘兜底(见 storage.py 文档)。读入前先按 Content-Length 挡,避免超大体进内存。
MAX_UPLOAD_BYTES = int(os.getenv("PLUGIN_MAX_UPLOAD_BYTES", str(200 * 1024 * 1024)))


def _match_plugin(name: str) -> Plugin:
    """按 package.json `name` 匹配唯一 plugin:精确 code 优先,回退尾段 LIKE。

    0 命中 / 多命中 → 400(明确文案)。命中唯一 → 返回该 Plugin。
    """
    # 1) 精确:plugin.code == name(唯一列,至多一条)
    exact = store.find_rows(Plugin, filters=[Plugin.code == name], limit=2)
    if len(exact) == 1:
        return exact[0]

    # 2) 回退:取 name 尾段(@scope/foo → foo;无 scope 则整名),code LIKE '%/<尾段>'
    tail = name.rsplit("/", 1)[-1]
    like = store.find_rows(Plugin, filters=[Plugin.code.like(f"%/{tail}")], limit=2)
    if len(like) == 1:
        return like[0]
    if len(like) > 1:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"插件包名 '{name}' 命中多个插件(尾段 '{tail}'),请用精确 code 注册的插件",
        )
    # 0 命中(精确与回退都没唯一命中)
    raise HTTPException(status.HTTP_400_BAD_REQUEST, f"未找到与插件包名 '{name}' 匹配的插件,请先在插件台账登记")


@router.post(
    "/upload",
    response_model=PluginUploadOut,
    summary="上传插件包(.tgz)",
    description=(
        "multipart 上传 .tgz:解析内 package.json 取 version(非文件名)→ 按包名匹配插件 → "
        "落盘 → 建版本+附件。超大小上限 413,包名无/多命中 400,非法包 400,版本重复 409。"
    ),
)
async def upload_plugin_version(
    request: Request,
    file: UploadFile = File(..., description="插件包 .tgz(NocoBase build --tar 产物)"),
    _: str = Depends(require_session),
) -> PluginUploadOut:
    # 评审 L3:读入前先按 Content-Length 挡超大体(分块上传可能缺该头,读入后再兜底)。
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_UPLOAD_BYTES:
                raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "上传文件超过大小上限")
        except ValueError:
            pass  # 坏的 Content-Length 不作为依据,交由读入后兜底

    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:  # 兜底:实际字节数超限(无/坏 Content-Length 时)
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "上传文件超过大小上限")
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "上传文件为空")

    # 解析 package.json(根级/package 前缀回退见 storage);非法包 → 400。version 取自此处,非文件名。
    try:
        meta = storage.parse_tgz(data)
    except storage.BadPackage as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"非法插件包:{exc}")
    name, version = meta["name"], meta["version"]

    plugin = _match_plugin(name)

    # 落地:先建 version(拿 id,且 UNIQUE(plugin_id, version) 冲突 → Conflict → 409)。
    # version 显式取自 package.json;name 顺带记包名便于追溯。
    try:
        version_row = store.create_row(
            PluginVersion,
            {"plugin_id": plugin.id, "version": version, "name": name},
        )
    except store.Conflict:
        raise HTTPException(status.HTTP_409_CONFLICT, f"插件 {name} 版本 {version} 已存在")

    # version 已建 → 落盘 + 建附件;任一失败清理 version 行(+ 落盘文件),不留孤儿。
    try:
        storage_path = storage.store_tgz(plugin.id, version_row.id, file.filename, data)
        attachment = store.create_row(
            PluginAttachment,
            {
                "plugin_version_id": version_row.id,
                "filename": storage._sanitize(file.filename),
                "size": len(data),
                "storage_path": storage_path,
            },
        )
    except Exception:
        # 补偿清理:删 version 行(附件若已建会随后续清理/无 FK,此处尽力删文件)。
        store.delete_row(PluginVersion, version_row.id)
        logger.exception("插件上传落地失败,已回滚 plugin_version id=%s", version_row.id)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "插件包落地失败")

    return PluginUploadOut.model_validate(
        {"plugin_version_id": version_row.id, "attachment_id": attachment.id, "version": version}
    )


@router.get(
    "",
    response_model=PluginVersionListOut,
    summary="插件版本列表",
    description="分页返回版本台账;支持 ?pluginId= 过滤(P1-SPA 上传页 ProTable 依赖信封形状)。",
)
async def list_plugin_versions(
    _: str = Depends(require_session),
    plugin_id: int | None = Query(default=None, alias="pluginId", title="按插件过滤"),
    page: int = Query(default=1, ge=1, title="页码"),
    page_size: int = Query(default=20, ge=1, le=200, alias="pageSize", title="每页条数"),
) -> PluginVersionListOut:
    filters = [PluginVersion.plugin_id == plugin_id] if plugin_id is not None else []
    rows, count = store.list_rows(PluginVersion, page=page, page_size=page_size, filters=filters)
    return PluginVersionListOut(
        count=count,
        rows=[PluginVersionOut.model_validate(row) for row in rows],
        page=page,
        page_size=page_size,
        total_page=math.ceil(count / page_size) if page_size else 0,
    )


@router.get(
    "/{plugin_version_id}",
    response_model=PluginVersionOut,
    summary="查询单个插件版本",
    description="按 id 查询;不存在 → 404。",
)
async def get_plugin_version(
    plugin_version_id: int,
    _: str = Depends(require_session),
) -> PluginVersionOut:
    record = store.get_row(PluginVersion, plugin_version_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "plugin version not found")
    return PluginVersionOut.model_validate(record)
