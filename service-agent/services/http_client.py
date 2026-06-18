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
