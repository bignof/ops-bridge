"""鉴权端到端测试(Task 3)。

人类会话 = `Authorization: Bearer <JWT>`(不用 cookie),单 admin、env 凭据。
凭据由 conftest 的 `os.environ.setdefault` 兜底:admin / admin-pw;
jwt_secret 为 ≥32 字符的测试密钥。一律经 `client` fixture(隔离临时库)。
"""


def test_login_ok_and_me(client) -> None:
    r = client.post("/auth/login", json={"username": "admin", "password": "admin-pw"})
    assert r.status_code == 200
    tok = r.json()["token"]
    assert tok
    r2 = client.get("/auth/me", headers={"Authorization": f"Bearer {tok}"})
    assert r2.status_code == 200 and r2.json()["user"] == "admin"


def test_login_wrong_password_401(client) -> None:
    assert client.post("/auth/login", json={"username": "admin", "password": "x"}).status_code == 401


def test_me_without_token_401(client) -> None:
    assert client.get("/auth/me").status_code == 401
