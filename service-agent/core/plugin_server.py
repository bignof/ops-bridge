"""
plugin_server.py — worker-facing 插件分发 HTTP（P1-2）

本机 worker（同主机容器）无 token 拉插件：
  GET /plugins?service=<svc>   → [{pluginName, version, url}]，url 改写指向本 agent /download/<attachmentId>
  GET /download/<attachmentId> → 流式 .tgz（命中本机缓存直给；未命中 agent 回源后给）

- 独立 server 绑 PLUGIN_SERVE_HOST（默认 127.0.0.1，不复用 HEALTH_HOST 的 0.0.0.0；worker 同主机访问、不出主机）。
- worker 无 namespace 概念：即便传了 namespace 也**忽略**，回源恒用 agent 本 ns（见 plugin_distribution）。
- 返回三字段 pluginName/version/url 与平台 /api/distribution/plugins 字面一致 → sync-plugins.js 解析零改动，
  只是 base URL 指向本机 agent、且无需 Authorization。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import config
from services import plugin_cache, plugin_distribution

logger = logging.getLogger(__name__)

_DOWNLOAD_PREFIX = "/download/"
_STREAM_CHUNK = 1024 * 256


class _PluginHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == "/plugins":
            self._handle_plugins(parse_qs(parsed.query))
        elif parsed.path == "/health":
            self._handle_health()
        elif parsed.path.startswith(_DOWNLOAD_PREFIX):
            self._handle_download(parsed.path[len(_DOWNLOAD_PREFIX):])
        else:
            self._send_error(404, "not found")

    def _handle_health(self) -> None:
        """GET /health（P5-4）：agent 自身的本机状态快照，给运维/排障用。

        纯只读、无副作用，不鉴权（与 /plugins 同：127.0.0.1 worker-facing，本就内网）。
        **本地便利，非权威**——agent 状态本来经 WS register(capabilities) + discovery-report 流向
        console，这个 /health 不替代那条链路，只是给本机一个不依赖 console 的即时快照。
        """
        payload = {
            "configured": plugin_distribution.is_configured(),
            "pluginNamespace": config.PLUGIN_NAMESPACE,
            "cache": plugin_cache.stats(),
            # discovery 低成本带「是否启用 + 周期」（只读 config）；上次上报时间需给 discovery_reporter
            # 加模块级状态，属复杂依赖，按 brief 省略，不为它引入耦合。
            "discovery": {
                "enabled": config.DISCOVERY_INTERVAL > 0,
                "intervalSec": config.DISCOVERY_INTERVAL,
            },
        }
        self._send_json(200, payload)

    def _handle_plugins(self, query: dict) -> None:
        # 只取 service；worker 传入的 namespace 一律忽略（agent 恒用自己配置的本 ns）。
        service_values = query.get("service") or []
        service = service_values[0] if service_values else ""
        if not service:
            self._send_error(400, "missing service")
            return
        try:
            manifest = plugin_distribution.fetch_manifest(service)
        except plugin_distribution.DistributionNotConfigured:
            self._send_error(503, "plugin distribution not configured")
            return
        except Exception as exc:  # 回源失败：不暴露内部，记日志回 502
            logger.warning("回源清单失败 service=%s: %s", service, exc)
            self._send_error(502, "upstream manifest failed")
            return

        base = self._self_base()
        rewritten = []
        for item in manifest:
            try:
                aid = plugin_distribution.attachment_id_from_url(item.get("url", ""))
            except ValueError:
                logger.warning("清单条目 url 无法解析 attachmentId，跳过: %r", item)
                continue
            # url 改写指向「自己」；pluginName/version 原样透传（契约字面）。
            rewritten.append(
                {
                    "pluginName": item.get("pluginName"),
                    "version": item.get("version"),
                    "url": f"{base}/download/{aid}",
                }
            )
        self._send_json(200, rewritten)

    def _handle_download(self, attachment_id: str) -> None:
        if not attachment_id:
            self._send_error(404, "not found")
            return
        try:
            path = plugin_cache.get_or_fetch(
                attachment_id,
                lambda dest: plugin_distribution.download_to(attachment_id, dest),
            )
        except plugin_distribution.DistributionNotConfigured:
            self._send_error(503, "plugin distribution not configured")
            return
        except ValueError:
            # 非法 attachmentId（plugin_cache._safe_id 拒，防路径穿越）→ 404。
            self._send_error(404, "not found")
            return
        except Exception as exc:  # 回源下载失败 → 502
            logger.warning("回源下载失败 attachmentId=%s: %s", attachment_id, exc)
            self._send_error(502, "upstream download failed")
            return
        self._send_file(path)

    def _self_base(self) -> str:
        # 改写 url 用 worker 实际连入的 Host（部署无关；缺 Host 头回退配置地址）。
        host = self.headers.get("Host") if self.headers else None
        if host:
            return f"http://{host}"
        return f"http://{config.PLUGIN_SERVE_HOST}:{config.PLUGIN_SERVE_PORT}"

    def _send_json(self, code: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, code: int, message: str) -> None:
        self._send_json(code, {"error": message})

    def _send_file(self, path: str) -> None:
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", "application/gzip")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(_STREAM_CHUNK)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def log_message(self, format, *args):
        return


def start_plugin_server():
    server = ThreadingHTTPServer((config.PLUGIN_SERVE_HOST, config.PLUGIN_SERVE_PORT), _PluginHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(
        "Plugin server listening on http://%s:%d/plugins",
        config.PLUGIN_SERVE_HOST,
        config.PLUGIN_SERVE_PORT,
    )
    return server


def maybe_start_plugin_server():
    """仅在配齐插件分发（PLATFORM_URL/PULL_TOKEN/PLUGIN_NAMESPACE）时启动；否则跳过（可选能力）。"""
    if not plugin_distribution.is_configured():
        logger.info("插件分发未配置（PLATFORM_URL/PULL_TOKEN/PLUGIN_NAMESPACE），跳过 worker-facing server")
        return None
    return start_plugin_server()
