# 零中断滚动重启 — 实现计划 1 / 3：service-agent

> **For agentic workers:** REQUIRED SUB-SKILL: 用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 按任务逐个实现。步骤用 `- [ ]` 复选框跟踪。
> 配套设计：`docs/2026-06-18-zero-downtime-rolling-restart-design.md`（v2）。本计划只覆盖 **service-agent**；hub、平台见计划 2/3。

**Goal:** 给 service-agent 增加两个新 WS 命令——`list-instances`（查本地 nacos + docker 对号出 containerId）和 `graceful-restart`（按 containerId 优雅重启单节点），作为滚动重启的执行原语。

**Architecture:** 走独立 WS 消息 `type`（参照现有 `logs_*`，不进 `command`/HANDLERS、不碰 update/restart）。新增 4 个薄模块（http_client / docker_cli / nacos_client / instance_match）+ 1 个命令模块 core/rolling.py，在 ws_client 的 `_on_message` 加两个分支。全部用 `requests`/`subprocess` + monkeypatch 测试。

**Tech Stack:** Python 3.12、pytest 8.3.5、`requests`（已在 requirements、本次首次使用）、`subprocess` 调 docker CLI、`python-dotenv`。

## Global Constraints

- **覆盖率硬门禁 97%**：每个新函数（含失败分支）必须有测试，否则 CI 红。运行（cwd = `service-agent/`）：
  `pytest --cov=agent --cov=config --cov=core --cov=services --cov-report=term-missing --cov-fail-under=97 -q`
- 打桩只用 `monkeypatch.setattr` + 手写 `Fake*`，**不引入** `pytest-mock`/`responses`。
- 新命令**不进 `HANDLERS`、不要求 `dir`**（按 containerId，无 dir 概念）；现有 `update`/`restart`/`command` 分支一字不改。
- 结构化回包用**独立 result type**（`list-instances-result` / `graceful-restart-result`），不复用 `output/message/error` 三字符串通道。
- nacos REST 路径必须带 context-path 前缀（默认 `/nacos`）；nacos 凭证可选、不打进日志；config 新 env **不加 `sys.exit` 强校验**（nacos 是可选能力）。
- 提交信息中文（保留 conventional 前缀英文）；提交前 `git branch --show-current` 确认在功能分支（勿在主干）。

## 协议契约（计划 2/hub 依赖，务必稳定）

- 入站（hub→agent）：
  - `{type:'list-instances', requestId, serviceName}`
  - `{type:'graceful-restart', requestId, containerId, healthBaseUrl, settleSec, shutdownTimeoutSec, readyTimeoutSec}`
- 出站（agent→hub）：
  - `{type:'list-instances-result', requestId, status:'success'|'failed', instances:[{address, containerId, healthy, matched}], error?}`
  - `{type:'graceful-restart-result', requestId, status:'success'|'failed', error?}`（**不回 ack**——hub 靠"发命令即标 in-progress"做粗粒度进度，避免与现有 `ack`→`mark_ack`(按 CommandModel 查) 冲突）

## File Structure

- Create `service-agent/services/http_client.py` — `requests` 薄封装（便于打桩）：`get_json` / `get_status` / `post`。
- Create `service-agent/services/docker_cli.py` — `docker` CLI 封装：`run_docker` / `list_running_containers` / `restart_container`。
- Create `service-agent/services/nacos_client.py` — 查本地 nacos 健康实例（可选鉴权）：`list_healthy_instances`。
- Create `service-agent/services/instance_match.py` — 纯函数：把 nacos 实例对号到 docker 容器：`match_instance`。
- Create `service-agent/core/rolling.py` — 两个命令处理：`handle_list_instances` / `handle_graceful_restart`。
- Modify `service-agent/config.py` — 加 `NACOS_*` 配置。
- Modify `service-agent/core/ws_client.py:48-63` — `_on_message` 加两分支。
- Tests：`tests/test_http_client.py`、`tests/test_docker_cli.py`、`tests/test_nacos_client.py`、`tests/test_instance_match.py`、`tests/test_rolling.py`、改 `tests/test_ws_client.py`。

---

### Task 1: config 增加 NACOS_* 配置

**Files:**
- Modify: `service-agent/config.py`（在 `HEALTH_PORT` 行后追加）
- Test: `service-agent/tests/test_config.py`

