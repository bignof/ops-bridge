from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


NETWORK = os.getenv("LOGS_E2E_NETWORK", "service-hub-logs-e2e")
HUB_CONTAINER = os.getenv("LOGS_E2E_HUB_CONTAINER", "service-hub-logs-e2e")
AGENT_CONTAINER = os.getenv("LOGS_E2E_AGENT_CONTAINER", "service-agent-logs-e2e")
TARGET_CONTAINER = os.getenv("LOGS_E2E_TARGET_CONTAINER", "service-target-logs-e2e")
TEST_ROOT = os.getenv("LOGS_E2E_TEST_ROOT", "/tmp/service-hub-logs-e2e")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "logs-e2e-admin-token")
AGENT_ID = os.getenv("AGENT_ID", "logs-e2e-agent")
MANAGED_DIR = "/data/logs-app"
LOG_MARKER = "log-pulse-"
REQUESTED_BY = "logs-e2e"
REQUEST_SOURCE = "logs-stream-validation"
HEALTH_PORT = 18081
EXPECTED_CHUNKS = 3

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
            f"docker rm -f {AGENT_CONTAINER} {HUB_CONTAINER} {TARGET_CONTAINER} >/dev/null 2>&1 || true; "
            f"docker compose -f {TEST_ROOT}/managed/logs-app/docker-compose.yml down >/dev/null 2>&1 || true; "
            f"docker network rm {NETWORK} >/dev/null 2>&1 || true; "
            f"rm -rf {TEST_ROOT}"
        ),
        check=False,
    )


def prepare_filesystem() -> None:
    shell(
        f"mkdir -p {TEST_ROOT}/hub-data {TEST_ROOT}/managed/logs-app && cat > {TEST_ROOT}/managed/logs-app/docker-compose.yml <<'EOF'\n"
        "services:\n"
        "  app:\n"
        "    image: busybox:1.36\n"
        f"    container_name: {TARGET_CONTAINER}\n"
        "    command:\n"
        "      - /bin/sh\n"
        "      - -lc\n"
        "      - |\n"
        "          while true; do\n"
        f"            date '+{LOG_MARKER}%s'\n"
        "            sleep 1\n"
        "          done\n"
        "EOF"
    )


def build_images() -> None:
    hub_root = repo_path(REPO_ROOT / "service-hub")
    agent_root = repo_path(REPO_ROOT / "service-agent")
    shell(
        f"cd {hub_root} && docker build -t service-hub:logs-e2e . >/tmp/service-hub-logs-e2e.log && "
        f"cd {agent_root} && docker build -t service-agent:logs-e2e . >/tmp/service-agent-logs-e2e.log"
    )


def create_network() -> None:
    docker("network", "create", NETWORK, check=False)


def start_target() -> None:
    shell(f"docker compose -f {TEST_ROOT}/managed/logs-app/docker-compose.yml up -d >/dev/null")


def wait_for_target_logs() -> str:
    for _ in range(20):
        result = docker("logs", "--tail", "20", TARGET_CONTAINER, check=False)
        output = (result.stdout + result.stderr).strip()
        if LOG_MARKER in output:
            return output
        time.sleep(1)
    raise RuntimeError("target container did not emit expected logs in time")


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
        "service-hub:logs-e2e",
    )


def wait_for_hub() -> dict:
    for _ in range(30):
        try:
            return hub_request("/health")
        except Exception:
            time.sleep(1)
    raise RuntimeError("service-hub did not become healthy in time")


def provision_agent() -> str:
    response = hub_request(
        "/api/agents",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Admin-Token": ADMIN_TOKEN,
        },
        body={"agentId": AGENT_ID},
    )
    return response["agentKey"]


def start_agent(agent_key: str) -> None:
    docker(
        "run",
        "-d",
        "--name",
        AGENT_CONTAINER,
        "--network",
        NETWORK,
        "-e",
        f"WS_URL=ws://{HUB_CONTAINER}:8080/ws/agent",
        "-e",
        f"AGENT_ID={AGENT_ID}",
        "-e",
        f"AGENT_KEY={agent_key}",
        "-e",
        "RECONNECT_DELAY=2",
        "-e",
        "HEARTBEAT_INTERVAL=5",
        "-e",
        f"HEALTH_PORT={HEALTH_PORT}",
        "-v",
        f"{TEST_ROOT}/managed:/data",
        "-v",
        "/var/run/docker.sock:/var/run/docker.sock",
        "service-agent:logs-e2e",
    )


def wait_for_agent_online() -> dict:
    for _ in range(40):
        agent = hub_request(f"/api/agents/{AGENT_ID}")
        if agent["online"]:
            return agent
        time.sleep(2)
    raise RuntimeError(f"agent {AGENT_ID} did not become online in time")


def wait_for_agent_health() -> dict:
    source = (
        "import json, urllib.request; "
        f"response = urllib.request.urlopen('http://127.0.0.1:{HEALTH_PORT}/health', timeout=10); "
        "print(response.read().decode())"
    )
    for _ in range(20):
        try:
            payload = json.loads(docker_exec_python(AGENT_CONTAINER, source))
            if payload.get("connected"):
                return payload
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError("agent health endpoint did not report a connected state in time")


