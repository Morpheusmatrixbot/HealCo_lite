"""Miscellaneous helper functions shared across modules."""

from __future__ import annotations

import base64
import re
from typing import Any, Dict, List, Optional

import requests

from .consts import GOOGLE_CSE_ID, GOOGLE_CSE_KEY, USER_AGENT


# ---------------------------------------------------------------------------
# Text helpers


def _norm_text(text: str) -> str:
    """Normalize text for searching."""
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_barcode(text: str) -> Optional[str]:
    """Extract a sequence of 8-14 digits which looks like a barcode."""
    match = re.search(r"\b(\d{8,14})\b", text)
    return match.group(1) if match else None


def _guess_country(query_text: str) -> str:
    query_lower = query_text.lower()
    if any(w in query_lower for w in ("россия", "рф", "москва")):
        return "ru"
    if any(w in query_lower for w in ("украина", "україна")):
        return "ua"
    if any(w in query_lower for w in ("беларусь", "белоруссия")):
        return "by"
    if "казахстан" in query_lower:
        return "kz"
    return "us"


def _guess_lang(query_text: str) -> str:
    query_lower = query_text.lower()
    if any(lang in query_lower for lang in ["апельсин", "банан", "яблоко", "молоко"]):
        return "ru"
    return "en"


def _extract_country(query_text: str) -> str:
    match = re.search(r"in\s+([a-zA-Z]+)$", query_text)
    if match:
        return match.group(1).lower()
    return _guess_country(query_text)


def _extract_lang(query_text: str) -> str:
    match = re.search(r"in\s+([a-zA-Z]+)$", query_text)
    if match:
        lang_map = {"french": "fr", "german": "de", "spanish": "es", "russian": "ru"}
        return lang_map.get(match.group(1).lower(), _guess_lang(query_text))
    return _guess_lang(query_text)


def _extract_ml(query_text: str) -> str:
    return "ml" if "ml" in query_text.lower() else "g"


def _extract_unit(query_text: str) -> Optional[str]:
    units = ["cup", "oz", "tbsp", "tsp", "lb", "kg", "g", "ml", "l"]
    ql = query_text.lower()
    for unit in units:
        if unit in ql:
            return unit
    return None


def _guess_category(query_text: str) -> str:
    ql = query_text.lower()
    if any(word in ql for word in ["apple", "pear", "banana", "orange", "grape", "strawberry", "fruit"]):
        return "Fruits"
    if any(word in ql for word in ["carrot", "broccoli", "spinach", "potato", "tomato", "vegetable"]):
        return "Vegetables"
    if any(word in ql for word in ["chicken", "beef", "pork", "fish", "meat", "egg"]):
        return "Meat & Poultry"
    if any(word in ql for word in ["milk", "cheese", "yogurt", "butter"]):
        return "Dairy"
    if any(word in ql for word in ["bread", "rice", "pasta", "cereal", "flour", "oats"]):
        return "Grains & Pasta"
    if any(word in ql for word in ["oil", "sugar", "salt", "spice", "sauce", "dressing"]):
        return "Oils & Fats"
    if any(word in ql for word in ["water", "juice", "tea", "coffee", "soda", "drink"]):
        return "Beverages"
    return "Unknown"


def _guess_ml(query_text: str) -> Optional[float]:
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:мл|ml)\b", query_text, flags=re.I)
    if match:
        return float(match.group(1).replace(",", "."))
    return None


def _guess_unit(query_text: str) -> Optional[str]:
    return _extract_unit(query_text)


def _extract_query(text: str) -> str:
    """Return text stripped from common noise such as units or numbers."""
    clean = _norm_text(text)
    # remove simple quantity expressions
    clean = re.sub(r"\b\d+[.,]?\d*\s*(?:g|гр|kg|ml|l|литр(?:а|ов)?)\b", "", clean)
    return clean.strip()


# ---------------------------------------------------------------------------
# Networking helpers

