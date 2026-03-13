from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


NETWORK = os.getenv("PHASE1_NETWORK", "service-hub-phase1-e2e")
HUB_CONTAINER = os.getenv("PHASE1_HUB_CONTAINER", "service-hub-phase1-e2e")
AGENT_CONTAINER = os.getenv("PHASE1_AGENT_CONTAINER", "service-agent-phase1-e2e")
NO_DOCKER_AGENT_CONTAINER = os.getenv("PHASE1_NO_DOCKER_AGENT_CONTAINER", "service-agent-phase1-no-docker-e2e")
TARGET_CONTAINER = os.getenv("PHASE1_TARGET_CONTAINER", "service-target-phase1-e2e")
TEST_ROOT = os.getenv("PHASE1_TEST_ROOT", "/tmp/service-hub-phase1-e2e")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "phase1-local-admin-token")
AGENT_ID = os.getenv("AGENT_ID", "phase1-agent")
NO_DOCKER_AGENT_ID = os.getenv("PHASE1_NO_DOCKER_AGENT_ID", "phase1-agent-no-docker")
INITIAL_IMAGE = os.getenv("INITIAL_IMAGE", "nginx:1.27-alpine")
UPDATED_IMAGE = os.getenv("UPDATED_IMAGE", "nginx:1.27.5-alpine")
MANAGED_DIR = "/data/e2e-app"
NO_COMPOSE_DIR = "/data/no-compose"
MISSING_DIR = "/data/missing-app"
PRIMARY_AGENT_HEALTH_PORT = 18081
SECONDARY_AGENT_HEALTH_PORT = 18082
REQUESTED_BY = "phase1-e2e"
RESTART_REQUEST_SOURCE = "phase1-restart"
UPDATE_REQUEST_SOURCE = "phase1-update"
MISSING_DIR_REQUEST_SOURCE = "phase1-missing-dir"
NO_COMPOSE_REQUEST_SOURCE = "phase1-no-compose"
NO_DOCKER_REQUEST_SOURCE = "phase1-no-docker"

REPO_ROOT = Path(__file__).resolve().parents[2]
USE_WSL_DOCKER = shutil.which("docker") is None and shutil.which("wsl.exe") is not None


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), capture_output=True, text=True, encoding="utf-8", errors="replace", check=check)


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    if USE_WSL_DOCKER:
        return run("wsl.exe", "docker", *args, check=check)
    return run("docker", *args, check=check)


def shell(script: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    if USE_WSL_DOCKER:
        return run("wsl.exe", "bash", "-lc", script, check=check)
    return run("bash", "-lc", script, check=check)


def repo_path(path: Path) -> str:
    if not USE_WSL_DOCKER:
        return str(path)
    drive = path.drive.rstrip(":").lower()
    if not drive:
        return str(path).replace("\\", "/")
    suffix = str(path)[2:].lstrip("\\/").replace("\\", "/")
    return f"/mnt/{drive}/{suffix}"


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


def hub_request(path: str, *, method: str = "GET", headers: dict[str, str] | None = None, body: dict | None = None) -> dict | list:
    headers_json = json.dumps(headers or {}, ensure_ascii=False)
    body_json = json.dumps(body, ensure_ascii=False) if body is not None else None
    source = [
        "import json, urllib.request",
        f"headers = json.loads({headers_json!r})",
    ]
    if body_json is None:
        source.append("data = None")
    else:
        source.extend(
            [
                f"payload = json.loads({body_json!r})",
                "data = json.dumps(payload).encode('utf-8')",
            ]
        )
    source.extend(
        [
            f"req = urllib.request.Request('http://127.0.0.1:8080{path}', data=data, headers=headers, method={method!r})",
            "print(urllib.request.urlopen(req, timeout=10).read().decode())",
        ]
    )
    return json.loads(docker_exec_python(HUB_CONTAINER, "; ".join(source)))


def cleanup() -> None:
    shell(
        (
            f"docker rm -f {AGENT_CONTAINER} {NO_DOCKER_AGENT_CONTAINER} {HUB_CONTAINER} {TARGET_CONTAINER} >/dev/null 2>&1 || true; "
            f"docker compose -f {TEST_ROOT}/managed/e2e-app/docker-compose.yml down >/dev/null 2>&1 || true; "
            f"docker network rm {NETWORK} >/dev/null 2>&1 || true; "
            f"rm -rf {TEST_ROOT}"
        ),
        check=False,
    )


def prepare_filesystem() -> None:
    shell(
        f"mkdir -p {TEST_ROOT}/hub-data {TEST_ROOT}/managed/e2e-app {TEST_ROOT}/managed/no-compose && cat > {TEST_ROOT}/managed/e2e-app/docker-compose.yml <<'EOF'\n"
        "services:\n"
        "  app:\n"
        f"    image: {INITIAL_IMAGE}\n"
        f"    container_name: {TARGET_CONTAINER}\n"
        "EOF"
    )


def build_images() -> None:
    hub_root = repo_path(REPO_ROOT / "service-hub")
    agent_root = repo_path(REPO_ROOT / "service-agent")
    shell(
        f"cd {hub_root} && docker build -t service-hub:phase1-e2e . >/tmp/service-hub-phase1-e2e.log && "
        f"cd {agent_root} && docker build -t service-agent:phase1-e2e . >/tmp/service-agent-phase1-e2e.log"
    )


def start_target() -> None:
    shell(f"docker compose -f {TEST_ROOT}/managed/e2e-app/docker-compose.yml up -d >/dev/null")


def create_network() -> None:
    docker("network", "create", NETWORK, check=False)


def start_hub() -> None:
    docker(
        "run",
        "-d",
        "--name",
        HUB_CONTAINER,
        "--network",
        NETWORK,
        "-e",
        f"ADMIN_TOKEN={ADMIN_TOKEN}",
        "-e",
        "PORT=8080",
        "-e",
        "DATABASE_URL=sqlite:////data/service-hub/service-hub.db",
        "-v",
        f"{TEST_ROOT}/hub-data:/data/service-hub",
        "service-hub:phase1-e2e",
    )


def wait_for_hub() -> dict:
    for _ in range(30):
        try:
            return hub_request("/health")
        except Exception:
            time.sleep(1)
    raise RuntimeError("service-hub did not become healthy in time")


def provision_agent(agent_id: str) -> str:
    response = hub_request(
        "/api/agents",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Admin-Token": ADMIN_TOKEN,
        },
        body={"agentId": agent_id},
    )
    return response["agentKey"]


