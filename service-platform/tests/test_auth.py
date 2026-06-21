"""鉴权端到端测试(Task 3 + P1a 评审 A1/A2/A7 测试补强)。

人类会话 = `Authorization: Bearer <JWT>`(不用 cookie),单 admin、env 凭据。
凭据由 conftest 的 `os.environ.setdefault` 兜底:admin / admin-pw;
jwt_secret 为 ≥32 字符的测试密钥。一律经 `client` fixture(隔离临时库)。

P1a 评审补强(原仅测「登录成功 / 错密码 / 无 token」,JWT 验签/过期/轮换零测试、
verify_login 错用户名/未配凭据零测试 —— 均被变异验证证为假绿):
- A1:JWT **过期 → 401**、**错 secret 签的结构完整 token → 401(验签生效)**、
  **轮换 jwt_secret 后旧 token 立即 401(kill-switch)**。
- A7:**错用户名(密码对)→ 401**、**置空 admin_user/password 后任何登录 → 401**
  (fail-closed,覆盖 auth.py「未配凭据」分支)。
- A2:**非 ASCII 用户名/密码 → 401 而非 500**(`hmac.compare_digest` bytes 版兜底)。

frozen settings 字段一律 `object.__setattr__` 改 + finally 还原(同 conftest 手法);
settings 是 `app.config` 的模块级单例,auth.py 的 issue_token/require_session/verify_login
均读它,故改它即生效。
"""

from __future__ import annotations

import time

import jwt

from app.auth import issue_token
from app.config import settings


# 另一把与 settings.jwt_secret 不同、但同样 ≥32 字符的密钥(用于「错 secret」「轮换」用例)。
_OTHER_SECRET = "another-test-secret-which-is-also-long-enough-9876543210"


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


# ── A7:错用户名(密码对)→ 401(变异 `return pw_ok` 忽略用户名时,本用例转红) ──
def test_login_wrong_username_401(client) -> None:
    r = client.post("/auth/login", json={"username": "not-admin", "password": "admin-pw"})
    assert r.status_code == 401, r.text


# ── A7:未配凭据(admin_user/password 任一为空)→ 任何登录 fail-closed 401 ──
def test_login_unconfigured_credentials_fail_closed_401(client) -> None:
    old_user = settings.admin_user
    old_pw = settings.admin_password
    object.__setattr__(settings, "admin_user", "")
    object.__setattr__(settings, "admin_password", "")
    try:
        # 即便「凭据」与置空后的空串相等,也必须被 fail-closed 分支拒绝。
        r_empty = client.post("/auth/login", json={"username": "", "password": ""})
        assert r_empty.status_code == 401, r_empty.text
        r_any = client.post("/auth/login", json={"username": "admin", "password": "admin-pw"})
        assert r_any.status_code == 401, r_any.text
    finally:
        object.__setattr__(settings, "admin_user", old_user)
        object.__setattr__(settings, "admin_password", old_pw)


# ── A2:非 ASCII 用户名/密码 → 401(而非 hmac.compare_digest 抛 TypeError → 500) ──
def test_login_non_ascii_credentials_401_not_500(client) -> None:
    # 非 ASCII 用户名(密码随意):bytes 版 compare_digest 正常比对 → 不匹配 → 401。
    r_user = client.post("/auth/login", json={"username": "管理员", "password": "admin-pw"})
    assert r_user.status_code == 401, r_user.text
    # 非 ASCII 密码:同理 → 401。
    r_pw = client.post("/auth/login", json={"username": "admin", "password": "密码"})
    assert r_pw.status_code == 401, r_pw.text
    # 两侧都非 ASCII → 401。
    r_both = client.post("/auth/login", json={"username": "用户", "password": "密码"})
    assert r_both.status_code == 401, r_both.text


# ── A2:非 ASCII 凭据**恰好正确**时仍应能登录(证明修复未把合法非 ASCII 一律拒死) ──
def test_login_non_ascii_credentials_correct_200(client) -> None:
    old_user = settings.admin_user
    old_pw = settings.admin_password
    object.__setattr__(settings, "admin_user", "管理员")
    object.__setattr__(settings, "admin_password", "密码强度①")
    try:
        r = client.post("/auth/login", json={"username": "管理员", "password": "密码强度①"})
        assert r.status_code == 200, r.text
        assert r.json().get("token")
    finally:
        object.__setattr__(settings, "admin_user", old_user)
        object.__setattr__(settings, "admin_password", old_pw)


# ── A1:过期 token → 401(变异 `verify_exp=False` 时,本用例转红) ──
def test_expired_token_rejected_401(client) -> None:
    old_ttl = settings.jwt_ttl_seconds
    object.__setattr__(settings, "jwt_ttl_seconds", -10)  # 临时签发一个 exp 已过期的 token
    try:
        expired = issue_token("admin")
    finally:
        object.__setattr__(settings, "jwt_ttl_seconds", old_ttl)
    r = client.get("/auth/me", headers={"Authorization": f"Bearer {expired}"})
    assert r.status_code == 401, r.text


# ── A1:用**不同于** settings.jwt_secret 的密钥签的结构完整 token → 401(验签生效) ──
#    变异 `verify_signature=False` 时本用例转红(说明 decode 真的在验签)。
def test_token_signed_with_wrong_secret_rejected_401(client) -> None:
    now = int(time.time())
    payload = {"sub": "admin", "iat": now, "exp": now + 3600}  # 结构完整、未过期
    forged = jwt.encode(payload, _OTHER_SECRET, algorithm="HS256")
    assert forged != ""
    r = client.get("/auth/me", headers={"Authorization": f"Bearer {forged}"})
    assert r.status_code == 401, r.text


# ── A1:轮换 settings.jwt_secret 后,旧 token 立即 401(kill-switch / 密钥轮换) ──
def test_secret_rotation_invalidates_old_token_401(client) -> None:
    tok = _login_token(client)
    # 轮换前旧 token 可用(回归基线)。
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {tok}"}).status_code == 200

    old_secret = settings.jwt_secret
    object.__setattr__(settings, "jwt_secret", _OTHER_SECRET)
    try:
        r = client.get("/auth/me", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 401, r.text  # 旧密钥签的 token 用新密钥验签失败 → 401
    finally:
        object.__setattr__(settings, "jwt_secret", old_secret)


def _login_token(client) -> str:
    r = client.post("/auth/login", json={"username": "admin", "password": "admin-pw"})
    assert r.status_code == 200, r.text
    return r.json()["token"]
