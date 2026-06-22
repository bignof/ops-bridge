"""
plugin_distribution.py — agent → console 回源(P1-3）

持本 namespace 的 pull-token 调 console 的 `/api/distribution/*`（复用平台现有端点）：
- fetch_manifest(service)：拉本 ns 的 active 插件清单，原样返回 console 的 [{pluginName, version, url}]。
- attachment_id_from_url(url)：从 console 的 download url 末段解析 attachmentId（清单不单列该字段）。
- download_to(attachment_id, dest_path)：流式回源 .tgz 落到 dest_path，作 plugin_cache.get_or_fetch 的 fetcher。

namespace 恒用 config.PLUGIN_NAMESPACE（本 ns），**忽略 worker 传入的 namespace**（§2.4 越 ns 面已关）。
未配 PLATFORM_URL/PULL_TOKEN/PLUGIN_NAMESPACE 时该 agent 不提供插件分发（抛 DistributionNotConfigured，
由上层降级为 503，不 sys.exit）。
"""
from __future__ import annotations

import logging
from urllib.parse import urlsplit

import config
from services import http_client

logger = logging.getLogger(__name__)


class DistributionNotConfigured(RuntimeError):
    """未配 PLATFORM_URL / PULL_TOKEN / PLUGIN_NAMESPACE —— 该 agent 不提供插件分发。"""


def is_configured() -> bool:
    return bool(config.PLATFORM_URL and config.PULL_TOKEN and config.PLUGIN_NAMESPACE)


def _require_configured() -> None:
    if not is_configured():
        raise DistributionNotConfigured(
            "插件分发未配置（需 PLATFORM_URL / PULL_TOKEN / PLUGIN_NAMESPACE）"
        )


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {config.PULL_TOKEN}"}


def _base() -> str:
    return config.PLATFORM_URL.rstrip("/")


def fetch_manifest(service: str) -> list:
    """回源拉清单：GET {PLATFORM_URL}/api/distribution/plugins?namespace=本ns&service=svc。

    返回 console 原样的 [{pluginName, version, url}]（url 指向 console 的 download）。
    namespace 恒用本 ns（config.PLUGIN_NAMESPACE），不接受调用方传入。
    """
    _require_configured()
    url = f"{_base()}/api/distribution/plugins"
    params = {"namespace": config.PLUGIN_NAMESPACE, "service": service}
    data = http_client.get_json(url, params=params, headers=_auth_headers(), timeout=15)
    if not isinstance(data, list):
        raise RuntimeError(f"清单响应非数组: {type(data).__name__}")
    return data


def attachment_id_from_url(url: str) -> str:
    """从 console download url 取末段 attachmentId（.../api/distribution/download/<id>）。"""
    path = urlsplit(url or "").path.rstrip("/")
    aid = path.rsplit("/", 1)[-1] if path else ""
    if not aid:
        raise ValueError(f"无法从 url 解析 attachmentId: {url!r}")
    return aid


def download_to(attachment_id, dest_path: str) -> None:
    """回源下载 attachmentId 的 .tgz 流式写入 dest_path（作 plugin_cache 的 fetcher）。"""
    _require_configured()
    url = f"{_base()}/api/distribution/download/{attachment_id}"
    http_client.download(url, dest_path, headers=_auth_headers(), timeout=120)
