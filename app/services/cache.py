"""Tiny in-memory TTL cache used by `OVHService.fetch_catalog`.

Not thread-safe by itself, but safe under FastAPI's single-threaded asyncio
event loop because there is no `await` between the read check and the delete.
If sync OVH calls ever move into a thread executor, add a lock here.
"""
from datetime import datetime, timedelta
from typing import Any


class SimpleCache:
    """Key→value store with per-entry expiry.

    Each entry stores `(value, expires_at)`. `get` returns the value if the
    entry exists and has not expired, otherwise removes it and returns None.
    """

    def __init__(self, ttl: int = 300) -> None:
        self._cache: dict[str, tuple[Any, datetime]] = {}
        self._ttl = ttl

    def get(self, key: str) -> Any:
        """Return the cached value if fresh, else None (and evict the stale entry)."""
        if key in self._cache:
            value, expires_at = self._cache[key]
            if datetime.now() < expires_at:
                return value
            # Stale — remove to avoid unbounded growth of expired entries.
            del self._cache[key]
        return None

    def set(self, key: str, value: Any) -> None:
        """Insert/overwrite a cache entry with a fresh expiry window."""
        expires_at = datetime.now() + timedelta(seconds=self._ttl)
        self._cache[key] = (value, expires_at)

    def clear(self) -> None:
        """Drop all entries."""
        self._cache.clear()


# Module-level singleton. The TTL is fixed on first creation; subsequent
# callers receive the same instance regardless of the `ttl` argument.
_cache: SimpleCache | None = None


def get_cache(ttl: int = 300) -> SimpleCache:
    """Return the shared SimpleCache singleton, creating it on first use."""
    global _cache
    if _cache is None:
        _cache = SimpleCache(ttl=ttl)
    return _cache
