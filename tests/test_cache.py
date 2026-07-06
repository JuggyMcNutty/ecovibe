"""Tests for SimpleCache TTL behaviour."""
import time

from app.services.cache import SimpleCache


def test_cache_set_get():
    c = SimpleCache(ttl=10)
    c.set("k", {"v": 1})
    assert c.get("k") == {"v": 1}


def test_cache_miss():
    c = SimpleCache(ttl=10)
    assert c.get("missing") is None


def test_cache_ttl_expiry():
    c = SimpleCache(ttl=1)
    c.set("k", "v")
    assert c.get("k") == "v"
    time.sleep(1.1)
    assert c.get("k") is None


def test_cache_overwrite():
    c = SimpleCache(ttl=10)
    c.set("k", "v1")
    c.set("k", "v2")
    assert c.get("k") == "v2"


def test_cache_clear():
    c = SimpleCache(ttl=10)
    c.set("k", "v")
    c.clear()
    assert c.get("k") is None
