"""Configuration helpers for HealCo Lite."""

from __future__ import annotations

import os
from dotenv import load_dotenv

# Load environment variables from a .env file if present.
load_dotenv()


def get_secret(key: str, default: str = "") -> str:
    """Return a secret value from the environment.

    This is a thin wrapper around :func:`os.getenv` that provides a default
    value when the key is missing.  It mirrors the behaviour used throughout
    the project and keeps the logic in a single place.
    """

    return os.getenv(key, default)


__all__ = ["get_secret"]
