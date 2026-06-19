"""In-memory TTL cache for SerpAPI flight/hotel results (1 hour)."""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any

_TTL_SECONDS = 3600
_store: dict[str, tuple[float, Any]] = {}


def _cache_key(prefix: str, payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, default=str)
    digest = hashlib.sha256(raw.encode()).hexdigest()[:20]
    return f"{prefix}:{digest}"


def get_cached(prefix: str, payload: dict[str, Any]) -> Any | None:
    key = _cache_key(prefix, payload)
    entry = _store.get(key)
    if not entry:
        return None
    expires, value = entry
    if time.time() >= expires:
        _store.pop(key, None)
        return None
    return value


def set_cached(prefix: str, payload: dict[str, Any], value: Any) -> None:
    key = _cache_key(prefix, payload)
    _store[key] = (time.time() + _TTL_SECONDS, value)


def clear_cache() -> None:
    _store.clear()
