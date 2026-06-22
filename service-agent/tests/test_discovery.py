import json

import pytest

from services import discovery, docker_cli, instance_match


# --------------------------------------------------------------------------- #
# docker_cli.list_all_containers（docker ps -aq + inspect）
# --------------------------------------------------------------------------- #


def test_list_all_containers_uses_ps_aq_and_inspect(monkeypatch):
    calls = []

    def fake_run(args, timeout=60):
        calls.append(args)
        if args[0] == "ps":
            return True, "id1\nid2\n"
        if args[0] == "inspect":
            return True, json.dumps([{"Id": "id1"}, {"Id": "id2"}])
        raise AssertionError(args)

    monkeypatch.setattr(docker_cli, "run_docker", fake_run)
    out = docker_cli.list_all_containers()
    assert [c["Id"] for c in out] == ["id1", "id2"]
    assert calls[0] == ["ps", "-aq"]  # -aq：含 stopped
    assert calls[1] == ["inspect", "id1", "id2"]


def test_list_all_containers_empty_returns_empty(monkeypatch):
    monkeypatch.setattr(docker_cli, "run_docker", lambda args, timeout=60: (True, "\n  \n"))
    assert docker_cli.list_all_containers() == []


def test_list_all_containers_ps_failure_raises(monkeypatch):
    monkeypatch.setattr(docker_cli, "run_docker", lambda args, timeout=60: (False, "boom"))
    with pytest.raises(RuntimeError):
        docker_cli.list_all_containers()


def test_list_all_containers_inspect_failure_raises(monkeypatch):
    def fake_run(args, timeout=60):
        if args[0] == "ps":
            return True, "id1\n"
        return False, "inspect boom"

    monkeypatch.setattr(docker_cli, "run_docker", fake_run)
    with pytest.raises(RuntimeError):
        docker_cli.list_all_containers()


# --------------------------------------------------------------------------- #
# discovery.collect_local_containers
# --------------------------------------------------------------------------- #


def _c(*, cid="c1", name="/proj-admin-1", project="proj", service="admin", workdir="/data/admin", image="img:1", running=True):
    return {
        "Id": cid,
        "Name": name,
        "State": {"Running": running},
        "Config": {
            "Image": image,
            "Labels": {
                "com.docker.compose.project": project,
                "com.docker.compose.service": service,
                "com.docker.compose.project.working_dir": workdir,
            },
        },
    }


def test_collect_extracts_fields_and_strips_name(monkeypatch):
    monkeypatch.setattr(docker_cli, "list_all_containers", lambda timeout=30: [_c()])
    out = discovery.collect_local_containers(managed_root="/data")
    assert out == [
        {
            "containerId": "c1",
            "containerName": "proj-admin-1",  # 去前导 /
            "composeProject": "proj",
            "composeService": "admin",
            "dir": "/data/admin",
            "image": "img:1",
            "running": True,
        }
    ]


def test_collect_includes_stopped(monkeypatch):
    monkeypatch.setattr(
        docker_cli,
        "list_all_containers",
        lambda timeout=30: [_c(cid="r", running=True), _c(cid="s", running=False, workdir="/data/b")],
    )
    out = discovery.collect_local_containers(managed_root="/data")
    assert {r["containerId"]: r["running"] for r in out} == {"r": True, "s": False}


def test_collect_skips_non_compose_containers(monkeypatch):
    non_compose = {"Id": "x", "Name": "/x", "State": {"Running": True}, "Config": {"Image": "i", "Labels": {}}}
    monkeypatch.setattr(docker_cli, "list_all_containers", lambda timeout=30: [non_compose, _c()])
    out = discovery.collect_local_containers(managed_root="")
    assert [r["containerId"] for r in out] == ["c1"]


@pytest.mark.parametrize(
    "workdir,root,kept",
    [
        ("/data/admin", "/data", True),
        ("/data", "/data", True),  # 等于根
        ("/other/admin", "/data", False),  # 根外
        ("/datax/admin", "/data", False),  # 前缀串扰(/datax 不在 /data 下)
        (None, "/data", False),  # 有根但容器无 working_dir
        ("/anything", "", True),  # 无根 = 不过滤
    ],
)
def test_collect_managed_root_filter(monkeypatch, workdir, root, kept):
    monkeypatch.setattr(docker_cli, "list_all_containers", lambda timeout=30: [_c(workdir=workdir)])
    out = discovery.collect_local_containers(managed_root=root)
    assert (len(out) == 1) is kept


def test_collect_handles_missing_config_name_and_state(monkeypatch):
    bare = {"Id": "b"}  # 无 Config → 无 project label → 跳过
    minimal = {
        "Id": "n",
        "Name": "",  # 空名 → None
        "State": {},  # 无 Running → False
        "Config": {"Labels": {"com.docker.compose.project": "p"}},  # 无 service/workdir/image
    }
    monkeypatch.setattr(docker_cli, "list_all_containers", lambda timeout=30: [bare, minimal])
    out = discovery.collect_local_containers(managed_root="")
    assert out == [
        {
            "containerId": "n",
            "containerName": None,
            "composeProject": "p",
            "composeService": None,
            "dir": None,
            "image": None,
            "running": False,
        }
    ]


