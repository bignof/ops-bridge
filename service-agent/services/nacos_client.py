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