**Interfaces:**
- Produces: `config.NACOS_SERVER` / `NACOS_NAMESPACE` / `NACOS_GROUP`(默认 `DEFAULT_GROUP`) / `NACOS_CONTEXT_PATH`(默认 `/nacos`) / `NACOS_USERNAME` / `NACOS_PASSWORD`，均 str。

- [ ] **Step 1: 写失败测试**

在 `tests/test_config.py` 追加：
```python
def test_nacos_defaults(monkeypatch):
    monkeypatch.setenv("WS_URL", "ws://x")
    monkeypatch.setenv("AGENT_KEY", "k")
    for v in ("NACOS_SERVER", "NACOS_NAMESPACE", "NACOS_USERNAME", "NACOS_PASSWORD"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.delenv("NACOS_GROUP", raising=False)
    monkeypatch.delenv("NACOS_CONTEXT_PATH", raising=False)
    import importlib, config
    importlib.reload(config)
    assert config.NACOS_SERVER == ""
    assert config.NACOS_GROUP == "DEFAULT_GROUP"
    assert config.NACOS_CONTEXT_PATH == "/nacos"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_config.py::test_nacos_defaults -q`
Expected: FAIL（`AttributeError: module 'config' has no attribute 'NACOS_SERVER'`）

- [ ] **Step 3: 实现**

在 `config.py` 的 `HEALTH_PORT = int(os.getenv('HEALTH_PORT', '18081'))` 之后追加：
```python
# --- nacos（滚动重启用，可选能力，勿加 sys.exit 强校验）---
NACOS_SERVER       = os.getenv('NACOS_SERVER', '')          # 形如 192.168.0.30:8848
NACOS_NAMESPACE    = os.getenv('NACOS_NAMESPACE', '')       # 空=public
NACOS_GROUP        = os.getenv('NACOS_GROUP', 'DEFAULT_GROUP')
NACOS_CONTEXT_PATH = os.getenv('NACOS_CONTEXT_PATH', '/nacos')
NACOS_USERNAME     = os.getenv('NACOS_USERNAME', '')
NACOS_PASSWORD     = os.getenv('NACOS_PASSWORD', '')
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_config.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add config.py tests/test_config.py
git commit -m "feat(agent): 增加 nacos 可选配置项(滚动重启用)"
```

---

### Task 2: http_client 薄封装

**Files:**
- Create: `service-agent/services/http_client.py`
- Test: `service-agent/tests/test_http_client.py`

**Interfaces:**
- Produces:
  - `get_json(url, params=None, timeout=10) -> dict`（非 2xx 抛 `requests.HTTPError`）
  - `get_status(url, timeout=5) -> int`（异常返回 0）
  - `post(url, timeout=60) -> tuple[int, str]`（返回 status_code, text）

- [ ] **Step 1: 写失败测试**

`tests/test_http_client.py`：
```python
import requests
from services import http_client

class FakeResp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text
    def json(self):
        return self._payload
    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

def test_get_json_ok(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda url, params=None, timeout=10: FakeResp(payload={"a": 1}))
    assert http_client.get_json("http://x", {"k": "v"}) == {"a": 1}

def test_get_status_handles_error(monkeypatch):
    def boom(*a, **k):
        raise requests.ConnectionError("refused")
    monkeypatch.setattr(requests, "get", boom)
    assert http_client.get_status("http://x") == 0

def test_post_returns_code_and_text(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda url, timeout=60: FakeResp(status=200, text="ok"))
    assert http_client.post("http://x") == (200, "ok")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_http_client.py -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'services.http_client'`）

- [ ] **Step 3: 实现**

`services/http_client.py`：
```python
import requests


def get_json(url, params=None, timeout=10):
    resp = requests.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def get_status(url, timeout=5):
    try:
        resp = requests.get(url, timeout=timeout)
        return resp.status_code
    except requests.RequestException:
        return 0


def post(url, timeout=60):
    resp = requests.post(url, timeout=timeout)
    return resp.status_code, resp.text
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_http_client.py -q`
Expected: PASS（3 passed）

- [ ] **Step 5: 提交**

```bash
git add services/http_client.py tests/test_http_client.py
git commit -m "feat(agent): 增加 http_client 薄封装(供 nacos/节点调用)"
```

---

### Task 3: docker_cli 封装

**Files:**
- Create: `service-agent/services/docker_cli.py`
- Test: `service-agent/tests/test_docker_cli.py`

