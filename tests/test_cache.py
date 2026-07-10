import time

from mesh_graph.api.cache import TTLCache


def test_get_set_roundtrip():
    c = TTLCache(maxsize=100)
    c.set("k", b"hello", ttl=10)
    assert c.get("k") == b"hello"


def test_get_missing_key():
    c = TTLCache(maxsize=100)
    assert c.get("nope") is None


def test_expired_entry_returns_none():
    c = TTLCache(maxsize=100)
    c.set("k", b"data", ttl=0.001)
    time.sleep(0.01)
    assert c.get("k") is None


def test_expired_entry_removed_on_access():
    c = TTLCache(maxsize=100)
    c.set("k", b"data", ttl=0.001)
    time.sleep(0.01)
    c.get("k")
    assert len(c) == 0


def test_evict_drops_oldest_when_over_maxsize():
    c = TTLCache(maxsize=2)
    c.set("a", b"1", ttl=60)
    c.set("b", b"2", ttl=60)
    c.set("c", b"3", ttl=60)
    assert c.get("a") is None
    assert c.get("b") is not None
    assert c.get("c") is not None


def test_expired_entries_swept_before_eviction_count():
    c = TTLCache(maxsize=2)
    c.set("a", b"1", ttl=0.001)
    c.set("b", b"2", ttl=60)
    time.sleep(0.01)
    c.set("c", b"3", ttl=60)
    # "a" was swept as stale; only "b" and "c" remain
    assert len(c) == 2
    assert c.get("b") is not None
    assert c.get("c") is not None


def test_zero_ttl_immediately_expired():
    c = TTLCache(maxsize=100)
    c.set("k", b"data", ttl=0)
    assert c.get("k") is None


def test_clear_removes_all_entries():
    c = TTLCache(maxsize=100)
    c.set("a", b"1", ttl=60)
    c.set("b", b"2", ttl=60)
    c.clear()
    assert len(c) == 0
    assert c.get("a") is None
