"""Simple in-memory TTL cache for catalog responses."""
from datetime import datetime, timedelta
from typing import Any


class SimpleCache:
    """Key/value store with per-entry expiry."""

    def __init__(self, ttl: int = 300) -> None:
        self._cache: dict[str, tuple[Any, datetime]] = {}
        self._ttl = ttl

    def get(self, key: str) -> Any:
        """Return the value if fresh, else None (and evict the stale entry)."""
        if key in self._cache:
            value, expires_at = self._cache[key]
            if datetime.now() < expires_at:
                return value
            del self._cache[key]
        return None

    def set(self, key: str, value: Any) -> None:
        expires_at = datetime.now() + timedelta(seconds=self._ttl)
        self._cache[key] = (value, expires_at)

    def set_ttl(self, ttl: int) -> None:
        """Change the TTL for entries written from now on.

        Existing entries keep the expiry computed when they were set;
        callers that need the new TTL to apply immediately should also
        call ``clear()`` (the settings PUT hook does).
        """
        self._ttl = ttl

    def clear(self) -> None:
        self._cache.clear()


_cache: SimpleCache | None = None


def get_cache(ttl: int | None = None) -> SimpleCache:
    """Shared singleton. A differing ``ttl`` updates the cache's TTL for
    subsequent writes (it used to be silently frozen at first creation,
    which made OVH_CACHE_TTL changes a no-op after the first fetch)."""
    global _cache
    if _cache is None:
        _cache = SimpleCache(ttl=ttl if ttl is not None else 300)
    elif ttl is not None and ttl != _cache._ttl:
        _cache.set_ttl(ttl)
    return _cache
