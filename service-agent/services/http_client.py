import requests

# 统一禁止跟随 30x 跳转，防止 SSRF 经重定向二次放大到非预期地址（H1）


def get_json(url, params=None, timeout=10):
    resp = requests.get(url, params=params, timeout=timeout, allow_redirects=False)
    resp.raise_for_status()
    return resp.json()


def get_status(url, timeout=5):
    try:
        resp = requests.get(url, timeout=timeout, allow_redirects=False)
        return resp.status_code
    except requests.RequestException:
        return 0


def post(url, timeout=60):
    resp = requests.post(url, timeout=timeout, allow_redirects=False)
    return resp.status_code, resp.text
