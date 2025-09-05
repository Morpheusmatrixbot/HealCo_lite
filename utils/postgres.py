import os
from contextlib import contextmanager
from typing import Any, List

import psycopg2
from psycopg2.extras import Json


DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_PASS")
DB_NAME = os.getenv("DB_NAME")
INSTANCE_UNIX_SOCKET = os.getenv("INSTANCE_UNIX_SOCKET")


def get_connection():
    """Create a new database connection using environment variables."""
    host = f"/cloudsql/{INSTANCE_UNIX_SOCKET}" if INSTANCE_UNIX_SOCKET else None
    return psycopg2.connect(user=DB_USER, password=DB_PASS, dbname=DB_NAME, host=host)


@contextmanager
def get_cursor():
    """Context manager yielding a cursor and handling connection lifecycle."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            yield cur
            conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Initialize key/value storage table if it does not exist."""
    with get_cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS kv_store (
                key TEXT PRIMARY KEY,
                value JSONB
            )
            """
        )


def db_get(key: str, default: Any = None) -> Any:
    with get_cursor() as cur:
        cur.execute("SELECT value FROM kv_store WHERE key=%s", (key,))
        row = cur.fetchone()
    if row:
        return row[0]
    return default


def db_set(key: str, value: Any) -> None:
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO kv_store (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """,
            (key, Json(value)),
        )


def db_keys_prefix(prefix: str) -> List[str]:
    with get_cursor() as cur:
        cur.execute("SELECT key FROM kv_store WHERE key LIKE %s", (prefix + "%",))
        return [r[0] for r in cur.fetchall()]


__all__ = [
    "get_connection",
    "get_cursor",
    "init_db",
    "db_get",
    "db_set",
    "db_keys_prefix",
]
