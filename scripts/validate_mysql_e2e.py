from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path


MYSQL_PASSWORD = "mazvMs93fYhUgv6NiHBH"
MYSQL_CONTAINER = "mysql8"
MYSQL_DATABASE = "service_hub_e2e"
HUB_CONTAINER = "service-hub-v2-mysql-e2e"
AGENT_CONTAINER = "service-agent-v2-mysql-e2e"
TARGET_CONTAINER = "orchidea-v2-mysql-host-nginx"
TEST_ROOT = "/tmp/orchidea-v2-mysql-host"


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), capture_output=True, text=True, check=check)


def wsl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run("wsl.exe", *args, check=check)


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return wsl("docker", *args, check=check)


def bash(script: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return wsl("bash", "-lc", script, check=check)


def docker_exec_python(container: str, source: str) -> str:
    result = docker("exec", container, "python", "-c", source, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            "docker exec python failed\n"
            f"container={container}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result.stdout.strip()


def cleanup() -> None:
    bash(
        (
            f"docker rm -f {AGENT_CONTAINER} {HUB_CONTAINER} {TARGET_CONTAINER} >/dev/null 2>&1 || true; "
            f"docker compose -f {TEST_ROOT}/managed/e2e-app/docker-compose.yml down >/dev/null 2>&1 || true; "
            f"rm -rf {TEST_ROOT}"
        ),
        check=False,
    )


def prepare_database() -> None:
    docker(
        "exec",
        MYSQL_CONTAINER,
        "mysql",
        "-uroot",
        f"-p{MYSQL_PASSWORD}",
        "-e",
        (
            f"DROP DATABASE IF EXISTS {MYSQL_DATABASE}; "
            f"CREATE DATABASE {MYSQL_DATABASE} CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;"
        ),
    )


def prepare_target_compose() -> None:
    bash(
        f"mkdir -p {TEST_ROOT}/managed/e2e-app && cat > {TEST_ROOT}/managed/e2e-app/docker-compose.yml <<'EOF'\n"
        "services:\n"
        "  app:\n"
        "    image: nginx:1.27-alpine\n"
        f"    container_name: {TARGET_CONTAINER}\n"
        "EOF"
    )


def build_images() -> None:
    hub_root = "/mnt/c/Users/bigno/Documents/work/orchisky/src/orchidea/service-hub"
    agent_root = "/mnt/c/Users/bigno/Documents/work/orchisky/src/orchidea/service-agent"
    bash(
        f"cd {hub_root} && docker build -t service-hub:v2-mysql-e2e . >/tmp/service-hub-v2-mysql-e2e.log && "
        f"cd {agent_root} && docker build -t service-agent:v2-mysql-e2e . >/tmp/service-agent-v2-mysql-e2e.log"
    )


def start_environment() -> None:
    cleanup()
    prepare_database()
    prepare_target_compose()
    build_images()
    bash(f"docker compose -f {TEST_ROOT}/managed/e2e-app/docker-compose.yml up -d >/dev/null")
    docker(
        "run",
        "-d",
        "--name",
        HUB_CONTAINER,
        "--network",
        f"container:{MYSQL_CONTAINER}",
        "-e",
        "AUTH_TOKEN=local-test-token",
        "-e",
        "PORT=8080",
        "-e",
        f"DATABASE_URL=mysql+pymysql://root:{MYSQL_PASSWORD}@127.0.0.1:3306/{MYSQL_DATABASE}",
        "service-hub:v2-mysql-e2e",
    )
    docker(
        "run",
        "-d",
        "--name",
        AGENT_CONTAINER,
        "--network",
        f"container:{MYSQL_CONTAINER}",
        "-e",
        "WS_URL=ws://127.0.0.1:8080/ws/agent",
        "-e",
        "AGENT_ID=v2-mysql-agent",
        "-e",
        "TOKEN=local-test-token",
        "-e",
        "RECONNECT_DELAY=2",
        "-e",
        "HEARTBEAT_INTERVAL=5",
        "-e",
        "HEALTH_PORT=18081",
        "-v",
        "/var/run/docker.sock:/var/run/docker.sock",
        "-v",
        f"{TEST_ROOT}/managed:/data",
        "service-agent:v2-mysql-e2e",
    )
    time.sleep(12)


def wait_for_command(request_id: str) -> dict:
    for _ in range(30):
        status = json.loads(
            docker_exec_python(
                HUB_CONTAINER,
                (
                    "import urllib.request; "
                    f"print(urllib.request.urlopen('http://127.0.0.1:8080/api/commands/{request_id}', timeout=10).read().decode())"
                ),
            )
        )
        if status["status"] not in {"queued", "processing"}:
            return status
        time.sleep(2)
    return status


def restart_hub() -> None:
    docker("rm", "-f", HUB_CONTAINER)
    docker(
        "run",
        "-d",
        "--name",
        HUB_CONTAINER,
        "--network",
        f"container:{MYSQL_CONTAINER}",
        "-e",
        "AUTH_TOKEN=local-test-token",
        "-e",
        "PORT=8080",
        "-e",
        f"DATABASE_URL=mysql+pymysql://root:{MYSQL_PASSWORD}@127.0.0.1:3306/{MYSQL_DATABASE}",
        "service-hub:v2-mysql-e2e",
    )
    time.sleep(8)


def main() -> int:
    try:
        start_environment()

        dispatch = json.loads(
            docker_exec_python(
                HUB_CONTAINER,
                (
                    "import json, urllib.request; "
                    "body=json.dumps({'action':'restart','dir':'/data/e2e-app'}).encode(); "
                    "req=urllib.request.Request('http://127.0.0.1:8080/api/agents/v2-mysql-agent/commands',"
                    "data=body, headers={'Content-Type':'application/json','X-Requested-By':'copilot-e2e','X-Requested-Source':'mysql-validation'}, method='POST'); "
                    "print(urllib.request.urlopen(req, timeout=10).read().decode())"
                ),
            )
        )
        request_id = dispatch["command"]["requestId"]
        final_status = wait_for_command(request_id)
        events_before = json.loads(
            docker_exec_python(
                HUB_CONTAINER,
                (
                    "import urllib.request; "
                    f"print(urllib.request.urlopen('http://127.0.0.1:8080/api/commands/{request_id}/events', timeout=10).read().decode())"
                ),
            )
        )

        if dispatch["command"]["requestedBy"] != "copilot-e2e":
            raise RuntimeError("requestedBy was not persisted")
        if dispatch["command"]["requestSource"] != "mysql-validation":
            raise RuntimeError("requestSource was not persisted")
        if final_status["status"] != "success":
            raise RuntimeError(f"command did not succeed: {final_status}")
        if [event["eventType"] for event in events_before] != ["created", "ack", "result"]:
            raise RuntimeError(f"unexpected event sequence: {events_before}")

        restart_hub()

        persisted_status = json.loads(
            docker_exec_python(
                HUB_CONTAINER,
                (
                    "import urllib.request; "
                    f"print(urllib.request.urlopen('http://127.0.0.1:8080/api/commands/{request_id}', timeout=10).read().decode())"
                ),
            )
        )
        persisted_events = json.loads(
            docker_exec_python(
                HUB_CONTAINER,
                (
                    "import urllib.request; "
                    f"print(urllib.request.urlopen('http://127.0.0.1:8080/api/commands/{request_id}/events', timeout=10).read().decode())"
                ),
            )
        )
        agents_after = json.loads(
            docker_exec_python(
                HUB_CONTAINER,
                "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/api/agents', timeout=10).read().decode())",
            )
        )

        if persisted_status["requestedBy"] != "copilot-e2e":
            raise RuntimeError("requestedBy was not preserved after hub restart")
        if persisted_status["requestSource"] != "mysql-validation":
            raise RuntimeError("requestSource was not preserved after hub restart")

        summary = {
            "dispatch": dispatch,
            "final_status_before_restart": final_status,
            "events_before_restart": events_before,
            "persisted_status_after_restart": persisted_status,
            "persisted_events_after_restart": persisted_events,
            "agents_after_restart": agents_after,
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    finally:
        hub_result = docker("logs", "--tail", "30", HUB_CONTAINER, check=False)
        agent_result = docker("logs", "--tail", "30", AGENT_CONTAINER, check=False)
        hub_logs = (hub_result.stdout + hub_result.stderr).strip()
        agent_logs = (agent_result.stdout + agent_result.stderr).strip()
        if hub_logs:
            print("\n=== hub logs ===")
            print(hub_logs)
        if agent_logs:
            print("\n=== agent logs ===")
            print(agent_logs)
        cleanup()


if __name__ == "__main__":
    sys.exit(main())