**Interfaces:**
- Produces:
  - `run_docker(args: list[str], timeout=60) -> tuple[bool, str]`（ok, stdout+stderr）
  - `list_running_containers(timeout=30) -> list[dict]`（`docker inspect` 全量 JSON；docker ps/inspect 失败抛 RuntimeError；无容器返回 `[]`）
  - `restart_container(container_id: str, timeout=120) -> tuple[bool, str]`

- [ ] **Step 1: 写失败测试**

`tests/test_docker_cli.py`：
```python
import json
import subprocess
from services import docker_cli

class FakeProc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err

def test_run_docker_ok(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: FakeProc(0, "done", ""))
    assert docker_cli.run_docker(["ps"]) == (True, "done")

def test_list_running_containers_empty(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: FakeProc(0, "\n", ""))
    assert docker_cli.list_running_containers() == []

def test_list_running_containers_parses_inspect(monkeypatch):
    calls = []
    def fake_run(cmd, **k):
        calls.append(cmd)
        if cmd[:2] == ["docker", "ps"]:
            return FakeProc(0, "abc\n", "")
        return FakeProc(0, json.dumps([{"Id": "abc"}]), "")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert docker_cli.list_running_containers() == [{"Id": "abc"}]
    assert calls[1][:2] == ["docker", "inspect"]

def test_list_running_containers_ps_fail(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: FakeProc(1, "", "boom"))
    import pytest
    with pytest.raises(RuntimeError):
        docker_cli.list_running_containers()

def test_restart_container(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: FakeProc(0, "abc", ""))
    assert docker_cli.restart_container("abc") == (True, "abc")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_docker_cli.py -q`
Expected: FAIL（无 `services.docker_cli`）

- [ ] **Step 3: 实现**

`services/docker_cli.py`：
```python
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_docker_cli.py -q`
Expected: PASS（5 passed）

- [ ] **Step 5: 提交**

```bash
git add services/docker_cli.py tests/test_docker_cli.py
git commit -m "feat(agent): 增加 docker_cli 封装(ps/inspect/restart)"
```

---

### Task 4: instance_match 对号纯函数

**Files:**
- Create: `service-agent/services/instance_match.py`
- Test: `service-agent/tests/test_instance_match.py`

**Interfaces:**
- Consumes: docker inspect dict（`NetworkSettings.Ports`、`NetworkSettings.Networks`、`Id`）
- Produces: `match_instance(instance: dict, containers: list[dict]) -> dict | None`
  - instance 形如 `{'ip': '192.168.0.30', 'port': 18029}`
  - **主键 = 宿主发布端口（HostPort）匹配**；兜底 = 容器 bridge IP 匹配；都不中返回 `None`

- [ ] **Step 1: 写失败测试**

`tests/test_instance_match.py`：
```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_instance_match.py -q`
Expected: FAIL（无 `services.instance_match`）

- [ ] **Step 3: 实现**

`services/instance_match.py`：
```python
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_instance_match.py -q`
Expected: PASS（4 passed）

- [ ] **Step 5: 提交**

```bash
git add services/instance_match.py tests/test_instance_match.py
git commit -m "feat(agent): 增加实例对号纯函数(宿主端口优先,bridge IP 兜底)"
```

---

### Task 5: nacos_client 查健康实例（可选鉴权）

**Files:**
- Create: `service-agent/services/nacos_client.py`
- Test: `service-agent/tests/test_nacos_client.py`

**Interfaces:**
- Consumes: `config.NACOS_*`、`http_client.get_json`
- Produces: `list_healthy_instances(service_name: str) -> list[dict]`，元素 `{'ip', 'port'}`；仅返回 `healthy and enabled` 的实例；`NACOS_SERVER` 未配抛 RuntimeError；URL 带 `NACOS_CONTEXT_PATH` 前缀；`NACOS_USERNAME` 非空时先登录拿 `accessToken`。

- [ ] **Step 1: 写失败测试**

