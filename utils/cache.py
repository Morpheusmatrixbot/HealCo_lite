"""Simple SQLite based cache used for search results."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Optional

from .consts import CACHE_SCHEMA

# Location for the cache database
os.makedirs("./data", exist_ok=True)
_con = sqlite3.connect("./data/cache.db")
_con.execute(
    """CREATE TABLE IF NOT EXISTS cache (
    key TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    last_used INTEGER NOT NULL,
    ttl INTEGER NOT NULL,
    size_bytes INTEGER NOT NULL
)"""
)
_con.commit()


def _cache_get(k: str) -> Optional[Any]:
    """Return cached object for *k* if it has not expired."""
    row = _con.execute(
        "SELECT payload,last_used,ttl FROM cache WHERE key=?", (k,)
    ).fetchone()
    if not row:
        return None
    payload, last_used, ttl = row
    now = int(time.time())
    if ttl and last_used + ttl < now:
        _con.execute("DELETE FROM cache WHERE key=?", (k,))
        _con.commit()
        return None
    _con.execute("UPDATE cache SET last_used=? WHERE key=?", (now, k))
    _con.commit()
    try:
        return json.loads(payload)
    except Exception:
        return None


def _cache_put(k: str, obj: Any, ttl: int = 0, limit_mb: int = 50) -> None:
    """Store *obj* in the cache under *k* for *ttl* seconds."""
    data = json.dumps(obj, ensure_ascii=False)
    now = int(time.time())
    _con.execute(
        "INSERT OR REPLACE INTO cache(key,payload,last_used,ttl,size_bytes) VALUES (?,?,?,?,?)",
        (k, data, now, int(ttl), len(data)),
    )
    _con.commit()

    # Prune old items if the database grows too large
    total = _con.execute("SELECT COALESCE(SUM(size_bytes),0) FROM cache").fetchone()[0] or 0
    limit = limit_mb * 1024 * 1024
    while total > limit:
        _con.execute(
            "DELETE FROM cache WHERE key IN (SELECT key FROM cache ORDER BY last_used ASC LIMIT 50)"
        )
        _con.commit()
        total = _con.execute("SELECT COALESCE(SUM(size_bytes),0) FROM cache").fetchone()[0] or 0


__all__ = ["_cache_get", "_cache_put", "CACHE_SCHEMA"]
