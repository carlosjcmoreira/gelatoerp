"""
flask_app/cache.py — SimpleCache façade over db.cache.

Provides a SimpleCache class with get/set/delete/delete_prefix methods
(thread-safe, in-process TTL cache), backed by the same store as db.cache
so both packages share one per-worker cache.

Usage:
    from flask_app.cache import cache

    value = cache.get('my_key')
    if value is None:
        value = expensive_query()
        cache.set('my_key', value, ttl=300)

    cache.delete('my_key')
    cache.delete_prefix('my_prefix')
"""

from db.cache import (
    cache_get,
    cache_set,
    invalidate,
    invalidate_prefix,
    clear_all,
    ttl_cache,
    ttl_cache_args,
)


class SimpleCache:
    """Dict + TTL in-memory cache, thread-safe via a shared lock."""

    def get(self, key: str):
        """Return cached value, or None if missing/expired."""
        return cache_get(key)

    def set(self, key: str, value, ttl: int = 300) -> None:
        """Store value with a TTL (seconds)."""
        cache_set(key, value, ttl)

    def delete(self, key: str) -> None:
        """Remove a single cache entry."""
        invalidate(key)

    def delete_prefix(self, prefix: str) -> None:
        """Remove all entries whose key starts with prefix."""
        invalidate_prefix(prefix)

    def clear(self) -> None:
        """Flush the entire cache."""
        clear_all()


cache = SimpleCache()

__all__ = ['SimpleCache', 'cache', 'ttl_cache', 'ttl_cache_args', 'invalidate', 'invalidate_prefix']
