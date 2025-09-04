"""Project wide constants."""

from __future__ import annotations

import os
from .config import get_secret

# Caching
CACHE_DAYS: int = int(os.getenv("CACHE_DAYS", "30"))
# Default TTL for cached search results in seconds
SEARCH_CACHE_TTL: int = int(os.getenv("SEARCH_CACHE_TTL", str(CACHE_DAYS * 24 * 60 * 60)))
CACHE_SCHEMA: str = os.getenv("CACHE_SCHEMA", "r1")

# Database
DB_PATH: str = os.getenv("HLITE_DB_PATH", "db.json")
DB_SCHEMA: str = os.getenv("DB_SCHEMA", "r1")
EAT_NOW_DB: str = os.getenv("EAT_NOW_DB", "eat_now.json")

# Google Custom Search configuration
GOOGLE_CSE_KEY: str = get_secret("GOOGLE_CSE_KEY", "")
GOOGLE_CSE_ID: str = get_secret("GOOGLE_CSE_ID", "")

# Miscellaneous
MAX_QUERY_LEN: int = int(os.getenv("MAX_QUERY_LEN", "80"))
ML_LIMIT: int = int(os.getenv("ML_LIMIT", "10"))
USER_AGENT: str = os.getenv("USER_AGENT", "healco-lite/1.0")

__all__ = [
    "CACHE_DAYS",
    "SEARCH_CACHE_TTL",
    "CACHE_SCHEMA",
    "DB_PATH",
    "DB_SCHEMA",
    "EAT_NOW_DB",
    "GOOGLE_CSE_KEY",
    "GOOGLE_CSE_ID",
    "MAX_QUERY_LEN",
    "ML_LIMIT",
    "USER_AGENT",
]
