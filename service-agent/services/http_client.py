import requests

# 统一禁止跟随 30x 跳转，防止 SSRF 经重定向二次放大到非预期地址（H1）


def get_json(url, params=None, timeout=10, headers=None):
    # headers 供回源带 Authorization: Bearer <pull-token>（P1-3）；None 时等同未设。
    resp = requests.get(url, params=params, timeout=timeout, allow_redirects=False, headers=headers)
    resp.raise_for_status()
    return resp.json()


def get_status(url, timeout=5):
    try:
        resp = requests.get(url, timeout=timeout, allow_redirects=False)
        return resp.status_code
    except requests.RequestException:
        return 0


def post(url, timeout=60, headers=None):
    # headers=None 时 requests 行为与不传一致；用于 /api/k8s/shutdown 透传凭据头（T4a）
    resp = requests.post(url, timeout=timeout, allow_redirects=False, headers=headers)
    return resp.status_code, resp.text


def download(url, dest_path, headers=None, timeout=60, chunk_size=1024 * 256):
    """流式 GET 下载到 dest_path（大 .tgz 不全载内存）。

    用于 P1-3 回源插件包。同样禁跟随重定向（H1，防 SSRF 经 30x 二次放大）。
    4xx/5xx 经 raise_for_status 冒泡;调用方（plugin_cache.get_or_fetch）据此判失败、清临时文件。
    """
    with requests.get(
        url, headers=headers, timeout=timeout, allow_redirects=False, stream=True
    ) as resp:
        resp.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=chunk_size):
                if chunk:
                    f.write(chunk)