def test_collect_uses_passed_containers_without_calling_docker(monkeypatch):
    def boom(timeout=30):
        raise AssertionError("传了 containers 就不该再调 docker")

    monkeypatch.setattr(docker_cli, "list_all_containers", boom)
    out = discovery.collect_local_containers(managed_root="", containers=[_c()])
    assert [r["containerId"] for r in out] == ["c1"]


# --------------------------------------------------------------------------- #
# instance_match.matching_containers（P3-2 冲突检测用)
# --------------------------------------------------------------------------- #


def _raw(cid, *, host_port=None, ip=None, project="proj", service="svc", workdir="/data/a", running=True, image="img:1"):
    ports = {"13000/tcp": [{"HostPort": str(host_port)}]} if host_port is not None else {}
    networks = {"bridge": {"IPAddress": ip}} if ip is not None else {}
    return {
        "Id": cid,
        "Name": f"/{project}-{service}-1",
        "State": {"Running": running},
        "NetworkSettings": {"Ports": ports, "Networks": networks},
        "Config": {
            "Image": image,
            "Labels": {
                "com.docker.compose.project": project,
                "com.docker.compose.service": service,
                "com.docker.compose.project.working_dir": workdir,
            },
        },
    }


def test_matching_containers_by_port_returns_all_candidates():
    a = _raw("a", host_port=18029)
    b = _raw("b", host_port=18029)  # 同宿主端口(异常,但要被检出)
    c = _raw("c", host_port=19000)
    got = instance_match.matching_containers({"port": 18029, "ip": "x"}, [a, b, c])
    assert {x["Id"] for x in got} == {"a", "b"}


def test_matching_containers_falls_back_to_ip_when_no_port():
    a = _raw("a", host_port=19000, ip="172.18.0.5")
    got = instance_match.matching_containers({"port": 18029, "ip": "172.18.0.5"}, [a])
    assert [x["Id"] for x in got] == ["a"]


def test_matching_containers_empty_when_no_port_and_no_ip():
    a = _raw("a", host_port=19000, ip="172.18.0.9")
    assert instance_match.matching_containers({"port": 18029}, [a]) == []  # 无 ip key
    assert instance_match.matching_containers({"port": 18029, "ip": "1.2.3.4"}, [a]) == []


# --------------------------------------------------------------------------- #
# discovery.enrich_with_nacos
# --------------------------------------------------------------------------- #


def test_enrich_single_match_attaches_service_and_health():
    raw = [_raw("a", host_port=18029)]
    records = discovery.collect_local_containers(containers=raw)
    instances = [{"serviceName": "wms", "ip": "10.0.0.1", "port": 18029, "healthy": True}]
    enriched, warnings = discovery.enrich_with_nacos(records, raw, instances)
    assert warnings == []
    assert enriched[0]["nacosService"] == "wms"
    assert enriched[0]["healthy"] is True


def test_enrich_no_match_keeps_none(monkeypatch):
    raw = [_raw("a", host_port=18029)]
    records = discovery.collect_local_containers(containers=raw)
    enriched, warnings = discovery.enrich_with_nacos(records, raw, [])  # 无 nacos 实例(已停/未注册仍报出)
    assert warnings == []
    assert enriched[0]["nacosService"] is None
    assert enriched[0]["healthy"] is None


def test_enrich_unhealthy_instance_propagates():
    raw = [_raw("a", host_port=18029)]
    records = discovery.collect_local_containers(containers=raw)
    instances = [{"serviceName": "wms", "ip": "x", "port": 18029, "healthy": False}]
    enriched, _ = discovery.enrich_with_nacos(records, raw, instances)
    assert enriched[0]["healthy"] is False


def test_enrich_one_instance_multi_container_warns():
    raw = [_raw("a", host_port=18029), _raw("b", host_port=18029)]
    records = discovery.collect_local_containers(containers=raw)
    instances = [{"serviceName": "wms", "ip": "x", "port": 18029, "healthy": True}]
    enriched, warnings = discovery.enrich_with_nacos(records, raw, instances)
    assert [w["type"] for w in warnings] == ["instance-multi-container"]
    assert warnings[0]["nacosService"] == "wms"
    assert set(warnings[0]["containerIds"]) == {"a", "b"}
    assert all(r["nacosService"] == "wms" for r in enriched)  # 各容器各 1 实例命中 → 仍可标


def test_enrich_one_container_multi_instance_warns_and_nulls():
    raw = [_raw("a", host_port=18029)]
    records = discovery.collect_local_containers(containers=raw)
    instances = [
        {"serviceName": "wms", "ip": "x", "port": 18029, "healthy": True},
        {"serviceName": "erp", "ip": "y", "port": 18029, "healthy": True},  # 都认领容器 a
    ]
    enriched, warnings = discovery.enrich_with_nacos(records, raw, instances)
    assert [w["type"] for w in warnings] == ["container-multi-instance"]
    assert set(warnings[0]["nacosServices"]) == {"wms", "erp"}
    assert enriched[0]["nacosService"] is None  # 歧义不猜
    assert enriched[0]["healthy"] is None


def test_enrich_does_not_mutate_input_records():
    raw = [_raw("a", host_port=18029)]
    records = discovery.collect_local_containers(containers=raw)
    snapshot = dict(records[0])
    discovery.enrich_with_nacos(records, raw, [{"serviceName": "wms", "ip": "x", "port": 18029, "healthy": True}])
    assert records[0] == snapshot  # 原 record 未被注入 nacosService
