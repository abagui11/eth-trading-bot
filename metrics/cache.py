"""Short TTL in-memory cache for external metric API calls."""

from __future__ import annotations

import time
from typing import Any, Callable, TypeVar

T = TypeVar("T")

_store: dict[str, tuple[Any, float]] = {}
_DEFAULT_TTL_SEC = 90.0


def get_or_fetch(key: str, fetcher: Callable[[], T], ttl_sec: float = _DEFAULT_TTL_SEC) -> T:
    now = time.time()
    cached = _store.get(key)
    if cached is not None and now < cached[1]:
        return cached[0]
    value = fetcher()
    _store[key] = (value, now + ttl_sec)
    return value


def clear_cache() -> None:
    _store.clear()
