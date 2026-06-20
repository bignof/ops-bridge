#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""service-platform 部署栈「网段隔离」对抗验证脚本(零三方依赖,仅标准库)。

证明 Task 1 的隔离拓扑成立——这是「节点控制」阻塞验收门「异网段直连 hub 被拒」的可执行验证。

拓扑前提(详见 deploy/README.md):
  - nginx 是唯一对外面(宿主机 80/443);service-hub / service-platform 都不向宿主机发布端口。
  - nginx 路由:`/ws/agent/` → service-hub:8080(WS);其余(含 /health、/、/api/*)→ service-platform:8080。
  - platform 有免鉴权 `GET /health` → 200;hub 的 /health 不经 nginx 暴露,验「hub 经 nginx 可达」只能打 /ws/agent/ 路径。

检查项(逐项打印「检查名 + 期望 + 实际 + PASS/FAIL」;任一**安全项**失败 → exit 1):
  1.【安全·阻塞门】异网段/宿主机直连 hub|platform 内部端口 <host>:8080 → 期望「连接被拒/超时」(未发布端口)。
  2.【可达】       nginx → platform:GET http://<host>:80/health → 期望 200。
  3.【可达·尽力】 nginx → hub(WS 路径):GET http://<host>:80/ws/agent/__probe__ → 期望「能经 nginx 到达 hub」
                  (任意 HTTP 状态码如 400/404/426 都算到达;连接被拒或 502 Bad Gateway 算 FAIL)。

注:真正的「异网段」需从**另一台不同子网主机**跑本脚本;本机/同机跑验的是「端口未发布」这一结构性前提
    (CI 代理)。检查 3 是「尽力可达」项——不计入退出码硬失败,仅打印结论(见 SECURITY_CHECKS)。

用法:
    python validate_isolation.py --host <edge-host>
    # 例(T13 集成验收):docker compose -f deploy/docker-compose.yml up -d 后,宿主机跑
    #     python scripts/validate_isolation.py --host 127.0.0.1
"""

from __future__ import annotations

import argparse
import socket
import sys
import urllib.error
import urllib.request


# ── 检查结果模型 ──────────────────────────────────────────────────────────────
class CheckResult:
    """单项检查结果:名称 / 期望 / 实际 / 是否通过 / 是否安全项(计入退出码)。"""

    def __init__(self, name: str, expected: str, actual: str, passed: bool, *, security: bool) -> None:
        self.name = name
        self.expected = expected
        self.actual = actual
        self.passed = passed
        self.security = security


# ── 检查 1:异网段/宿主机直连内部端口 → 期望连接被拒/超时(端口未发布) ──────────
def check_internal_port_unreachable(host: str, port: int, timeout: float) -> CheckResult:
    """TCP 直连 <host>:<port>。

    判定:
      - 连接被拒(ConnectionRefusedError)/ 超时(timeout)/ 主机不可达 / DNS 不可解析 → PASS(端口未发布,符合隔离)。
      - **成功建立连接** → FAIL(说明有人误发布了内部端口,隔离被打破)。

    跨平台说明:Windows 与 Linux 上,无监听端口的本机直连都抛 ConnectionRefusedError(OSError 子类);
    异网段被防火墙静默丢包则表现为 socket.timeout(同属 OSError)。两者都判 PASS。
    """
    name = f"【安全】直连内部端口 {host}:{port} 应不可达"
    expected = "连接被拒 / 超时 / 不可达(端口未向宿主机发布)"
    sock = None
    try:
        # create_connection 会做 DNS 解析 + connect;成功即拿到已连接 socket。
        sock = socket.create_connection((host, port), timeout=timeout)
    except (ConnectionRefusedError, socket.timeout, TimeoutError) as exc:
        # 最典型的「被拒/超时」——隔离成立。
        return CheckResult(name, expected, f"已拒绝:{type(exc).__name__}", True, security=True)
    except socket.gaierror as exc:
        # 主机名无法解析(异网段拿不到内网 DNS),同样算不可达。
        return CheckResult(name, expected, f"主机不可解析:{exc}", True, security=True)
    except OSError as exc:
        # 其它网络层错误(如 EHOSTUNREACH / ENETUNREACH)统一归为「不可达」。
        return CheckResult(name, expected, f"不可达:{type(exc).__name__}: {exc}", True, security=True)
    else:
        # 居然连上了 → 内部端口被暴露,隔离失败。
        return CheckResult(name, expected, "连接成功(内部端口被暴露!)", False, security=True)
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


# ── HTTP 探测辅助:返回 (status_code, detail);连接级失败 status_code 置 None ─────
def _http_probe(url: str, timeout: float) -> tuple[int | None, str]:
    """GET url。

    返回:
      - (状态码, 描述):正常响应或 HTTP 错误码(4xx/5xx,经 HTTPError 拿到 .code)都算「服务端有应答」。
      - (None, 描述):URLError / 连接被拒 / 超时等「连不上」的连接级失败,status_code 置 None。
    """
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        # 4xx/5xx:服务端有应答(含 502)。.code 即状态码。
        return exc.code, f"HTTP {exc.code} {exc.reason}"
    except urllib.error.URLError as exc:
        # 连接级失败(连接被拒、超时、DNS 等):URLError 包裹底层原因。
        return None, f"连接失败:{exc.reason}"
    except (socket.timeout, TimeoutError) as exc:
        return None, f"超时:{type(exc).__name__}"
    except OSError as exc:
        return None, f"连接失败:{type(exc).__name__}: {exc}"


# ── 检查 2:nginx → platform /health → 期望 200 ───────────────────────────────
def check_nginx_to_platform(host: str, http_port: int, timeout: float) -> CheckResult:
    """经 nginx 边缘打 platform 的免鉴权 /health,期望 200(证明 nginx 起且反代 platform 成立)。"""
    url = f"http://{host}:{http_port}/health"
    name = f"【可达】nginx → platform:GET {url}"
    expected = "HTTP 200(nginx 反代 platform /health)"
    code, detail = _http_probe(url, timeout)
    return CheckResult(name, expected, detail, code == 200, security=True)


# ── 检查 3:nginx → hub(WS 路径)→ 期望「到达 hub」(任意 HTTP 码;502/连不上 = FAIL) ─
def check_nginx_to_hub(host: str, http_port: int, timeout: float) -> CheckResult:
    """经 nginx 打 hub 的 /ws/agent/ 路径探针,验「nginx 能到达 hub」。

    判定:
      - 任意 HTTP 状态码(400/404/426 …)→ PASS(请求经 nginx 到达了 hub,hub 给出了应答)。
        注:对 FastAPI 的 @websocket 端点发普通 GET,hub 通常回 400/404——这正是「到达」的证据。
      - 502 Bad Gateway → FAIL(nginx 起了但到不了 hub 上游)。
      - 连接级失败(连不上 nginx)→ FAIL(nginx 没起 / 不可达)。
    """
    url = f"http://{host}:{http_port}/ws/agent/__probe__"
    name = f"【可达·尽力】nginx → hub(WS 路径):GET {url}"
    expected = "经 nginx 到达 hub(任意 HTTP 状态码;502 / 连不上 = 未到达)"
    code, detail = _http_probe(url, timeout)
    if code is None:
        return CheckResult(name, expected, detail, False, security=False)
    if code == 502:
        return CheckResult(name, expected, f"{detail}(nginx 到不了 hub 上游)", False, security=False)
    return CheckResult(name, expected, f"{detail}(已到达 hub)", True, security=False)


# ── 打印 ──────────────────────────────────────────────────────────────────────
def _print_result(result: CheckResult) -> None:
    flag = "PASS" if result.passed else "FAIL"
    tag = "" if result.security else "(参考项,不计入退出码)"
    print(f"[{flag}] {result.name}{tag}")
    print(f"        期望: {result.expected}")
    print(f"        实际: {result.actual}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="service-platform 部署栈网段隔离对抗验证(仅标准库)。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "退出码:任一【安全】项 FAIL → 1;否则 0。\n"
            "完整三项全绿需 `docker compose -f deploy/docker-compose.yml up -d` 起栈后,"
            "在宿主机(理想为异网段主机)执行本脚本。"
        ),
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="目标边缘主机(nginx 所在宿主机的 IP / 域名)。默认 127.0.0.1(本机自检)。",
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=80,
        help="nginx 对外 HTTP 端口。默认 80。",
    )
    parser.add_argument(
        "--internal-port",
        type=int,
        default=8080,
        help="hub / platform 容器内部端口(隔离正确时此端口在宿主机不可达)。默认 8080。",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="每项检查的连接 / 请求超时(秒)。默认 5。",
    )
    args = parser.parse_args(argv)

    print("=" * 78)
    print("service-platform 部署栈 · 网段隔离对抗验证")
    print(f"目标主机: {args.host}  | nginx HTTP 端口: {args.http_port}  | 内部端口: {args.internal_port}")
    print("=" * 78)
    if args.host in ("127.0.0.1", "localhost", "::1"):
        print(
            "提示: 本机/同机执行——验的是「内部端口未向宿主机发布」这一结构性前提(CI 代理)。\n"
            "      真正的「异网段直连被拒」需从另一台不同子网主机以 --host 指向边缘 IP 复跑本脚本。"
        )
    print("-" * 78)

    results = [
        check_internal_port_unreachable(args.host, args.internal_port, args.timeout),
        check_nginx_to_platform(args.host, args.http_port, args.timeout),
        check_nginx_to_hub(args.host, args.http_port, args.timeout),
    ]
    for result in results:
        _print_result(result)
        print("-" * 78)

    security_failed = [r for r in results if r.security and not r.passed]
    reference_failed = [r for r in results if not r.security and not r.passed]

    print("汇总:")
    print(f"  安全项: {sum(1 for r in results if r.security and r.passed)}/{sum(1 for r in results if r.security)} 通过")
    if reference_failed:
        print(f"  参考项: {len(reference_failed)} 项未通过(不影响退出码,但表明 nginx→hub 链路异常,需排查)")
    if security_failed:
        print("结果: FAIL —— 隔离/可达安全门未通过:")
        for r in security_failed:
            print(f"        - {r.name} | 实际: {r.actual}")
        return 1
    print("结果: PASS —— 全部安全项通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
