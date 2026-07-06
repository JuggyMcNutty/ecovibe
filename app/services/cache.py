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

    def clear(self) -> None:
        self._cache.clear()


_cache: SimpleCache | None = None


def get_cache(ttl: int = 300) -> SimpleCache:
    """Shared singleton. TTL is fixed on first creation."""
    global _cache
    if _cache is None:
        _cache = SimpleCache(ttl=ttl)
    return _cache
