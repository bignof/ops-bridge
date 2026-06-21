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


def compose_project(container):
    """读容器 docker inspect 的 com.docker.compose.project label（compose 工程名）。

    供上层把 nacos 实例落到的容器工程名与 Service.dir 推得的工程名比对：
    优雅操作按 containerId（实例级），force compose 按目录（工程级），两套寻址
    必须指向同一组容器，否则寻址漂移、危险。Config / Labels / 键任一层缺失返回 None。
    """
    return ((container.get("Config") or {}).get("Labels") or {}).get("com.docker.compose.project")