def stream_logs() -> dict:
    headers_json = json.dumps(
        {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "X-Requested-By": REQUESTED_BY,
            "X-Requested-Source": REQUEST_SOURCE,
        },
        ensure_ascii=False,
    )
    body_json = json.dumps(
        {
            "dir": MANAGED_DIR,
            "tail": 1,
            "timestamps": False,
        },
        ensure_ascii=False,
    )
    source = "\n".join(
        [
            "import json, urllib.request",
            f"headers = json.loads({headers_json!r})",
            f"payload = json.loads({body_json!r})",
            f"expected_chunks = {EXPECTED_CHUNKS}",
            f"req = urllib.request.Request('http://127.0.0.1:8080/api/agents/{AGENT_ID}/logs/stream', data=json.dumps(payload).encode('utf-8'), headers=headers, method='POST')",
            "response = urllib.request.urlopen(req, timeout=30)",
            "session_id = response.headers.get('X-Log-Session-Id')",
            "content_type = response.headers.get('Content-Type')",
            "events = []",
            "chunks = []",
            "current_event = None",
            "while True:",
            "    raw_line = response.readline()",
            "    if not raw_line:",
            "        break",
            "    line = raw_line.decode('utf-8').rstrip('\\r\\n')",
            "    if not line:",
            "        continue",
            "    if line.startswith('event: '):",
            "        current_event = line[7:]",
            "        continue",
            "    if not line.startswith('data: '):",
            "        continue",
            "    payload = json.loads(line[6:])",
            "    events.append({'event': current_event, 'data': payload})",
            "    if current_event == 'chunk':",
            "        chunks.append(payload.get('chunk', ''))",
            "    if current_event == 'error' or len(chunks) >= expected_chunks:",
            "        break",
            "response.close()",
            "print(json.dumps({'status': getattr(response, 'status', None), 'contentType': content_type, 'sessionId': session_id, 'events': events, 'chunks': chunks}, ensure_ascii=False))",
        ]
    )
    return json.loads(docker_exec_python(HUB_CONTAINER, source))


def container_logs(container: str, *, tail: int = 120) -> str:
    result = docker("logs", "--tail", str(tail), container, check=False)
    if result.returncode != 0:
        return ""
    return (result.stdout + result.stderr).strip()


def assert_stream_result(stream_result: dict, agent_logs: str) -> None:
    if stream_result.get("status") != 200:
        raise RuntimeError(f"unexpected stream response status: {stream_result}")

    content_type = stream_result.get("contentType") or ""
    if "text/event-stream" not in content_type:
        raise RuntimeError(f"unexpected stream content type: {stream_result}")

    session_id = stream_result.get("sessionId") or ""
    if not session_id:
        raise RuntimeError(f"missing log session id in stream response: {stream_result}")

    events = stream_result.get("events") or []
    event_names = [event.get("event") for event in events]
    if "started" not in event_names:
        raise RuntimeError(f"log stream did not emit a started event: {stream_result}")
    if "error" in event_names:
        raise RuntimeError(f"log stream emitted an unexpected error event: {stream_result}")

    chunks = stream_result.get("chunks") or []
    if len(chunks) < EXPECTED_CHUNKS:
        raise RuntimeError(f"log stream returned too few chunks: {stream_result}")
    if not all(LOG_MARKER in chunk for chunk in chunks):
        raise RuntimeError(f"log chunks did not contain the expected marker {LOG_MARKER!r}: {stream_result}")
    if len(set(chunks)) < 2:
        raise RuntimeError(f"log stream chunks did not change across follow mode: {stream_result}")

    started_event = next((event for event in events if event.get("event") == "started"), None)
    started_payload = started_event["data"] if started_event is not None else {}
    if started_payload.get("tail") != 1:
        raise RuntimeError(f"log stream started with an unexpected tail value: {stream_result}")

    if f"Starting log session: session_id={session_id}" not in agent_logs:
        raise RuntimeError(f"agent logs did not show the log session start for {session_id}")
    if f"Stopping log session: session_id={session_id}" not in agent_logs:
        raise RuntimeError(f"agent logs did not show the log session stop for {session_id}")


def main() -> int:
    validation_completed = False
    try:
        cleanup()
        prepare_filesystem()
        build_images()
        create_network()
        start_target()
        target_log_preview = wait_for_target_logs()
        start_hub()
        hub_health = wait_for_hub()
        agent_key = provision_agent()
        start_agent(agent_key)
        agent_snapshot = wait_for_agent_online()
        agent_health = wait_for_agent_health()

        stream_result = stream_logs()
        time.sleep(3)
        hub_logs = container_logs(HUB_CONTAINER)
        agent_logs = container_logs(AGENT_CONTAINER)
        target_logs = container_logs(TARGET_CONTAINER)

        assert_stream_result(stream_result, agent_logs)

        summary = {
            "hub_health": hub_health,
            "agent_snapshot": agent_snapshot,
            "agent_health": agent_health,
            "target_log_preview": target_log_preview,
            "stream_result": stream_result,
            "hub_logs_tail": hub_logs,
            "agent_logs_tail": agent_logs,
            "target_logs_tail": target_logs,
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        validation_completed = True
        return 0
    finally:
        hub_logs = container_logs(HUB_CONTAINER)
        agent_logs = container_logs(AGENT_CONTAINER)
        target_logs = container_logs(TARGET_CONTAINER)
        if hub_logs:
            print("\n=== hub logs ===")
            print(hub_logs)
        if agent_logs:
            print("\n=== agent logs ===")
            print(agent_logs)
        if target_logs:
            print("\n=== target logs ===")
            print(target_logs)
        if validation_completed and "Connected to ServiceHub!" not in agent_logs:
            raise RuntimeError("agent logs did not show a successful connection to service-hub")
        cleanup()


if __name__ == "__main__":
    sys.exit(main())
