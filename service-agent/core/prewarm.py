"""
prewarm.py — agent 侧「预热」插件缓存(P4-6)

console 在 restart 投放前发独立 message type `prewarm`,agent 提前把目标 service 的插件
**回源下载入本机缓存**,使随后 worker 重启拉包零等待(缓存命中直给,见 plugin_server /download)。

best-effort 语义(关键取舍):
- 预热失败**绝不影响 restart 本身**——它只是一次「提前热身」,失败了顶多让随后拉包退回实时回源
  (worker 拉包路径 plugin_server 仍会自己回源),不会让重启卡住或报错。
- 因此单个 service / 单个插件失败只记日志 + 继续,累计**成功预热的插件数** warmed;有任一插件
  成功即回 success(warmed=成功数);全失败 / 未配置回源 → failed。console 侧据此应「短超时 + 忽略失败」。

实现上**完全复用**既有回源 + 缓存链,不另写下载逻辑:
  plugin_distribution.fetch_manifest(service)  → 本 ns 该 service 的 [{pluginName,version,url}]
  plugin_distribution.attachment_id_from_url   → 从 url 取 attachmentId
  plugin_cache.get_or_fetch(aid, fetcher)      → 命中直给 / 未命中由 fetcher 回源落盘(与 /download 同一入口)
namespace 恒用 agent 本 ns(plugin_distribution 内部固定),不接受 console 传入。

分发方式与 graceful-restart 对称:独立 message type、ws_client 在独立线程调本函数,**不进 HANDLERS**
(HANDLERS 是 docker compose 命令动作集 / capabilities 来源,prewarm 不是命令动作)。
"""
from __future__ import annotations

import logging
import re

from core.handlers import send_message
from services import plugin_cache, plugin_distribution

logger = logging.getLogger(__name__)

# 与 rolling._redact 同语义:回包 / 日志里把 nacos accessToken 脱敏(纵深防御)。
# prewarm 自身不碰 nacos,但 fetch / 下载异常文本可能裹入上游 url 等,统一过一道脱敏更稳。
_TOKEN_RE = re.compile(r"accessToken=[^&\s]+")


def _redact(text) -> str:
    return _TOKEN_RE.sub("accessToken=***", str(text))


def _prewarm_service(service: str) -> int:
    """预热单个 service 的全部插件,返回**成功预热的插件数**。

    单个插件失败(url 解析不出 attachmentId / 回源下载失败)只记日志 + 继续,不抛。
    """
    manifest = plugin_distribution.fetch_manifest(service)
    warmed = 0
    for item in manifest:
        url = item.get("url", "")
        try:
            aid = plugin_distribution.attachment_id_from_url(url)
            # 复用 /download 同一入口:命中直给、未命中回源落盘(同主机多容器只回源一次)。
            plugin_cache.get_or_fetch(
                aid,
                lambda dest, _aid=aid: plugin_distribution.download_to(_aid, dest),
            )
            warmed += 1
        except Exception as exc:  # 单插件失败不中断其余(best-effort)
            logger.warning(
                "prewarm 单插件失败,跳过 service=%s plugin=%s: %s",
                service, item.get("pluginName"), _redact(exc),
            )
    return warmed


def handle_prewarm(ws, data):
    """处理 prewarm 消息:回源预热各 service 的插件入本机缓存,回 prewarm-result。

    契约:
      console → agent: {type:'prewarm', requestId, services:[<serviceCode>...]}
      agent → console: {type:'prewarm-result', requestId, status:'success'|'failed', warmed:<int>, error?:<脱敏>}
    - 未配置回源(is_configured()=False)→ status='failed' + error 说明未配置(warmed=0)。
    - 有任一插件成功预热 → status='success'(warmed=成功数);全失败 / 无可预热 → status='failed'。
    """
    request_id = data.get("requestId")

    # 未配回源:本 agent 不提供插件分发,预热无从谈起,直接回 failed(不抛、不影响别的)。
    if not plugin_distribution.is_configured():
        send_message(ws, {
            "type": "prewarm-result", "requestId": request_id,
            "status": "failed", "warmed": 0,
            "error": "插件分发未配置(需 PLATFORM_URL / PULL_TOKEN / PLUGIN_NAMESPACE),无法预热",
        })
        return

    services = data.get("services") or []
    warmed = 0
    last_error = None
    for service in services:
        try:
            warmed += _prewarm_service(service)
        except Exception as exc:  # 单 service 失败(如清单回源失败)不中断其余 service(best-effort)
            last_error = _redact(exc)
            logger.warning("prewarm 单 service 失败,跳过 service=%s: %s", service, last_error)

    if warmed > 0:
        # 有任一插件成功即视为成功(best-effort:部分预热也比不预热强);不回 error。
        send_message(ws, {
            "type": "prewarm-result", "requestId": request_id,
            "status": "success", "warmed": warmed,
        })
    else:
        # 全失败 / 无可预热:回 failed,带最后一条脱敏 error 便于定位(可能为 None,如 services 为空)。
        msg = {
            "type": "prewarm-result", "requestId": request_id,
            "status": "failed", "warmed": 0,
        }
        if last_error is not None:
            msg["error"] = last_error
        send_message(ws, msg)
