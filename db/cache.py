"""
Simple per-process TTL cache for static / rarely-changing DB data.

Each gunicorn worker has its own cache (no shared memory between workers),
which is fine: cache hit rate is still high and avoids DB round-trips per request.

Usage:
    from db.cache import ttl_cache, invalidate

    @ttl_cache('sabores_list', ttl=300)
    def get_sabores_list(...):
        ...

    # After mutating underlying data:
    invalidate('sabores_list')
"""
import threading
import time
import functools
import logging

logger = logging.getLogger(__name__)

_store: dict = {}       # key -> (value, expiry_timestamp)
_lock = threading.Lock()


def ttl_cache(key: str, ttl: int = 300):
    """
    Decorator factory.  `key` is a logical cache key (shared across all
    calls to the decorated function regardless of arguments — use only for
    zero-argument or argument-independent functions).

    For argument-sensitive functions use `ttl_cache_args` below.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            now = time.monotonic()
            with _lock:
                entry = _store.get(key)
                if entry is not None and now < entry[1]:
                    return entry[0]
            result = fn(*args, **kwargs)
            with _lock:
                _store[key] = (result, now + ttl)
            return result
        wrapper._cache_key = key
        return wrapper
    return decorator


def ttl_cache_args(key_prefix: str, ttl: int = 300):
    """
    Like ttl_cache but incorporates function arguments into the cache key.
    Suitable for functions like get_sabores_list(apenas_eurokg=True/False).
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            full_key = f"{key_prefix}:{args}:{sorted(kwargs.items())}"
            now = time.monotonic()
            with _lock:
                entry = _store.get(full_key)
                if entry is not None and now < entry[1]:
                    return entry[0]
            result = fn(*args, **kwargs)
            with _lock:
                _store[full_key] = (result, now + ttl)
            return result
        wrapper._cache_key_prefix = key_prefix
        return wrapper
    return decorator


def invalidate(*keys: str) -> None:
    """Remove one or more cache entries immediately."""
    with _lock:
        for k in keys:
            popped = _store.pop(k, None)
            if popped is not None:
                logger.debug("Cache invalidated: %s", k)


def invalidate_prefix(prefix: str) -> None:
    """Remove all cache entries whose key starts with `prefix`."""
    with _lock:
        to_del = [k for k in _store if k.startswith(prefix)]
        for k in to_del:
            del _store[k]
        if to_del:
            logger.debug("Cache invalidated prefix '%s': %d entries", prefix, len(to_del))


def clear_all() -> None:
    """Flush the entire cache (useful for tests / admin reset)."""
    with _lock:
        _store.clear()


def cache_get(key: str):
    """Return cached value for key, or None if missing/expired."""
    now = time.monotonic()
    with _lock:
        entry = _store.get(key)
        if entry is not None and now < entry[1]:
            return entry[0]
    return None


def cache_set(key: str, value, ttl: int = 300) -> None:
    """Store value under key with a TTL (seconds)."""
    now = time.monotonic()
    with _lock:
        _store[key] = (value, now + ttl)