`tests/test_nacos_client.py`：
```python
import os
os.environ.setdefault("WS_URL", "ws://test")    # config import 期会校验这两个 env,本机无 .env 时不设会 sys.exit
os.environ.setdefault("AGENT_KEY", "test-key")
import pytest
from services import nacos_client, http_client
import config

def test_requires_server(monkeypatch):
    monkeypatch.setattr(config, "NACOS_SERVER", "")
    with pytest.raises(RuntimeError):
        nacos_client.list_healthy_instances("svc")

def test_filters_unhealthy_and_builds_url(monkeypatch):
    monkeypatch.setattr(config, "NACOS_SERVER", "1.2.3.4:8848")
    monkeypatch.setattr(config, "NACOS_CONTEXT_PATH", "/nacos")
    monkeypatch.setattr(config, "NACOS_NAMESPACE", "dev")
    monkeypatch.setattr(config, "NACOS_GROUP", "DEFAULT_GROUP")
    monkeypatch.setattr(config, "NACOS_USERNAME", "")
    captured = {}
    def fake_get_json(url, params=None, timeout=10):
        captured["url"] = url
        captured["params"] = params
        return {"hosts": [
            {"ip": "10.0.0.1", "port": 18029, "healthy": True, "enabled": True},
            {"ip": "10.0.0.2", "port": 18030, "healthy": False, "enabled": True},
            {"ip": "10.0.0.3", "port": 18031, "healthy": True, "enabled": False},
        ]}
    monkeypatch.setattr(http_client, "get_json", fake_get_json)
    out = nacos_client.list_healthy_instances("memory-share")
    assert out == [{"ip": "10.0.0.1", "port": 18029}]
    assert captured["url"] == "http://1.2.3.4:8848/nacos/v1/ns/instance/list"
    assert captured["params"]["serviceName"] == "memory-share"
    assert captured["params"]["namespaceId"] == "dev"

def test_login_when_username_set(monkeypatch):
    monkeypatch.setattr(config, "NACOS_SERVER", "1.2.3.4:8848")
    monkeypatch.setattr(config, "NACOS_CONTEXT_PATH", "/nacos")
    monkeypatch.setattr(config, "NACOS_NAMESPACE", "")
    monkeypatch.setattr(config, "NACOS_GROUP", "DEFAULT_GROUP")
    monkeypatch.setattr(config, "NACOS_USERNAME", "nacos")
    monkeypatch.setattr(config, "NACOS_PASSWORD", "pw")
    monkeypatch.setattr(nacos_client, "_login", lambda: "tok-123")
    def fake_get_json(url, params=None, timeout=10):
        assert params["accessToken"] == "tok-123"
        return {"hosts": []}
    monkeypatch.setattr(http_client, "get_json", fake_get_json)
    assert nacos_client.list_healthy_instances("svc") == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_nacos_client.py -q`
Expected: FAIL（无 `services.nacos_client`）

- [ ] **Step 3: 实现**

`services/nacos_client.py`：
```python
import requests

import config
from services import http_client


def _login():
    base = f"http://{config.NACOS_SERVER}{config.NACOS_CONTEXT_PATH}"
    resp = requests.post(
        f"{base}/v1/auth/login",
        data={"username": config.NACOS_USERNAME, "password": config.NACOS_PASSWORD},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get("accessToken")


def list_healthy_instances(service_name):
    if not config.NACOS_SERVER:
        raise RuntimeError("NACOS_SERVER 未配置，无法发现实例")
    base = f"http://{config.NACOS_SERVER}{config.NACOS_CONTEXT_PATH}"
    params = {"serviceName": service_name, "groupName": config.NACOS_GROUP}
    if config.NACOS_NAMESPACE:
        params["namespaceId"] = config.NACOS_NAMESPACE
    if config.NACOS_USERNAME:
        params["accessToken"] = _login()
    data = http_client.get_json(f"{base}/v1/ns/instance/list", params=params)
    hosts = data.get("hosts") or []
    return [
        {"ip": h["ip"], "port": h["port"]}
        for h in hosts
        if h.get("healthy") and h.get("enabled")
    ]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_nacos_client.py -q`
Expected: PASS（3 passed）

- [ ] **Step 5: 提交**

```bash
git add services/nacos_client.py tests/test_nacos_client.py
git commit -m "feat(agent): 增加 nacos 健康实例查询(带 /nacos 前缀+可选鉴权)"
```

---

### Task 6: handle_list_instances 命令

**Files:**
- Create: `service-agent/core/rolling.py`（本任务先写 list 部分）
- Test: `service-agent/tests/test_rolling.py`

**Interfaces:**
- Consumes: `nacos_client.list_healthy_instances`、`docker_cli.list_running_containers`、`instance_match.match_instance`、`handlers.send_message`
- Produces: `handle_list_instances(ws, data)`，回 `{type:'list-instances-result', requestId, status, instances:[{address,containerId,healthy,matched}], error?}`

- [ ] **Step 1: 写失败测试**

