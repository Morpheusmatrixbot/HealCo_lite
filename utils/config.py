"""Configuration helpers for HealCo Lite."""

from __future__ import annotations

import os
from dotenv import load_dotenv
from google.cloud import secretmanager

# Load environment variables from a .env file if present.
load_dotenv()


def get_secret(key: str, default: str = "") -> str:
    """Return a secret value from the environment or Google Secret Manager.

    The function first checks the local environment variables. If the key is
    not present it tries to access a secret with the same name in Google Cloud
    Secret Manager. The project ID is taken from the ``GCP_PROJECT`` or
    ``GOOGLE_CLOUD_PROJECT`` environment variable. If the secret cannot be
    retrieved, ``default`` is returned.
    """

    value = os.getenv(key)
    if value is not None:
        return value

    project_id = os.getenv("GCP_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")
    if project_id:
        try:
            client = secretmanager.SecretManagerServiceClient()
            name = f"projects/{project_id}/secrets/{key}/versions/latest"
            response = client.access_secret_version(name=name)
            return response.payload.data.decode("utf-8")
        except Exception:
            pass

    return default


__all__ = ["get_secret"]
