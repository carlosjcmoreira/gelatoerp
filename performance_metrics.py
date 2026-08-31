"""Request-local performance counters with no SQL or user data."""
from contextvars import ContextVar, Token
from dataclasses import dataclass
import time


@dataclass
class RequestMetrics:
    started_at: float
    db_duration_ms: float = 0.0
    query_count: int = 0


_current: ContextVar[RequestMetrics | None] = ContextVar(
    'request_performance_metrics', default=None
)


def begin_request() -> Token:
    return _current.set(RequestMetrics(started_at=time.perf_counter()))


def record_query(duration_seconds: float) -> None:
    metrics = _current.get()
    if metrics is not None:
        metrics.query_count += 1
        metrics.db_duration_ms += duration_seconds * 1000


def snapshot() -> dict:
    metrics = _current.get()
    if metrics is None:
        return {'duration_ms': 0.0, 'db_duration_ms': 0.0, 'query_count': 0}
    return {
        'duration_ms': (time.perf_counter() - metrics.started_at) * 1000,
        'db_duration_ms': metrics.db_duration_ms,
        'query_count': metrics.query_count,
    }


def end_request(token: Token | None) -> None:
    if token is not None:
        _current.reset(token)