def start_agent(
    *,
    container_name: str,
    agent_id: str,
    agent_key: str,
    health_port: int,
    mount_docker_socket: bool,
) -> None:
    args = [
        "run",
        "-d",
        "--name",
        container_name,
        "--network",
        NETWORK,
        "-e",
        f"WS_URL=ws://{HUB_CONTAINER}:8080/ws/agent",
        "-e",
        f"AGENT_ID={agent_id}",
        "-e",
        f"AGENT_KEY={agent_key}",
        "-e",
        "RECONNECT_DELAY=2",
        "-e",
        "HEARTBEAT_INTERVAL=5",
        "-e",
        f"HEALTH_PORT={health_port}",
        "-v",
        f"{TEST_ROOT}/managed:/data",
    ]
    if mount_docker_socket:
        args.extend(["-v", "/var/run/docker.sock:/var/run/docker.sock"])
    args.append("service-agent:phase1-e2e")
    docker(*args)


def wait_for_agent_online(agent_id: str) -> dict:
    for _ in range(40):
        agent = hub_request(f"/api/agents/{agent_id}")
        if agent["online"]:
            return agent
        time.sleep(2)
    raise RuntimeError(f"agent {agent_id} did not become online in time")


def wait_for_agent_health(container_name: str, health_port: int) -> dict:
    source = (
        "import json, urllib.request; "
        f"response = urllib.request.urlopen('http://127.0.0.1:{health_port}/health', timeout=10); "
        "print(response.read().decode())"
    )
    for _ in range(20):
        try:
            payload = json.loads(docker_exec_python(container_name, source))
            if payload.get("connected"):
                return payload
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError(f"agent health endpoint did not report a connected state in time for {container_name}")


def dispatch_command(
    action: str,
    *,
    agent_id: str,
    project_dir: str,
    image: str | None = None,
    request_source: str,
) -> dict:
    body = {
        "action": action,
        "dir": project_dir,
    }
    if image is not None:
        body["image"] = image
    return hub_request(
        f"/api/agents/{agent_id}/commands",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Requested-By": REQUESTED_BY,
            "X-Requested-Source": request_source,
        },
        body=body,
    )


def wait_for_command(request_id: str) -> dict:
    status = {}
    for _ in range(45):
        status = hub_request(f"/api/commands/{request_id}")
        if status["status"] not in {"queued", "processing"}:
            return status
        time.sleep(2)
    raise RuntimeError(f"command {request_id} did not finish in time: {status}")


def assert_event_sequence(request_id: str, expected_sequence: list[str]) -> list[dict]:
    events = hub_request(f"/api/commands/{request_id}/events")
    sequence = [event["eventType"] for event in events]
    if sequence != expected_sequence:
        raise RuntimeError(f"unexpected event sequence for {request_id}: {events}")
    return events


