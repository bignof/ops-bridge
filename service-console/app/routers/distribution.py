"""分发端点(Task 11):queryPlugin 兼容 + id 归属式下载防 IDOR + fetch_record。

`GET /api/distribution/plugins?namespace=&service=`、
`GET /api/distribution/download/{attachment_id}`。

⚠️ **中间件白名单已放行 `/api/distribution/**`**(见 `app/middleware.py`),故这两个
端点**没有 session JWT 保护**,鉴权 100% 靠端点内 **pull token**(`Authorization:
Bearer <plain>`)。安全不变式(逐条照做,本任务核心):

1. **pull token 解析(`_resolve`)**:从 `Authorization: Bearer` 取明文;**先对
   None/空/非 Bearer 短路**,再经 `store.resolve_namespace_by_pull_token`(内部遍历
   namespace 用 `tokens.verify_token` 常量时间逐一比对)反解 namespace;**无任何匹配 →
   None**。绝不先 `==` 裸比哈希。
2. **plugins 归属**:token 必须**属于** query 的 `namespace`(反解出的 ns.code ==
   query namespace),否则 **403**(测试 #2);token 不可解析(无 / 坏)→ 403(测试 #7/#8)。
3. **download 归属(防 IDOR)**:只靠 token→ns + `store.attachment_in_namespace`
   的 active spv 链校验,**不靠任何 query 参数**;不符一律 **404**(不是 403——防探测
   存在性,测试 #4)。无 Authorization → 401(缺凭据);坏 token → 404(等价无权访问任何物)。
4. **fetch_record 写入**:每命中一个插件写一行(对齐旧 `queryPlugin` 的 bulkCreate),
   字段走 snake(`store.create_fetch_record` 内 by_alias=False),勿被 camel 键灌坏。

返回契约(跨计划,`sync-plugins.js` 直接解析,**字段字面 `pluginName`/`version`/`url`,
不走 to_camel 改名**):`GET plugins` → 数组 `[{pluginName, version, url}]`,
`url = settings.plugin_download_base_url + "/api/distribution/download/" + attachmentId`。
"""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query, status
from starlette.responses import StreamingResponse

from app import storage, store
from app.config import settings
from app.db_models import Namespace


router = APIRouter(prefix="/api/distribution", tags=["分发(节点拉包)"])


def _extract_bearer(authorization: str | None) -> str | None:
    """从 `Authorization: Bearer <plain>` 取明文;缺/非 Bearer → None(不抛)。"""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return authorization[len("Bearer "):]


def _resolve(authorization: str | None) -> tuple[Namespace | None, bool]:
    """反解 pull token → (namespace 或 None, 是否带了合法 Bearer 头)。

    安全不变式 #1:对 None/空/非 Bearer 先短路(had_bearer=False);带了 Bearer 才进
    `store.resolve_namespace_by_pull_token`(常量时间逐一比对),无匹配返回 (None, True)。
    """
    plain = _extract_bearer(authorization)
    if plain is None:
        return None, False
    return store.resolve_namespace_by_pull_token(plain), True


@router.get(
    "/plugins",
    summary="查询发布版本(节点拉包清单)",
    description=(
        "Bearer=pull token;**小写查询参数 namespace/service**(对应 namespaceCode/"
        "serviceCode,跨计划契约勿改名)。校验 token 属该 namespace(否则 403),查 active "
        "版本链,返回数组 [{pluginName, version, url}](三字段字面不走 to_camel,sync-plugins "
        "直接解析),并为每命中插件写一行 fetch_record。token 无 / 坏 → 403。"
    ),
)
async def query_plugins(
    namespace: str = Query(..., title="命名空间编码(= namespaceCode)"),
    service: str = Query(..., title="服务编码(= serviceCode)"),
    authorization: str | None = Header(default=None),
) -> list[dict]:
    ns, _had_bearer = _resolve(authorization)
    # token 不可解析(无 / 坏)→ 403;能解析但不属 query 的 namespace → 403(不变式 #2)。
    if ns is None or ns.code != namespace:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "pull token 无权访问该命名空间")

    plugins = store.query_active_plugins(namespace, service)
    result: list[dict] = []
    for p in plugins:
        # 每命中一个插件写一行拉取记录(对齐旧 queryPlugin 的 bulkCreate;snake 字段)。
        store.create_fetch_record(
            namespace_id=p["namespace_id"],
            service_id=p["service_id"],
            plugin_id=p["plugin_id"],
            plugin_version_id=p["plugin_version_id"],
            remark=f"{p['plugin_code']}@{p['version']}",
        )
        # 字段字面 pluginName/version/url(契约,不走 to_camel)。
        result.append(
            {
                "pluginName": p["plugin_code"],
                "version": p["version"],
                "url": f"{settings.plugin_download_base_url}/api/distribution/download/{p['attachment_id']}",
            }
        )
    return result


@router.get(
    "/download/{attachment_id}",
    summary="下载插件包(id 归属式,防 IDOR)",
    description=(
        "Bearer=pull token;反解 token → namespace,校验该 attachment 经 active spv 链"
        "归属于该 namespace(只靠 token→ns + 链,不靠 query 参数),不符一律 404(防探测"
        "存在性)。无 Authorization → 401;坏 token → 404。放行后流式返回 .tgz。"
    ),
)
async def download(
    attachment_id: int,
    authorization: str | None = Header(default=None),
) -> StreamingResponse:
    ns, had_bearer = _resolve(authorization)
    if not had_bearer:
        # 缺凭据 → 401(纵深回归 #7:中间件已放行 distribution,这里自己拒)。
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing pull token")
    if ns is None:
        # 坏 token(#8):等价于无权访问任何物 → 404(归属式,不暴露存在性)。
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")

    # 归属链校验(#4 IDOR 核心):只靠 token 反解的 ns,绝不接受 query 旁路。
    att = store.attachment_in_namespace(attachment_id, ns.id)
    if att is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")

    try:
        stream = storage.open_stream(att.storage_path)
    except (FileNotFoundError, storage.BadPackage):
        # 库内有记录但盘上文件缺失 / 路径越界:对客户端一律 404(不暴露内部状态)。
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    return StreamingResponse(stream, media_type="application/gzip")
