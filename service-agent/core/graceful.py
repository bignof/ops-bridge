"""
graceful.py — 优雅停机（drain）原语

叶子模块：只 import 标准库 + services.http_client，**绝不** import core.handlers / core.rolling，
以避免环（handlers → graceful → rolling → handlers）。rolling.py 与 handlers.py 单向复用本模块。

drain 模型：worker（NocoBase）优雅停机只认 HTTP POST /api/k8s/shutdown，
该 POST 会阻塞到 worker drain 完成或超时再返回。本模块把「校验 base + 发 shutdown」抽成
纯函数 drain()，供优雅 stop 与优雅 pull-redeploy 复用——不发 ws 消息、不做 compose，
编排（ack / 回包 / compose stop / update）由 handlers 负责。
"""
import ipaddress
from urllib.parse import urlparse

from services import http_client


def _validate_health_base_url(base):
    """
    校验 healthBaseUrl，防 SSRF（H1）。
    要求：scheme ∈ {http, https}，且 host 必须是非公网（private/loopback/link-local）IP。
    非法时抛 ValueError（由调用方转成 failed 结果，不冒泡）。
    """
    if not base:
        raise ValueError("healthBaseUrl 为空")
    parsed = urlparse(base)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"scheme 非法（仅允许 http/https）: {parsed.scheme or '空'}")
    host = parsed.hostname
    if not host:
        raise ValueError("缺少 host")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # 非 IP（如域名）一律拒绝，避免 DNS 解析到公网
        raise ValueError(f"host 必须是内网 IP，非域名: {host}")
    if ip.is_global:
        raise ValueError(f"host 为公网可路由地址，禁止访问: {host}")
    return base


def drain(base, shutdown_timeout=60):
    """
    对 worker 发起优雅停机（drain）：校验 base → POST /api/k8s/shutdown。

    - base 非法 / 公网 / 域名 → 抛 ValueError（在发 shutdown 前就拒，绝不发 POST）。
    - POST /api/k8s/shutdown 阻塞至 worker drain 完成或超时（shutdown_timeout 秒）后返回 (code, text)。
    - code != 200 → 抛 RuntimeError。

    纯函数：不发 ws 消息、不做 compose；编排交给调用方（handlers）。
    """
    _validate_health_base_url(base)
    code, _text = http_client.post(f"{base}/api/k8s/shutdown", timeout=shutdown_timeout)
    if code != 200:
        raise RuntimeError(f"shutdown 返回 {code}")