`tests/test_rolling.py`：
```python
import os
os.environ.setdefault("WS_URL", "ws://test")    # core.rolling→services.nacos_client→config,import 期会校验这两个 env
os.environ.setdefault("AGENT_KEY", "test-key")
import json
from core import rolling
from services import nacos_client, docker_cli

class FakeWS:
    def __init__(self):
        self.sent = []
    def send(self, payload):
        self.sent.append(json.loads(payload))

def test_list_instances_success(monkeypatch):
    monkeypatch.setattr(nacos_client, "list_healthy_instances",
                        lambda s: [{"ip": "192.168.0.30", "port": 18029}])
    monkeypatch.setattr(docker_cli, "list_running_containers",
                        lambda: [{"Id": "abcdef1234567890", "NetworkSettings":
                                  {"Ports": {"80/tcp": [{"HostPort": "18029"}]}, "Networks": {}}}])
    ws = FakeWS()
    rolling.handle_list_instances(ws, {"requestId": "r1", "serviceName": "memory-share"})
    msg = ws.sent[-1]
    assert msg["type"] == "list-instances-result" and msg["status"] == "success"
    assert msg["instances"] == [{"address": "192.168.0.30:18029",
                                 "containerId": "abcdef123456", "healthy": True, "matched": True}]

def test_list_instances_unmatched_flagged(monkeypatch):
    monkeypatch.setattr(nacos_client, "list_healthy_instances",
                        lambda s: [{"ip": "10.9.9.9", "port": 9999}])
    monkeypatch.setattr(docker_cli, "list_running_containers", lambda: [])
    ws = FakeWS()
    rolling.handle_list_instances(ws, {"requestId": "r1", "serviceName": "svc"})
    inst = ws.sent[-1]["instances"][0]
    assert inst["matched"] is False and inst["containerId"] is None

def test_list_instances_failure(monkeypatch):
    def boom(s):
        raise RuntimeError("nacos down")
    monkeypatch.setattr(nacos_client, "list_healthy_instances", boom)
    ws = FakeWS()
    rolling.handle_list_instances(ws, {"requestId": "r1", "serviceName": "svc"})
    assert ws.sent[-1]["status"] == "failed" and "nacos down" in ws.sent[-1]["error"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_rolling.py -q`
Expected: FAIL（无 `core.rolling`）

- [ ] **Step 3: 实现**

`core/rolling.py`（首段）：
```python
import logging

from core.handlers import send_message
from services import docker_cli, nacos_client
from services.instance_match import match_instance

logger = logging.getLogger(__name__)


def handle_list_instances(ws, data):
    request_id = data.get("requestId")
    service_name = data.get("serviceName")
    try:
        instances = nacos_client.list_healthy_instances(service_name)
        containers = docker_cli.list_running_containers()
        result = []
        for inst in instances:
            container = match_instance(inst, containers)
            result.append({
                "address": f"{inst['ip']}:{inst['port']}",
                "containerId": container["Id"][:12] if container else None,
                "healthy": True,
                "matched": container is not None,
            })
        send_message(ws, {"type": "list-instances-result", "requestId": request_id,
                          "status": "success", "instances": result})
    except Exception as exc:
        logger.error(f"list-instances 失败: {exc}")
        send_message(ws, {"type": "list-instances-result", "requestId": request_id,
                          "status": "failed", "error": str(exc)})
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_rolling.py -q`
Expected: PASS（3 passed）

- [ ] **Step 5: 提交**

```bash
git add core/rolling.py tests/test_rolling.py
git commit -m "feat(agent): 增加 list-instances 命令(本地 nacos+docker 对号)"
```

---

### Task 7: handle_graceful_restart 命令

**Files:**
- Modify: `service-agent/core/rolling.py`（追加 graceful-restart）
- Test: `service-agent/tests/test_rolling.py`（追加）

**Interfaces:**
- Consumes: `http_client.post`/`get_status`、`docker_cli.restart_container`、`time.sleep`、`handlers.send_message`
- Produces: `handle_graceful_restart(ws, data)`，回 `{type:'graceful-restart-result', requestId, status, error?}`（**不回 ack**）。顺序：POST `{healthBaseUrl}/api/k8s/shutdown` → `docker restart <containerId>` → 轮询 `{healthBaseUrl}/api/health/ready==200`（`readyTimeoutSec`）→ `sleep settleSec`。

- [ ] **Step 1: 写失败测试**

