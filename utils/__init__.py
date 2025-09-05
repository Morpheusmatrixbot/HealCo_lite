"""Utility package providing configuration, logging, caching and helper
functions for the HealCo Lite project."""

from .config import get_secret
from .logging import logger
from .cache import _cache_get, _cache_put, CACHE_SCHEMA
from .postgres import db_get, db_set, db_keys_prefix
from . import consts
from .utils import (
    _extract_barcode,
    _extract_country,
    _extract_lang,
    _extract_ml,
    _extract_query,
    _extract_unit,
    _guess_category,
    _guess_country,
    _guess_lang,
    _guess_ml,
    _guess_unit,
    _google_cse_search,
    _google_cse_search_branded,
    _google_cse_search_images,
    _google_search,
    _norm_text,
    _parse_google_recipes,
    _parse_link,
    _url_to_base64,
)

__all__ = [
    "get_secret",
    "logger",
    "_cache_get",
    "_cache_put",
    "CACHE_SCHEMA",
    "db_get",
    "db_set",
    "db_keys_prefix",
    "consts",
    "_extract_barcode",
    "_extract_country",
    "_extract_lang",
    "_extract_ml",
    "_extract_query",
    "_extract_unit",
    "_guess_category",
    "_guess_country",
    "_guess_lang",
    "_guess_ml",
    "_guess_unit",
    "_google_cse_search",
    "_google_cse_search_branded",
    "_google_cse_search_images",
    "_google_search",
    "_norm_text",
    "_parse_google_recipes",
    "_parse_link",
    "_url_to_base64",
]
