"""Light‑weight key/value storage used as a fallback when external databases
are unavailable."""

from __future__ import annotations

import json
import os
from typing import Any, List

from .consts import DB_PATH


class LocalDB:
    """Very small JSON backed key/value store."""

    def __init__(self, path: str = DB_PATH):
        self.path = path
        if not os.path.exists(self.path):
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({}, f, ensure_ascii=False, indent=2)
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.store = json.load(f)
        except Exception:
            self.store = {}

    def _save(self) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.store, f, ensure_ascii=False, indent=2)

    # Mapping style helpers -------------------------------------------------
    def __getitem__(self, k: str) -> Any:
        return self.store.get(k)

    def __setitem__(self, k: str, v: Any) -> None:
        self.store[k] = v
        self._save()

    def __contains__(self, k: str) -> bool:
        return k in self.store

    def keys(self) -> List[str]:
        return list(self.store.keys())


DB = LocalDB()


def db_get(k: str, default: Any = None) -> Any:
    return DB.store.get(k, default)


def db_set(k: str, v: Any) -> None:
    DB[k] = v


def db_keys_prefix(prefix: str) -> List[str]:
    return [k for k in DB.keys() if str(k).startswith(prefix)]


__all__ = ["LocalDB", "DB", "db_get", "db_set", "db_keys_prefix"]