`tests/test_rolling.py` 追加：
```python
import time
from services import http_client

def test_graceful_restart_success(monkeypatch):
    monkeypatch.setattr(http_client, "post", lambda url, timeout=60: (200, "ok"))
    monkeypatch.setattr(docker_cli, "restart_container", lambda cid, timeout=120: (True, "ok"))
    monkeypatch.setattr(http_client, "get_status", lambda url, timeout=5: 200)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    ws = FakeWS()
    rolling.handle_graceful_restart(ws, {"requestId": "g1", "containerId": "abc",
        "healthBaseUrl": "http://192.168.0.30:18029", "settleSec": 1,
        "shutdownTimeoutSec": 60, "readyTimeoutSec": 10})
    assert ws.sent == [{"type": "graceful-restart-result", "requestId": "g1", "status": "success"}]

def test_graceful_restart_shutdown_fail(monkeypatch):
    monkeypatch.setattr(http_client, "post", lambda url, timeout=60: (500, "err"))
    ws = FakeWS()
    rolling.handle_graceful_restart(ws, {"requestId": "g1", "containerId": "abc",
        "healthBaseUrl": "http://x", "settleSec": 0, "shutdownTimeoutSec": 60, "readyTimeoutSec": 10})
    assert ws.sent[-1]["status"] == "failed" and "shutdown" in ws.sent[-1]["error"]

def test_graceful_restart_not_ready(monkeypatch):
    monkeypatch.setattr(http_client, "post", lambda url, timeout=60: (200, "ok"))
    monkeypatch.setattr(docker_cli, "restart_container", lambda cid, timeout=120: (True, "ok"))
    monkeypatch.setattr(http_client, "get_status", lambda url, timeout=5: 503)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    # 让 _wait_ready 立即超时：把 time.time 固定推进
    # 多喂几个:_wait_ready 用 3 次,失败分支 logger.error→LogRecord.__init__ 还会再调 time.time()
    seq = iter([1000.0, 1000.0, 2000.0, 2000.0, 2000.0])
    monkeypatch.setattr(time, "time", lambda: next(seq))
    ws = FakeWS()
    rolling.handle_graceful_restart(ws, {"requestId": "g1", "containerId": "abc",
        "healthBaseUrl": "http://x", "settleSec": 0, "shutdownTimeoutSec": 60, "readyTimeoutSec": 1})
    assert ws.sent[-1]["status"] == "failed" and "ready" in ws.sent[-1]["error"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_rolling.py -q`
Expected: FAIL（`handle_graceful_restart` 未定义）

- [ ] **Step 3: 实现**

`core/rolling.py` 追加（顶部 import 补 `import time` 和 `from services import http_client`）：
```python
import time

from services import http_client


def _wait_ready(url, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if http_client.get_status(url) == 200:
            return True
        time.sleep(3)
    return False


def handle_graceful_restart(ws, data):
    request_id = data.get("requestId")
    container_id = data.get("containerId")
    base = data.get("healthBaseUrl")
    settle = int(data.get("settleSec", 35))
    shutdown_timeout = int(data.get("shutdownTimeoutSec", 60))
    ready_timeout = int(data.get("readyTimeoutSec", 180))
    try:
        code, _text = http_client.post(f"{base}/api/k8s/shutdown", timeout=shutdown_timeout)
        if code != 200:
            raise RuntimeError(f"shutdown 返回 {code}")
        ok, out = docker_cli.restart_container(container_id)
        if not ok:
            raise RuntimeError(f"docker restart 失败: {out}")
        if not _wait_ready(f"{base}/api/health/ready", ready_timeout):
            raise RuntimeError("节点未在超时内 ready")
        time.sleep(settle)
        send_message(ws, {"type": "graceful-restart-result", "requestId": request_id, "status": "success"})
    except Exception as exc:
        logger.error(f"graceful-restart 失败: {exc}")
        send_message(ws, {"type": "graceful-restart-result", "requestId": request_id,
                          "status": "failed", "error": str(exc)})
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_rolling.py -q`
Expected: PASS（6 passed）

- [ ] **Step 5: 提交**

```bash
git add core/rolling.py tests/test_rolling.py
git commit -m "feat(agent): 增加 graceful-restart 命令(shutdown→restart→等就绪→settle)"
```

---

### Task 8: ws_client 路由接入两新命令

**Files:**
- Modify: `service-agent/core/ws_client.py:48-63`
- Test: `service-agent/tests/test_ws_client.py`（追加）

