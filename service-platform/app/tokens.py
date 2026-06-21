"""per-namespace pull token：生成 / 哈希 / 校验（Task 5）。

节点拉包凭据 = 每命名空间一个 pull token。平台只存其 sha256 哈希
(`namespace.pull_token_hash`)，明文仅签发时一次性返回（show-once）。
纯函数，无外部依赖。
"""

import hashlib
import hmac
import secrets


def new_pull_token() -> tuple[str, str]:
    """生成新 pull token，返回 (明文, sha256 哈希)。明文仅此一次可得。"""
    plain = secrets.token_urlsafe(32)
    return plain, hash_token(plain)


def hash_token(plain: str) -> str:
    """sha256 hexdigest（64 位十六进制），确定性。"""
    return hashlib.sha256(plain.encode()).hexdigest()


def verify_token(plain: str, stored_hash: str) -> bool:
    """常量时间校验；明文或哈希为空一律 False（fail-closed，不抛异常）。"""
    if not plain or not stored_hash:
        return False
    return hmac.compare_digest(hash_token(plain), stored_hash)
