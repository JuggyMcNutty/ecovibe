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


def test_get_cache_updates_ttl_on_later_call():
    """get_cache(ttl) must honour a changed TTL — it used to freeze the
    first value forever, making runtime TTL changes a silent no-op."""
    import app.services.cache as cache_mod

    cache_mod._cache = None
    c1 = cache_mod.get_cache(ttl=300)
    assert c1._ttl == 300
    c2 = cache_mod.get_cache(ttl=60)
    assert c2 is c1          # still the singleton
    assert c1._ttl == 60     # but the TTL followed

    # No-arg calls leave the TTL untouched.
    cache_mod.get_cache()
    assert c1._ttl == 60
    cache_mod._cache = None


def test_set_ttl_affects_only_new_entries():
    c = SimpleCache(ttl=100)
    c.set("old", "v")
    c.set_ttl(1)
    c.set("new", "v")
    time.sleep(1.1)
    assert c.get("old") == "v"   # kept its original 100s expiry
    assert c.get("new") is None  # expired under the new 1s TTL
