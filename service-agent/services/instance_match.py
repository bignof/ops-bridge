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