def assert_contains_any(value: str | None, expected_substrings: list[str], *, context: str) -> None:
    haystack = value or ""
    if any(item in haystack for item in expected_substrings):
        return
    raise RuntimeError(f"{context} did not contain any expected substring {expected_substrings!r}: {haystack!r}")


def assert_failed_command(
    *,
    agent_id: str,
    project_dir: str,
    request_source: str,
    expected_sequence: list[str],
    expected_error_substring: str | None = None,
    expected_output_substrings: list[str] | None = None,
) -> dict:
    dispatch = dispatch_command(
        "restart",
        agent_id=agent_id,
        project_dir=project_dir,
        request_source=request_source,
    )
    request_id = dispatch["command"]["requestId"]
    status = wait_for_command(request_id)
    events = assert_event_sequence(request_id, expected_sequence)

    if status["status"] != "failed":
        raise RuntimeError(f"expected failed status for {request_id}, got: {status}")
    if expected_error_substring is not None:
        error = status.get("error") or ""
        if expected_error_substring not in error:
            raise RuntimeError(f"expected error containing {expected_error_substring!r}, got: {status}")
    if expected_output_substrings is not None:
        assert_contains_any(status.get("output"), expected_output_substrings, context=f"command {request_id} output")

    return {
        "dispatch": dispatch,
        "final_status": status,
        "events": events,
    }


def inspect_target_image() -> str:
    result = docker("inspect", TARGET_CONTAINER, "--format", "{{.Config.Image}}", check=False)
    if result.returncode != 0:
        raise RuntimeError(f"failed to inspect target container image: {result.stderr.strip()}")
    return result.stdout.strip()


def read_compose_file() -> str:
    result = shell(f"cat {TEST_ROOT}/managed/e2e-app/docker-compose.yml", check=False)
    if result.returncode != 0:
        raise RuntimeError(f"failed to read test compose file: {result.stderr.strip()}")
    return result.stdout


def restart_hub() -> None:
    docker("rm", "-f", HUB_CONTAINER)
    start_hub()
    wait_for_hub()


