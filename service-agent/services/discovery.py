"""
discovery.py — agent 本机发现采集(P3-1)

把 docker（**含 stopped 容器**）+ compose label 组装成发现记录骨架。只产出 docker 可得字段;
nacosService / healthy / host 由上层（P3-3 上报线程结合 nacos）补齐后经 WS 上报 console。

为什么以 docker 为主:nacos 只见在跑的实例,而台账要能管「已停但想 start」的工程 —— 故用
docker（含 stopped）枚举本机受管 compose 工程,nacos 仅补 nacosService/health。

过滤:
- 必须是 compose 工程容器（有 `com.docker.compose.project` label),非 compose 容器跳过;
- 若给了 managed_root,容器的 compose working_dir 须落在其下（只报本机受管工程;
  realpath 级安全由执行闸 services.compose.validate_managed_dir 兜底,这里只做发现过滤）。

本模块刻意**不 import config**（caller 传 managed_root）—— 保持纯函数、便于单测。
"""
from __future__ import annotations

import logging

from services import docker_cli, instance_match

logger = logging.getLogger(__name__)

_LABEL_PROJECT = "com.docker.compose.project"
_LABEL_SERVICE = "com.docker.compose.service"
_LABEL_WORKDIR = "com.docker.compose.project.working_dir"


def _labels(container) -> dict:
    return (container.get("Config") or {}).get("Labels") or {}


def _container_name(container):
    name = container.get("Name") or ""
    return name[1:] if name.startswith("/") else (name or None)


def _under_root(workdir, root) -> bool:
    if not root:
        return True  # 未设受管根 = 不按目录过滤
    if not workdir:
        return False  # 有受管根但容器无 working_dir label → 无法确认归属,排除
    root = root.rstrip("/")
    # posix 前缀判定(agent 跑 Linux;不用 os.path.normpath 以免 Windows 测试环境分隔符歧义)。
    return workdir == root or workdir.startswith(root + "/")


def collect_local_containers(managed_root="", timeout=30, containers=None) -> list:
    """枚举本机 compose 受管容器（含 stopped）的发现记录骨架。

    返回 [{containerId, containerName, composeProject, composeService, dir, image, running}]。
    managed_root 为空 = 不按目录过滤(报全部 compose 容器)。
    containers 给定则复用(避免与 enrich_with_nacos 重复 docker inspect),None 则现取。
    """
    raw = docker_cli.list_all_containers(timeout=timeout) if containers is None else containers
    records = []
    for c in raw:
        labels = _labels(c)
        project = labels.get(_LABEL_PROJECT)
        if not project:
            continue  # 非 compose 管理的容器跳过
        workdir = labels.get(_LABEL_WORKDIR)
        if not _under_root(workdir, managed_root):
            continue
        records.append(
            {
                "containerId": c.get("Id"),
                "containerName": _container_name(c),
                "composeProject": project,
                "composeService": labels.get(_LABEL_SERVICE),
                "dir": workdir,
                "image": (c.get("Config") or {}).get("Image"),
                "running": bool((c.get("State") or {}).get("Running")),
            }
        )
    return records


def enrich_with_nacos(records, raw_containers, nacos_instances):
    """给容器记录补 nacosService/healthy（按 nacos 实例 ip:port 反查容器),并做落位防错（P3-2,评审 M3）。

    入参:
    - records:collect_local_containers 的输出(用 containerId 关联);
    - raw_containers:同一批 docker inspect dicts(按端口/IP 匹配 nacos 实例);
    - nacos_instances:[{serviceName, ip, port, healthy}](本机相关的 nacos 实例,含不健康)。

    返回 (enriched_records, warnings):
    - 每条 record 补 nacosService/healthy;无匹配则均为 None(已停或非 nacos 注册的容器仍报出,可被管理);
    - 冲突不静默取第一个,落 warnings:
      - 一实例多容器(instance-multi-container);
      - 一容器多实例(container-multi-instance)→ 该容器 nacosService/healthy 置 None(歧义不猜)。
    """
    by_container: dict = {}
    warnings = []
    for inst in nacos_instances:
        cands = instance_match.matching_containers(inst, raw_containers)
        svc = inst.get("serviceName")
        if len(cands) > 1:
            warnings.append(
                {
                    "type": "instance-multi-container",
                    "nacosService": svc,
                    "address": f"{inst.get('ip')}:{inst.get('port')}",
                    "containerIds": [c.get("Id") for c in cands],
                }
            )
        for c in cands:
            by_container.setdefault(c.get("Id"), []).append(
                {"nacosService": svc, "healthy": bool(inst.get("healthy"))}
            )

    enriched = []
    for rec in records:
        rec = dict(rec)  # 不改原始 record
        hits = by_container.get(rec.get("containerId"), [])
        if len(hits) == 1:
            rec["nacosService"] = hits[0]["nacosService"]
            rec["healthy"] = hits[0]["healthy"]
        else:
            if len(hits) > 1:  # 一容器被多实例认领 → 冲突,不猜
                warnings.append(
                    {
                        "type": "container-multi-instance",
                        "containerId": rec.get("containerId"),
                        "composeProject": rec.get("composeProject"),
                        "nacosServices": [h["nacosService"] for h in hits],
                    }
                )
            rec["nacosService"] = None
            rec["healthy"] = None
        enriched.append(rec)
    return enriched, warnings
