from __future__ import annotations

import time
from typing import Optional


class TTLCache:
    """Simple in-memory TTL cache.

    Entries are keyed by a string that encodes all request parameters plus an
    optional data version token (e.g. ``SELECT MAX(ts)``).  The TTL is used only
    for eventual eviction; a changed version token produces a different key and
    forces a fresh build.
    """

    def __init__(self, maxsize: int = 1000):
        self._maxsize = maxsize
        self._data: dict[str, tuple[float, bytes]] = {}

    def get(self, key: str) -> Optional[bytes]:
        now = time.time()
        entry = self._data.get(key)
        if entry is None:
            return None
        expires_at, data = entry
        if now >= expires_at:
            del self._data[key]
            return None
        return data

    def set(self, key: str, data: bytes, ttl: float) -> None:
        self._data[key] = (time.time() + ttl, data)
        if len(self._data) > self._maxsize:
            self._evict()

    def __len__(self) -> int:
        return len(self._data)

    def clear(self) -> None:
        self._data.clear()

    def _evict(self) -> None:
        now = time.time()
        stale = [k for k, (exp, _) in self._data.items() if now >= exp]
        for k in stale:
            del self._data[k]
        if len(self._data) > self._maxsize:
            sorted_entries = sorted(self._data.keys(), key=lambda k: self._data[k][0])
            for k in sorted_entries[: len(self._data) - self._maxsize]:
                del self._data[k]


def cache_key(**parts) -> str:
    """Deterministic cache-key string from keyword arguments."""
    return "|".join(f"{k}={v}" for k, v in sorted(parts.items()))
