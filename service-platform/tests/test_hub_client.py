"""hub_client 单测(Task 4)。

平台用 admin token 调 service-hub 的命名空间(provision/rotate agent)端点:
- `provision_agent` → POST `{hub}/api/agents`,body `{agentId}`,header `X-Admin-Token`,读返回 `agentKey`。
- `rotate_agent_key` → POST `{hub}/api/agents/{id}/credentials/rotate`,读返回 `agentKey`。

测试纪律(评审 H8,权威范式 = `service-hub/tests/test_api.py:36`):`settings` 是
`@dataclass(frozen=True)`,**禁** `monkeypatch.setattr(<frozen 实例>, attr, val, raising=False)`
(底层仍 `FrozenInstanceError`)。本文件统一用 `monkeypatch.setattr(hc, "settings", SimpleNamespace(...))`
整体替换模块级引用(`hub_client.py` 是 `from app.config import settings`,可行,且无需 teardown)。

httpx 不真发请求:monkeypatch `hc.httpx.post` 为 fake,断言 URL/header/body/返回解析。
绑定约束:`X-Admin-Token` 仅服务端持有,断言其透传;敏感串(token / agentKey)不入日志。
"""

from __future__ import annotations

import types

import pytest

import app.hub_client as hc


class _Resp:
    """最小 httpx.Response 替身:只实现 hub_client 用到的 raise_for_status / json。

    `data` 可为任意对象(dict / list / 标量);`json_raises` 置真时 `json()` 抛 ValueError
    (模拟 200 但 body 非 JSON,如反代返 HTML 错误页 → httpx 内部 JSONDecodeError ⊂ ValueError)。
    """

    def __init__(self, data, *, status_ok: bool = True, json_raises: bool = False) -> None:
        self._d = data
        self._status_ok = status_ok
        self._json_raises = json_raises

    def raise_for_status(self) -> None:
        if not self._status_ok:
            raise hc.httpx.HTTPStatusError("boom", request=None, response=None)  # type: ignore[arg-type]

    def json(self):
        if self._json_raises:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._d


def _fake_settings(*, url: str = "http://hub:8080", token: str = "T") -> types.SimpleNamespace:
    return types.SimpleNamespace(service_hub_url=url, hub_admin_token=token)


def test_provision_agent(monkeypatch) -> None:
    calls: dict = {}

    def fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002 (对齐 httpx.post 签名)
        calls["url"] = url
        calls["headers"] = headers
        calls["json"] = json
        calls["timeout"] = timeout
        return _Resp({"agentKey": "k-123"})

    monkeypatch.setattr(hc, "settings", _fake_settings())
    monkeypatch.setattr(hc.httpx, "post", fake_post)

    assert hc.provision_agent("ns1") == "k-123"
    assert calls["url"].endswith("/api/agents")
    assert calls["url"] == "http://hub:8080/api/agents"
    assert calls["headers"]["X-Admin-Token"] == "T"
    assert calls["headers"]["Content-Type"] == "application/json"
    assert calls["json"] == {"agentId": "ns1"}
    assert calls["timeout"] == 15


def test_rotate_agent_key(monkeypatch) -> None:
    calls: dict = {}

    def fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        calls["url"] = url
        calls["headers"] = headers
        calls["json"] = json
        return _Resp({"agentKey": "k-rot"})

    monkeypatch.setattr(hc, "settings", _fake_settings())
    monkeypatch.setattr(hc.httpx, "post", fake_post)

    assert hc.rotate_agent_key("ns2") == "k-rot"
    assert calls["url"] == "http://hub:8080/api/agents/ns2/credentials/rotate"
    assert calls["headers"]["X-Admin-Token"] == "T"


def test_provision_agent_missing_hub_url_raises(monkeypatch) -> None:
    called = {"hit": False}

    def fake_post(*a, **k):
        called["hit"] = True
        return _Resp({"agentKey": "should-not-happen"})

    monkeypatch.setattr(hc, "settings", _fake_settings(url=""))
    monkeypatch.setattr(hc.httpx, "post", fake_post)

    with pytest.raises(hc.HubError):
        hc.provision_agent("ns1")
    assert called["hit"] is False  # 未配置 hub 时不应发起请求


def test_rotate_agent_key_missing_hub_url_raises(monkeypatch) -> None:
    called = {"hit": False}

    def fake_post(*a, **k):
        called["hit"] = True
        return _Resp({"agentKey": "x"})

    monkeypatch.setattr(hc, "settings", _fake_settings(url=""))
    monkeypatch.setattr(hc.httpx, "post", fake_post)

    with pytest.raises(hc.HubError):
        hc.rotate_agent_key("ns2")
    assert called["hit"] is False


def test_provision_agent_no_key_in_response_raises(monkeypatch) -> None:
    monkeypatch.setattr(hc, "settings", _fake_settings())
    monkeypatch.setattr(hc.httpx, "post", lambda *a, **k: _Resp({}))

    with pytest.raises(hc.HubError):
        hc.provision_agent("ns1")