async def _google_cse_search(
    query: str,
    num: int = 10,
    search_type: str | None = None,
    organic_only: bool = False,
    domain_filter: str | None = None,
) -> List[Dict[str, Any]]:
    """Perform a Google Custom Search query and return raw items."""
    if not GOOGLE_CSE_KEY or not GOOGLE_CSE_ID:
        return []
    params = {
        "key": GOOGLE_CSE_KEY,
        "cx": GOOGLE_CSE_ID,
        "q": query,
        "num": num,
    }
    if search_type == "image":
        params["searchType"] = "image"
    if domain_filter:
        params["siteSearch"] = domain_filter
        params["siteSearchFilter"] = "i" if organic_only else "e"

    headers = {"User-Agent": USER_AGENT}

    def _do_request():
        return requests.get("https://www.googleapis.com/customsearch/v1", params=params, headers=headers, timeout=20)

    resp = await __import__("asyncio").to_thread(_do_request)
    if resp.status_code != 200:
        return []
    data = resp.json()
    return data.get("items", [])


async def _google_cse_search_branded(query: str, num: int = 10) -> List[Dict[str, Any]]:
    return await _google_cse_search(
        query,
        num=num,
        search_type="organic",
        organic_only=True,
        domain_filter="fatsecret.com,fatsecret.ru,ozon.ru,wildberries.ru",
    )


async def _google_cse_search_images(
    query: str, referer_url: str, title: str, grams: Optional[float], milli_l: Optional[float]
) -> List[Dict[str, Any]]:
    results = await _google_cse_search(query, search_type="image", num=1)
    if not results:
        return []
    image_url = results[0].get("link")
    if not image_url:
        return []
    return [
        {
            "image_url": image_url,
            "referer_url": referer_url,
            "title": title,
            "grams": grams,
            "milli_l": milli_l,
        }
    ]


async def _google_search(query: str, num: int = 10) -> List[Dict[str, Any]]:
    """Fallback to Google CSE search when plain search is requested."""
    return await _google_cse_search(query, num=num)


# ---------------------------------------------------------------------------
# Parsing helpers


def _parse_link(url: Optional[str], title: str, snippet: str, source: str, referer_url: str | None = None) -> Optional[Dict[str, Any]]:
    """Very small heuristic parser extracting nutritional information from text."""
    text = f"{title} {snippet}"
    cal = re.search(r"(\d+)\s*(?:kcal|cal)\b", text, re.I)
    prot = re.search(r"protein[:\s]+([\d.]+)\s*", text, re.I)
    fat = re.search(r"fat[:\s]+([\d.]+)\s*", text, re.I)
    carb = re.search(r"carb[:\s]+([\d.]+)\s*", text, re.I)
    if not cal and not prot and not fat and not carb:
        return None
    data: Dict[str, Any] = {
        "name": title or "—",
        "source": source,
        "source_url": url or referer_url,
    }
    if cal:
        data["kcal_100g"] = float(cal.group(1))
    if prot:
        data["protein_100g"] = float(prot.group(1))
    if fat:
        data["fat_100g"] = float(fat.group(1))
    if carb:
        data["carbs_100g"] = float(carb.group(1))
    return data


def _parse_google_recipes(
    url: str, title: str, snippet: str, grams: Optional[float], milli_l: Optional[float]
) -> Optional[Dict[str, Any]]:
    try:
        data = {}
        cal_match = re.search(r"(\d+)\s*(?:kcal|cal)\b", snippet, re.IGNORECASE)
        if cal_match:
            data["kcal_100g"] = int(cal_match.group(1))
        macro_match = re.search(
            r"(?:protein|белки)[:\s]+([\d.]+)\s*г\b.*?(?:fat|жиры)[:\s]+([\d.]+)\s*г\b.*?(?:carbs|углеводы)[:\s]+([\d.]+)\s*г\b",
            snippet,
            re.IGNORECASE,
        )
        if macro_match:
            data["protein_100g"] = float(macro_match.group(1))
            data["fat_100g"] = float(macro_match.group(2))
            data["carbs_100g"] = float(macro_match.group(3))
        if data.get("kcal_100g") is not None:
            data["name"] = title
            data["source"] = "Google Recipes"
            data["source_url"] = url
            return data
    except Exception:
        pass
    return None


def _url_to_base64(data: bytes) -> str:
    return base64.b64encode(data).decode("utf-8")


__all__ = [
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
