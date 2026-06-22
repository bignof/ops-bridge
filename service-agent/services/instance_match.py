def _published_host_ports(container):
    ports = (container.get("NetworkSettings") or {}).get("Ports") or {}
    result = set()
    for bindings in ports.values():
        for binding in bindings or []:
            host_port = binding.get("HostPort")
            if host_port:
                result.add(int(host_port))
    return result


def _bridge_ips(container):
    nets = (container.get("NetworkSettings") or {}).get("Networks") or {}
    return {n.get("IPAddress") for n in nets.values() if n.get("IPAddress")}


def match_instance(instance, containers):
    port = int(instance["port"])
    for c in containers:                       # 主键：宿主发布端口
        if port in _published_host_ports(c):
            return c
    ip = instance.get("ip")                     # 兜底：容器 bridge IP
    for c in containers:
        if ip in _bridge_ips(c):
            return c
    return None


def matching_containers(instance, containers):
    """返回**所有**匹配该实例的容器（冲突检测用,不像 match_instance 只取第一个）。

    与 match_instance 同优先级:先按宿主发布端口;该层无命中再退 bridge IP。返回所选层的
    全部候选 —— 命中 >1 即「一实例多容器」冲突(评审 M3:同机两工程注册容器内同端口 →
    端口主键命中失败落 IP 兜底、IP 也撞 → 张冠李戴;须冲突告警,不静默取第一个）。
    """
    port = int(instance["port"])
    by_port = [c for c in containers if port in _published_host_ports(c)]
    if by_port:
        return by_port
    ip = instance.get("ip")
    if not ip:
        return []
    return [c for c in containers if ip in _bridge_ips(c)]


def compose_project(container):
    """读容器 docker inspect 的 com.docker.compose.project label（compose 工程名）。

    供上层把 nacos 实例落到的容器工程名与 Service.dir 推得的工程名比对：
    优雅操作按 containerId（实例级），force compose 按目录（工程级），两套寻址
    必须指向同一组容器，否则寻址漂移、危险。Config / Labels / 键任一层缺失返回 None。
    """
    return ((container.get("Config") or {}).get("Labels") or {}).get("com.docker.compose.project")
