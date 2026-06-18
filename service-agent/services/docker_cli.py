import json
import subprocess


def run_docker(args, timeout=60):
    cmd = ["docker"] + args
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return result.returncode == 0, result.stdout + result.stderr


def list_running_containers(timeout=30):
    ok, out = run_docker(["ps", "-q"], timeout=timeout)
    if not ok:
        raise RuntimeError(f"docker ps failed: {out}")
    ids = [line.strip() for line in out.splitlines() if line.strip()]
    if not ids:
        return []
    ok, out = run_docker(["inspect"] + ids, timeout=timeout)
    if not ok:
        raise RuntimeError(f"docker inspect failed: {out}")
    return json.loads(out)


def restart_container(container_id, timeout=120):
    return run_docker(["restart", container_id], timeout=timeout)
