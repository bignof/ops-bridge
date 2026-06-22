"""
discovery_reporter.py — agent 周期发现上报线程(P3-3)

每 DISCOVERY_INTERVAL 秒:docker(含 stopped)+ nacos(本 ns 全实例)经 discovery 组装 + 落位防错,
再用当前 WS 主动上报 console(type=discovery-report)。线程与 WS 生命周期耦合(连上才报、断线随
ws.keep_running 退出),参照 ws_client._start_heartbeat。nacos 未配 / 查询失败则只报 docker 侧
(nacosService=None),不影响「已停也可管」。

上报消息契约(agent → console,console 侧 P3-4 接收落 DiscoveredNode):
  { "type": "discovery-report", "agentId": "<id>", "nodes": [<enriched record>...],
    "warnings": [<落位冲突>...], "ts": <epoch> }
每个 node = discovery.enrich_with_nacos 的输出:
  { containerId, containerName, composeProject, composeService, dir, image, running,
    nacosService, healthy }
"""
import logging
import threading
import time

import config
from core.handlers import send_message
from services import discovery, docker_cli, nacos_client

logger = logging.getLogger(__name__)


def _nacos_instances():
    """取本 ns 全部 nacos 实例;未配 nacos 或查询失败 → 返回 [](本轮只报 docker 侧,不中断发现)。"""
    if not config.NACOS_SERVER:
        return []
    try:
        return nacos_client.list_all_instances()
    except Exception as exc:
        logger.warning("发现:nacos 实例查询失败,本轮只报 docker 侧: %s", exc)
        return []


def build_report():
    """组装一次发现上报消息(docker 含 stopped + nacos 补 nacosService/healthy + 落位防错)。"""
    raw = docker_cli.list_all_containers()
    records = discovery.collect_local_containers(managed_root=config.MANAGED_PROJECTS_ROOT, containers=raw)
    enriched, warnings = discovery.enrich_with_nacos(records, raw, _nacos_instances())
    return {
        "type": "discovery-report",
        "agentId": config.AGENT_ID,
        "nodes": enriched,
        "warnings": warnings,
        "ts": time.time(),
    }


def report_once(ws):
    """构建并发送一次发现上报;任何失败只记日志,绝不让发现线程因单轮异常退出。"""
    try:
        send_message(ws, build_report())
    except Exception as exc:
        logger.error("发现上报失败: %s", exc)


def start_discovery_reporter(ws):
    """启动周期发现上报线程(DISCOVERY_INTERVAL<=0 则禁用);返回线程或 None。"""
    interval = config.DISCOVERY_INTERVAL
    if interval <= 0:
        logger.info("发现上报已禁用(DISCOVERY_INTERVAL<=0)")
        return None

    def _loop():
        while ws and ws.keep_running:
            time.sleep(interval)
            if ws and ws.keep_running:
                report_once(ws)

    thread = threading.Thread(target=_loop, daemon=True)
    thread.start()
    logger.info("发现上报线程启动(每 %ds)", interval)
    return thread