def test_rotate_agent_key_no_key_in_response_raises(monkeypatch) -> None:
    monkeypatch.setattr(hc, "settings", _fake_settings())
    monkeypatch.setattr(hc.httpx, "post", lambda *a, **k: _Resp({}))

    with pytest.raises(hc.HubError):
        hc.rotate_agent_key("ns2")


# --- R3(复审):hub 200 但 body 非 dict / 非 JSON → 归一化为 HubError(不逃出窄 except) ---
#    raise_for_status() 通过(200)后 `r.json().get("agentKey")`:body 非 JSON → JSONDecodeError
#    (ValueError);body 是 JSON 数组/标量 → `.get` 抛 AttributeError。二者都不是 HubError/
#    httpx.HTTPError,会逃出路由层窄 except → 裸 500 + 孤儿 namespace。修复后统一抛 HubError。
#
#    变异验证:去掉 hub_client 的 json 解析归一化(直接 r.json().get(...)),非 dict 分支会抛
#    AttributeError、非 JSON 分支会抛 ValueError(均非 HubError)→ 下列用例转红。


@pytest.mark.parametrize("body", [[1, 2, 3], "agentKey", 5, 3.14, True, None, []])
def test_provision_agent_non_dict_body_raises_hub_error(monkeypatch, body) -> None:
    monkeypatch.setattr(hc, "settings", _fake_settings())
    monkeypatch.setattr(hc.httpx, "post", lambda *a, **k: _Resp(body))
    with pytest.raises(hc.HubError):
        hc.provision_agent("ns1")


@pytest.mark.parametrize("body", [[1, 2, 3], "agentKey", 5, None])
def test_rotate_agent_key_non_dict_body_raises_hub_error(monkeypatch, body) -> None:
    monkeypatch.setattr(hc, "settings", _fake_settings())
    monkeypatch.setattr(hc.httpx, "post", lambda *a, **k: _Resp(body))
    with pytest.raises(hc.HubError):
        hc.rotate_agent_key("ns2")


def test_provision_agent_non_json_body_raises_hub_error(monkeypatch) -> None:
    # 200 但 body 非 JSON(反代 HTML 错误页等)→ json() 抛 ValueError → 归一化 HubError。
    monkeypatch.setattr(hc, "settings", _fake_settings())
    monkeypatch.setattr(hc.httpx, "post", lambda *a, **k: _Resp(None, json_raises=True))
    with pytest.raises(hc.HubError):
        hc.provision_agent("ns1")


def test_rotate_agent_key_non_json_body_raises_hub_error(monkeypatch) -> None:
    monkeypatch.setattr(hc, "settings", _fake_settings())
    monkeypatch.setattr(hc.httpx, "post", lambda *a, **k: _Resp(None, json_raises=True))
    with pytest.raises(hc.HubError):
        hc.rotate_agent_key("ns2")


# --- R7(复审):rotate 的 quote() 第二道闸须有测试(删 quote 全绿=测试缝) ---
#    rotate_agent_key 把 agent_id 拼进 hub URL 路径段,必须 quote(safe='') 编码,否则含
#    `/` `..` 的 code 改变请求路径(存储型路径注入第二道闸,与 NamespaceIn 白名单纵深互补)。
#
#    变异验证:删掉 `quote(agent_id, safe='')`(直接拼 agent_id),URL 路径段会出现裸 `/` 与
#    `..` → 下列断言转红。


# --- Task 9b:list_agents / list_instances(节点页消费)单测 ---
#    list_agents → GET {hub}/api/agents,透传 X-Admin-Token,返回 hub 的 AgentSnapshot 列表。
#    list_instances → POST {hub}/api/agents/{quote(id)}/list-instances,body {serviceName},短超时。


def test_list_agents(monkeypatch) -> None:
    calls: dict = {}

    def fake_get(url, headers=None, timeout=None):
        calls["url"] = url
        calls["headers"] = headers
        calls["timeout"] = timeout
        return _Resp([{"agentId": "a1", "online": True}, {"agentId": "a2", "online": False}])

    monkeypatch.setattr(hc, "settings", _fake_settings())
    monkeypatch.setattr(hc.httpx, "get", fake_get)

    agents = hc.list_agents()
    assert [a["agentId"] for a in agents] == ["a1", "a2"]
    assert calls["url"] == "http://hub:8080/api/agents"
    assert calls["headers"]["X-Admin-Token"] == "T"
    assert calls["timeout"] == 15  # 列表用通用 _HUB_TIMEOUT


def test_list_agents_missing_hub_url_raises(monkeypatch) -> None:
    called = {"hit": False}

    def fake_get(*a, **k):
        called["hit"] = True
        return _Resp([])

    monkeypatch.setattr(hc, "settings", _fake_settings(url=""))
    monkeypatch.setattr(hc.httpx, "get", fake_get)

    with pytest.raises(hc.HubError):
        hc.list_agents()
    assert called["hit"] is False  # 未配置 hub 时不应发起请求


