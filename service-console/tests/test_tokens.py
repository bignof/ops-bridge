"""per-namespace pull token 的生成 / 哈希 / 校验测试(Task 5)。

纯函数,无外部依赖,不经 `client` fixture。覆盖:往返(verify 正确→True、
错→False)、明文长度 ≥32、哈希 ≠ 明文、空值短路→False、哈希确定性。
"""

from app import tokens


def test_pull_token_roundtrip() -> None:
    plain, h = tokens.new_pull_token()
    assert plain and len(plain) >= 32 and h != plain
    assert tokens.verify_token(plain, h) is True
    assert tokens.verify_token("wrong", h) is False


def test_hash_token_deterministic_and_hex() -> None:
    """同一明文哈希稳定;sha256 hexdigest 恒为 64 位十六进制。"""
    h1 = tokens.hash_token("abc")
    h2 = tokens.hash_token("abc")
    assert h1 == h2
    assert len(h1) == 64 and all(c in "0123456789abcdef" for c in h1)


def test_new_pull_token_unique() -> None:
    """两次生成的明文与哈希都应不同(secrets 随机)。"""
    p1, h1 = tokens.new_pull_token()
    p2, h2 = tokens.new_pull_token()
    assert p1 != p2 and h1 != h2


def test_verify_token_empty_inputs_false() -> None:
    """明文或哈希为空一律 False(fail-closed),不抛异常。"""
    _, h = tokens.new_pull_token()
    assert tokens.verify_token("", h) is False
    assert tokens.verify_token("anything", "") is False
    assert tokens.verify_token("", "") is False