def main() -> int:
    validation_completed = False
    try:
        cleanup()
        prepare_filesystem()
        build_images()
        create_network()
        start_target()
        start_hub()

        hub_health = wait_for_hub()
        agent_key = provision_agent(AGENT_ID)
        start_agent(
            container_name=AGENT_CONTAINER,
            agent_id=AGENT_ID,
            agent_key=agent_key,
            health_port=PRIMARY_AGENT_HEALTH_PORT,
            mount_docker_socket=True,
        )
        agent_snapshot = wait_for_agent_online(AGENT_ID)
        agent_health = wait_for_agent_health(AGENT_CONTAINER, PRIMARY_AGENT_HEALTH_PORT)

        restart_dispatch = dispatch_command(
            "restart",
            agent_id=AGENT_ID,
            project_dir=MANAGED_DIR,
            request_source=RESTART_REQUEST_SOURCE,
        )
        restart_request_id = restart_dispatch["command"]["requestId"]
        restart_status = wait_for_command(restart_request_id)
        restart_events = assert_event_sequence(restart_request_id, ["created", "ack", "result"])
        if restart_status["status"] != "success":
            raise RuntimeError(f"restart command failed: {restart_status}")

        update_dispatch = dispatch_command(
            "update",
            agent_id=AGENT_ID,
            project_dir=MANAGED_DIR,
            image=UPDATED_IMAGE,
            request_source=UPDATE_REQUEST_SOURCE,
        )
        update_request_id = update_dispatch["command"]["requestId"]
        update_status = wait_for_command(update_request_id)
        update_events = assert_event_sequence(update_request_id, ["created", "ack", "result"])
        if update_status["status"] != "success":
            raise RuntimeError(f"update command failed: {update_status}")

        compose_contents = read_compose_file()
        if f"image: {UPDATED_IMAGE}" not in compose_contents:
            raise RuntimeError("compose file was not updated to the requested image")

        target_image = inspect_target_image()
        if target_image != UPDATED_IMAGE:
            raise RuntimeError(f"target container image was not updated: expected {UPDATED_IMAGE}, got {target_image}")

        restart_hub()
        reconnected_agent = wait_for_agent_online(AGENT_ID)
        persisted_restart = hub_request(f"/api/commands/{restart_request_id}")
        persisted_update = hub_request(f"/api/commands/{update_request_id}")
        persisted_update_events = hub_request(f"/api/commands/{update_request_id}/events")

        if persisted_restart["requestedBy"] != REQUESTED_BY:
            raise RuntimeError("restart command requester metadata was not preserved")
        if persisted_update["requestSource"] != UPDATE_REQUEST_SOURCE:
            raise RuntimeError("update command source metadata was not preserved")
        if [event["eventType"] for event in persisted_update_events] != ["created", "ack", "result"]:
            raise RuntimeError("update command events were not preserved after hub restart")

        missing_dir_failure = assert_failed_command(
            agent_id=AGENT_ID,
            project_dir=MISSING_DIR,
            request_source=MISSING_DIR_REQUEST_SOURCE,
            expected_sequence=["created", "result"],
            expected_error_substring=f"Directory not found: {MISSING_DIR}",
        )
        no_compose_failure = assert_failed_command(
            agent_id=AGENT_ID,
            project_dir=NO_COMPOSE_DIR,
            request_source=NO_COMPOSE_REQUEST_SOURCE,
            expected_sequence=["created", "result"],
            expected_error_substring=f"No docker-compose.yaml/yml found in {NO_COMPOSE_DIR}",
        )

        no_docker_agent_key = provision_agent(NO_DOCKER_AGENT_ID)
        start_agent(
            container_name=NO_DOCKER_AGENT_CONTAINER,
            agent_id=NO_DOCKER_AGENT_ID,
            agent_key=no_docker_agent_key,
            health_port=SECONDARY_AGENT_HEALTH_PORT,
            mount_docker_socket=False,
        )
        no_docker_agent_snapshot = wait_for_agent_online(NO_DOCKER_AGENT_ID)
        no_docker_agent_health = wait_for_agent_health(NO_DOCKER_AGENT_CONTAINER, SECONDARY_AGENT_HEALTH_PORT)
        no_docker_failure = assert_failed_command(
            agent_id=NO_DOCKER_AGENT_ID,
            project_dir=MANAGED_DIR,
            request_source=NO_DOCKER_REQUEST_SOURCE,
            expected_sequence=["created", "ack", "result"],
            expected_output_substrings=[
                "Cannot connect to the Docker daemon",
                "error during connect",
                "docker.sock",
            ],
        )

        summary = {
            "hub_health": hub_health,
            "initial_agent_snapshot": agent_snapshot,
            "initial_agent_health": agent_health,
            "restart_command": {
                "dispatch": restart_dispatch,
                "final_status": restart_status,
                "events": restart_events,
            },
            "update_command": {
                "dispatch": update_dispatch,
                "final_status": update_status,
                "events": update_events,
                "compose_file": compose_contents,
                "target_container_image": target_image,
            },
            "failure_commands": {
                "missing_directory": missing_dir_failure,
                "missing_compose_file": no_compose_failure,
                "docker_unavailable": no_docker_failure,
            },
            "post_restart_agent_snapshot": reconnected_agent,
            "secondary_agent_snapshot": no_docker_agent_snapshot,
            "secondary_agent_health": no_docker_agent_health,
            "persisted_restart_command": persisted_restart,
            "persisted_update_command": persisted_update,
            "persisted_update_events": persisted_update_events,
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        validation_completed = True
        return 0
    finally:
        hub_result = docker("logs", "--tail", "80", HUB_CONTAINER, check=False)
        agent_result = docker("logs", "--tail", "80", AGENT_CONTAINER, check=False)
        no_docker_agent_result = docker("logs", "--tail", "80", NO_DOCKER_AGENT_CONTAINER, check=False)
        hub_logs = (hub_result.stdout + hub_result.stderr).strip()
        agent_logs = (agent_result.stdout + agent_result.stderr).strip()
        no_docker_agent_logs = (no_docker_agent_result.stdout + no_docker_agent_result.stderr).strip()
        if hub_result.returncode == 0 and hub_logs:
            print("\n=== hub logs ===")
            print(hub_logs)
        if agent_result.returncode == 0 and agent_logs:
            print("\n=== agent logs ===")
            print(agent_logs)
        if no_docker_agent_result.returncode == 0 and no_docker_agent_logs:
            print("\n=== no-docker agent logs ===")
            print(no_docker_agent_logs)
        if validation_completed and "Connected to ServiceHub!" not in agent_logs:
            raise RuntimeError("agent logs did not show a successful connection to service-hub")
        if validation_completed and "Connected to ServiceHub!" not in no_docker_agent_logs:
            raise RuntimeError("secondary agent logs did not show a successful connection to service-hub")
        cleanup()


if __name__ == "__main__":
    sys.exit(main())
