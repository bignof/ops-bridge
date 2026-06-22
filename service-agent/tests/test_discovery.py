import json

import pytest

from services import discovery, docker_cli


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
