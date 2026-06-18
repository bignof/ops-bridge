from services.instance_match import match_instance

def _c(cid, host_port=None, ip=None):
    ports = {}
    if host_port is not None:
        ports = {"80/tcp": [{"HostIp": "0.0.0.0", "HostPort": str(host_port)}]}
    nets = {"bridge": {"IPAddress": ip}} if ip else {}
    return {"Id": cid, "NetworkSettings": {"Ports": ports, "Networks": nets}}

def test_match_by_published_port():
    containers = [_c("a", host_port=18029), _c("b", host_port=18030)]
    assert match_instance({"ip": "192.168.0.30", "port": 18029}, containers)["Id"] == "a"

def test_match_by_bridge_ip_fallback():
    containers = [_c("a", host_port=None, ip="172.17.0.5")]
    assert match_instance({"ip": "172.17.0.5", "port": 13000}, containers)["Id"] == "a"

def test_no_match_returns_none():
    containers = [_c("a", host_port=18029)]
    assert match_instance({"ip": "10.9.9.9", "port": 9999}, containers) is None

def test_port_takes_priority_over_ip():
    # 端口命中 b，IP 命中 a；应返回 b（端口优先）
    containers = [_c("a", host_port=None, ip="192.168.0.30"), _c("b", host_port=18029)]
    assert match_instance({"ip": "192.168.0.30", "port": 18029}, containers)["Id"] == "b"