def test_list_agents_propagates_http_error(monkeypatch) -> None:
    # 非 2xx → raise_for_status 抛 HTTPStatusError(向上抛,路由层退化为空 map,不在此吞)。
    monkeypatch.setattr(hc, "settings", _fake_settings())
    monkeypatch.setattr(hc.httpx, "get", lambda *a, **k: _Resp([], status_ok=False))
    with pytest.raises(hc.httpx.HTTPStatusError):
        hc.list_agents()


def test_list_instances(monkeypatch) -> None:
    calls: dict = {}

    def fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        calls["url"] = url
        calls["headers"] = headers
        calls["json"] = json
        calls["timeout"] = timeout
        return _Resp({"status": "success", "instances": [{"healthy": True}]})

    monkeypatch.setattr(hc, "settings", _fake_settings())
    monkeypatch.setattr(hc.httpx, "post", fake_post)

    out = hc.list_instances("a1", "svc-nacos")
    assert out["status"] == "success"
    assert calls["url"] == "http://hub:8080/api/agents/a1/list-instances"
    assert calls["headers"]["X-Admin-Token"] == "T"
    assert calls["json"] == {"serviceName": "svc-nacos"}
    assert calls["timeout"] == 5.0  # 短超时(默认),保证节点页响应


def test_list_instances_custom_timeout(monkeypatch) -> None:
    calls: dict = {}

    def fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        calls["timeout"] = timeout
        return _Resp({"status": "success", "instances": []})

    monkeypatch.setattr(hc, "settings", _fake_settings())
    monkeypatch.setattr(hc.httpx, "post", fake_post)

    hc.list_instances("a1", "svc", timeout=2.5)
    assert calls["timeout"] == 2.5


def test_list_instances_missing_hub_url_raises(monkeypatch) -> None:
    called = {"hit": False}

    def fake_post(*a, **k):
        called["hit"] = True
        return _Resp({"status": "success"})

    monkeypatch.setattr(hc, "settings", _fake_settings(url=""))
    monkeypatch.setattr(hc.httpx, "post", fake_post)

    with pytest.raises(hc.HubError):
        hc.list_instances("a1", "svc")
    assert called["hit"] is False


def test_list_instances_propagates_http_error(monkeypatch) -> None:
    # hub 返非 2xx(如 agent 离线 409 / 未应答 502)→ HTTPStatusError 向上抛,路由层标该行 degraded。
    monkeypatch.setattr(hc, "settings", _fake_settings())
    monkeypatch.setattr(hc.httpx, "post", lambda *a, **k: _Resp({}, status_ok=False))
    with pytest.raises(hc.httpx.HTTPStatusError):
        hc.list_instances("a1", "svc")


def test_list_instances_quotes_agent_id_in_url(monkeypatch) -> None:
    # 纵深防御第二道闸:agent_id 拼进路径段必须 quote(safe='')(同 rotate 的路径注入护栏)。
    # 变异验证:删掉 quote(直接拼 agent_id),路径段残留裸 / 与 .. → 下列断言转红。
    calls: dict = {}

    def fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        calls["url"] = url
        return _Resp({"status": "success", "instances": []})

    monkeypatch.setattr(hc, "settings", _fake_settings())
    monkeypatch.setattr(hc.httpx, "post", fake_post)

    hc.list_instances("a/b/../c", "svc")
    url = calls["url"]
    assert "/api/agents/" in url and "/list-instances" in url, url
    seg = url.split("/api/agents/", 1)[1].rsplit("/list-instances", 1)[0]
    assert "/" not in seg, f"agent_id 未编码进 URL 路径段(残留裸 /,可路径注入): {seg!r}"
    assert "%2F" in seg.upper()  # `/` → %2F(证明编码确实发生)


def test_rotate_agent_key_quotes_agent_id_in_url(monkeypatch) -> None:
    calls: dict = {}

    def fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        calls["url"] = url
        return _Resp({"agentKey": "k"})

    monkeypatch.setattr(hc, "settings", _fake_settings())
    monkeypatch.setattr(hc.httpx, "post", fake_post)

    hc.rotate_agent_key("a/b/../c")
    url = calls["url"]
    # 取出 `/api/agents/<seg>/credentials/rotate` 中的 <seg>(agent_id 编码后路径段)。
    assert "/api/agents/" in url and "/credentials/rotate" in url, url
    seg = url.split("/api/agents/", 1)[1].rsplit("/credentials/rotate", 1)[0]
    # 核心安全不变式:agent_id 内的 `/` 必须被编码成 %2F,使整串退化为**单个**路径段——
    # 不含裸 `/`,也就不存在真正的 `..` 穿越段(`..` 字面残留但被编码 `/` 夹住,无法改变路径)。
    assert "/" not in seg, f"agent_id 未编码进 URL 路径段(残留裸 /,可路径注入): {seg!r}"
    assert "/../" not in url.split("/api/agents/", 1)[1], "残留真正的 /../ 穿越段"
    assert "%2F" in seg.upper()  # `/` → %2F(证明编码确实发生)
