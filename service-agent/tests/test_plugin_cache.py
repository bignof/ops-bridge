import os

# plugin_cache 会 import config,而 config 在缺 WS_URL/AGENT_KEY 时会 sys.exit(与 test_handlers 同套路)。
os.environ.setdefault("WS_URL", "ws://test")
os.environ.setdefault("AGENT_KEY", "test-key")

import pytest

from services import plugin_cache


@pytest.fixture(autouse=True)
def cache_env(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_cache.config, "PLUGIN_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(plugin_cache.config, "PLUGIN_CACHE_MAX_BYTES", 10_000)
    plugin_cache._locks.clear()  # 隔离用例间的模块级锁
    yield


def _writer(data: bytes):
    def fn(tmp: str) -> None:
        with open(tmp, "wb") as f:
            f.write(data)
    return fn


def test_safe_id_rejects_traversal_and_bad_chars():
    for bad in ["../etc", "a/b", "a\\b", "..", "", "a" * 200, "a b"]:
        with pytest.raises(ValueError):
            plugin_cache.cache_path(bad)


def test_safe_id_accepts_normal_ids():
    for ok in ["123", "uuid-abc_DEF.9", "42"]:
        assert plugin_cache.cache_path(ok).endswith(f"{ok}.tgz")


def test_miss_then_hit_only_fetches_once():
    calls = {"n": 0}

    def fetch(tmp):
        calls["n"] += 1
        with open(tmp, "wb") as f:
            f.write(b"pkg-bytes")

    p1 = plugin_cache.get_or_fetch("123", fetch)
    assert os.path.isfile(p1)
    assert calls["n"] == 1
    assert plugin_cache.is_cached("123")

    # 命中缓存,不再回源
    p2 = plugin_cache.get_or_fetch("123", fetch)
    assert p2 == p1
    assert calls["n"] == 1


def test_fetcher_empty_output_raises_and_not_cached():
    def bad(tmp):
        open(tmp, "wb").close()  # 空文件

    with pytest.raises(RuntimeError):
        plugin_cache.get_or_fetch("9", bad)
    assert not plugin_cache.is_cached("9")
    # 临时文件不残留
    assert os.listdir(plugin_cache.cache_dir()) == []


def test_fetcher_exception_propagates_and_cleans_tmp():
    def boom(tmp):
        with open(tmp, "wb") as f:
            f.write(b"partial")
        raise RuntimeError("network down")

    with pytest.raises(RuntimeError):
        plugin_cache.get_or_fetch("7", boom)
    assert not plugin_cache.is_cached("7")
    assert os.listdir(plugin_cache.cache_dir()) == []


def test_lru_evicts_oldest_over_cap(monkeypatch):
    monkeypatch.setattr(plugin_cache.config, "PLUGIN_CACHE_MAX_BYTES", 0)  # 写入期间不淘汰
    for aid in ["a", "b", "c"]:
        plugin_cache.get_or_fetch(aid, _writer(b"y" * 100))
    # 强制递增 mtime:a 最旧、c 最新
    for i, aid in enumerate(["a", "b", "c"]):
        ts = 1000 + i
        os.utime(plugin_cache.cache_path(aid), (ts, ts))

    monkeypatch.setattr(plugin_cache.config, "PLUGIN_CACHE_MAX_BYTES", 250)  # 300 > 250 → 淘汰最旧
    plugin_cache._evict_if_needed()

    assert not plugin_cache.is_cached("a")
    assert plugin_cache.is_cached("b")
    assert plugin_cache.is_cached("c")


def test_evict_cap_zero_means_unlimited(monkeypatch):
    monkeypatch.setattr(plugin_cache.config, "PLUGIN_CACHE_MAX_BYTES", 0)
    for aid in ["a", "b", "c"]:
        plugin_cache.get_or_fetch(aid, _writer(b"z" * 1000))
    plugin_cache._evict_if_needed()
    assert all(plugin_cache.is_cached(x) for x in ["a", "b", "c"])