**Interfaces:**
- Consumes: `core.rolling.handle_list_instances` / `handle_graceful_restart`
- Produces: `_on_message` 对 `type in ('list-instances','graceful-restart')` 在 daemon 线程分发；现有分支不变。

- [ ] **Step 1: 写失败测试**

先看 `tests/test_ws_client.py` 现有 `_import_ws_client` 重载方式（设 WS_URL/AGENT_KEY 后 reload）。追加：
```python
def test_on_message_routes_rolling(monkeypatch):
    ws_client = _import_ws_client(monkeypatch)   # 复用现有 helper
    calls = []
    # ws_client 用 `from core.rolling import ...` 绑定了本地名,monkeypatch 必须打在 ws_client 上
    # (参照本文件既有 test_ws_client.py 打 `ws_client.dispatch` 的范式,打 core.rolling 改不到已绑定引用)
    monkeypatch.setattr(ws_client, "handle_list_instances", lambda ws, data: calls.append(("list", data)))
    monkeypatch.setattr(ws_client, "handle_graceful_restart", lambda ws, data: calls.append(("gr", data)))
    # 让线程同步执行，便于断言
    monkeypatch.setattr(ws_client.threading, "Thread",
                        lambda target, args, daemon: type("T", (), {"start": lambda self: target(*args)})())
    ws_client._on_message(None, '{"type":"list-instances","requestId":"r1","serviceName":"s"}')
    ws_client._on_message(None, '{"type":"graceful-restart","requestId":"g1","containerId":"c"}')
    assert [c[0] for c in calls] == ["list", "gr"]
```
> 注：若现有 `_import_ws_client` 签名不同，按该文件实际 helper 调整；目的是拿到已 reload 的 `ws_client` 模块。

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_ws_client.py::test_on_message_routes_rolling -q`
Expected: FAIL（消息未路由，calls 为空）

- [ ] **Step 3: 实现**

`core/ws_client.py` 顶部 import 追加：
```python
from core.rolling import handle_graceful_restart, handle_list_instances
```
在 `_on_message` 的 `elif msg_type == 'ping':` 分支**之前**插入：
```python
        elif msg_type == 'list-instances':
            threading.Thread(target=handle_list_instances, args=(ws, data), daemon=True).start()
        elif msg_type == 'graceful-restart':
            threading.Thread(target=handle_graceful_restart, args=(ws, data), daemon=True).start()
```

- [ ] **Step 4: 跑全量测试 + 覆盖率门禁**

Run: `pytest --cov=agent --cov=config --cov=core --cov=services --cov-report=term-missing --cov-fail-under=97 -q`
Expected: PASS 且覆盖率 ≥97%（若某新模块行未覆盖，按 term-missing 提示补测试用例后再提交）

- [ ] **Step 5: 提交**

```bash
git add core/ws_client.py tests/test_ws_client.py
git commit -m "feat(agent): _on_message 接入 list-instances/graceful-restart 路由"
```

---

## 联调验证（在 49 测试床，可选但建议）

计划 1 完成后，可不依赖 hub 直接对 agent 验证（需把新镜像部到 49 的 cnp-dev-agent，或本地起 agent 连一个临时 ws server 模拟 hub 下发）。最简验证：构造一条 `graceful-restart` WS 消息发给 agent，观测它对 memory-share-1 完成"shutdown→restart→ready"，且 `non-200` 仍为 0（沿用 spec §9 压测）。完整端到端在计划 2（hub）完成后做。

## Self-Review（已核对）

- **Spec 覆盖**：list-instances（§4.2）✅；graceful-restart by containerId（§4.1/§4.2，已去 composeDir）✅；宿主端口优先对号 + 对不上号显式 `matched:false`（§4.2/H5）✅；nacos `/nacos` 前缀（L2）✅；nacos 可选鉴权 env（L3）✅；HTTP 全新引入（评审纠正）✅；结构化 result type（H4 agent 侧）✅。
- **跨任务类型一致**：`match_instance` 返回 container dict/None；`list_running_containers` 返回 inspect dict（含 `Id`/`NetworkSettings`）；rolling 用 `container["Id"][:12]`——一致。结果 type 名 `list-instances-result`/`graceful-restart-result` 与契约一致。
- **占位符**：无。每步含可运行代码与命令。
- **遗留给计划 2 的契约**：上方「协议契约」块即 hub 侧依赖的入站/出站消息形状。
