import asyncio
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests_oauthlib import OAuth1
from bs4 import BeautifulSoup

from utils.cache import CACHE_SCHEMA, _cache_get, _cache_put
from utils.config import get_secret
from utils.consts import (
    CACHE_DAYS,
    DB_PATH,
    DB_SCHEMA,
    EAT_NOW_DB,
    GOOGLE_CSE_ID,
    GOOGLE_CSE_KEY,
    MAX_QUERY_LEN,
    ML_LIMIT,
    SEARCH_CACHE_TTL,
    USER_AGENT,
)
from utils.logging import logger
from utils.utils import (
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

# ===================== FATSECRET CONFIG =====================
FATSECRET_KEY = get_secret("FATSECRET_KEY", "")
FATSECRET_SECRET = get_secret("FATSECRET_SECRET", "")
FS_BASE = "https://platform.fatsecret.com/rest/server.api"
VISION_KEY = get_secret("VISION_KEY", "")

# ===================== AV.RU SCRAPER CONFIG =====================
AV_UA_HEADERS = {
    "User-Agent": "healco-lite/1.0 (+mailto:Rafael.sayadi@gmail.com)",
    "Accept-Language": "ru,ru-RU;q=0.9,en;q=0.8",
}
# глобальный лимитер: <= 1 запрос/сек к av.ru
_av_sem = asyncio.Semaphore(1)
_av_last_ts = 0.0
async def _av_rate_limit():
    global _av_last_ts
    async with _av_sem:
        now = time.time()
        delay = max(0.0, 1.0 - (now - _av_last_ts))
        if delay:
            await asyncio.sleep(delay)
        _av_last_ts = time.time()


def _to_float(v):
    if v is None:
        return None
    s = str(v)
    s = re.sub(r"[^\d,.\-]", "", s).replace(",", ".")
    try:
        return float(s)
    except Exception:
        return None

def _num(m, idx=1):
    if not m:
        return None
    try:
        return float(m.group(idx).replace(",", "."))
    except Exception:
        return None

def _av_from_ld(ld):
    if not isinstance(ld, dict):
        return None
    name = (ld.get("name") or ld.get("headline") or "").strip() or "—"
    brand = ld.get("brand")
    if isinstance(brand, dict):
        brand = brand.get("name")
    nut = ld.get("nutrition") or {}
    def g(k):
        v = nut.get(k)
        if isinstance(v, dict):
            v = v.get("@value") or v.get("value")
        return _to_float(v)
    kcal = g("calories") or g("energy") or g("caloriesContent")
    prot = _to_float(nut.get("proteinContent"))
    fat  = _to_float(nut.get("fatContent"))
    carb = _to_float(nut.get("carbohydrateContent"))
    if any(x is not None for x in (kcal, prot, fat, carb)):
        return {
            "name": name, "brand": brand,
            "kcal_100g": kcal, "protein_100g": prot, "fat_100g": fat, "carbs_100g": carb,
            "barcode": None,
        }
    return None

def _av_from_state(pr):
    if not isinstance(pr, dict):
        return None
    nut = pr.get("nutrition") or {}
    if not nut:
        return None
    return {
        "name": pr.get("title") or pr.get("name") or "—",
        "brand": pr.get("brand"),
        "kcal_100g": _to_float(nut.get("calories")),
        "protein_100g": _to_float(nut.get("protein")),
        "fat_100g": _to_float(nut.get("fat")),
        "carbs_100g": _to_float(nut.get("carbohydrates")),
        "barcode": pr.get("barcode") or pr.get("ean"),
    }

def _parse_av_ru_html(html: str) -> dict | None:
    """Возвращает dict с полями *_100g и опционально barcode."""
    soup = BeautifulSoup(html, "lxml")
    # 1) JSON-LD
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(tag.string or "")
            if isinstance(data, list):
                for item in data:
                    out = _av_from_ld(item)
                    if out:
                        return out
            else:
                out = _av_from_ld(data)
                if out:
                    return out
        except Exception:
            pass
    # 2) window.__INITIAL_STATE__
    m = re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;<", html, flags=re.S)
    if m:
        try:
            st = json.loads(m.group(1))
            pr = (st.get("product") or st.get("goods") or {}).get("item") or {}
            out = _av_from_state(pr)
            if out:
                return out
        except Exception:
            pass
    # 3) Грубый разбор текста
    txt = soup.get_text(" ", strip=True).lower()
    kcal = _num(re.search(r"ккал[^\d]{0,5}(\d+[.,]?\d*)", txt))
    prot = _num(re.search(r"бел(ок|ки)[^\d]{0,5}(\d+[.,]?\d*)", txt), idx=2)
    fat  = _num(re.search(r"жир(ы)?[^\d]{0,5}(\d+[.,]?\d*)",   txt), idx=2)
    carb = _num(re.search(r"углевод(ы)?[^\d]{0,5}(\d+[.,]?\d*)", txt), idx=2)
    if any(v is not None for v in (kcal, prot, fat, carb)):
        h1 = soup.select_one("h1")
        title = h1.get_text(strip=True) if h1 else None
        if not title:
            og = soup.find("meta", attrs={"property": "og:title"})
            title = og.get("content") if og else "—"
        # Баркод часто встречается в тексте
        bcod = _num(re.search(r"\b(\d{8,14})\b", txt))
        return {
            "name": title or "—",
            "brand": None,
            "kcal_100g": kcal, "protein_100g": prot, "fat_100g": fat, "carbs_100g": carb,
            "barcode": str(int(bcod)) if bcod else None,
        }
    return None


async def _fetch_av_ru(url: str) -> dict | None:
    """Загружает и парсит av.ru (с кэшем и лимитом)."""
    ck = f"avru:{url}"
    cached = _cache_get(ck)
    if cached:
        return cached
    try:
        await _av_rate_limit()
        def _do():
            return requests.get(url, headers=AV_UA_HEADERS, timeout=20)
        r = await asyncio.to_thread(_do)
        if r.status_code != 200:
            logger.warning("av.ru HTTP %s for %s", r.status_code, url)
            return None
        data = _parse_av_ru_html(r.text)
        if data:
            _cache_put(ck, data, ttl=60*60*24*3)  # кэш 3 дня
        return data
    except Exception as e:
        logger.warning("av.ru fetch failed: %s", e)
        return None


def _fatsecret_auth():
    if not FATSECRET_KEY or not FATSECRET_SECRET:
        return None
    # FatSecret требует точно такую подпись OAuth1
    return OAuth1(
        client_key=FATSECRET_KEY,
        client_secret=FATSECRET_SECRET,
        signature_method='HMAC-SHA1',
        signature_type='QUERY'  # FatSecret требует параметры в query string
    )


async def _fs_request(method: str, params: dict | None = None) -> dict | None:
    """Universal FatSecret REST call (OAuth1 signed)."""
    auth = _fatsecret_auth()
    if not auth:
        logger.warning("FatSecret credentials are not set (empty key/secret). Skip FS call.")
        return None
    
    # FatSecret API требует GET запрос с параметрами в query string
    query_params = {"method": method, "format": "json"}
    if params:
        query_params.update(params)

    def _do():
        # FatSecret API использует GET с OAuth подписью в query string
        return requests.get(FS_BASE, params=query_params, auth=auth, timeout=25)

    try:
        logger.info(f"FatSecret API call: {method}, params: {params}")
        r = await asyncio.to_thread(_do)
        logger.info(f"FatSecret API call: {method}, status: {r.status_code}")
        logger.debug(f"FatSecret request URL: {r.url}")
        
        if r.status_code != 200:
            body = r.text[:1000]
            logger.error(f"FatSecret HTTP {r.status_code}. Body: {body}")
            if r.status_code in (401, 403):
                logger.error("Check: 1) API keys correct 2) IP whitelisted 3) Plan active")
            return None
        
        try:
            json_data = r.json()
            logger.info(f"FatSecret response received: {type(json_data)}")
            logger.debug(f"FatSecret raw response: {json_data}")
            
            # Проверяем на ошибки в ответе
            if isinstance(json_data, dict) and "error" in json_data:
                error_info = json_data["error"]
                logger.error(f"FatSecret API error: {error_info}")
                return json_data  # Возвращаем ошибку для отладки
                
            return json_data
        except Exception as je:
            logger.warning("FatSecret JSON decode failed: %s... (%s)", r.text[:300], je)
            return None
    except Exception as e:
        logger.warning("FatSecret request failed: %s", e)
        return None


async def _fs_get_food(food_id: str) -> Optional[Dict[str, Any]]:
    """Get food item by ID from FatSecret (v2)."""
    res = await _fs_request("food.get.v2", {"food_id": str(food_id)})
    if res and "food" in res:
        return res["food"]
    return res  # Возвращаем как есть, если структура отличается

async def _fs_find_by_barcode(barcode: str) -> Optional[str]:
    """Find food ID by barcode from FatSecret."""
    res = await _fs_request("food.find_id_for_barcode", {"barcode": barcode})
    if res and isinstance(res, dict):
        return res.get("food_id", res.get("id"))
    return None

def _extract_barcode(text: str) -> Optional[str]:
    """Extract barcode from text."""
    # Ищем последовательности из 8-14 цифр
    match = re.search(r'\b(\d{8,14})\b', text)
    return match.group(1) if match else None

async def _fs_search_best(query: str) -> Optional[Dict[str, Any]]:
    """Search for the best matching food item from FatSecret."""
    # Правильные параметры для foods.search
    res = await _fs_request("foods.search", {"search_expression": query, "max_results": "10"})
    
    logger.info(f"FatSecret search response for '{query}': {res}")
    
    if not res:
        logger.info(f"No response from FatSecret for: {query}")
        return None
    
    # Обработка разных форматов ответа
    foods_data = None
    if "foods" in res:
        foods_data = res["foods"]
        if "food" in foods_data:
            foods_data = foods_data["food"]
    elif "food" in res:
        foods_data = res["food"]
    
    if not foods_data:
        logger.info(f"No foods found in FatSecret response for: {query}")
        return None
        
    # Нормализуем в список
    if isinstance(foods_data, dict):
        foods = [foods_data]
    else:
        foods = foods_data
    
    if not foods:
        logger.info(f"Empty foods list for: {query}")
        return None
    
    logger.info(f"Found {len(foods)} foods for '{query}'")
    
    # Выбираем лучший результат
    def _score(fd: Dict[str, Any]) -> int:
        score = 0
        # Приоритет брендовым продуктам
        if fd.get("brand_name"):
            score += 2
        # Приоритет продуктам с ID (полные записи)
        if fd.get("food_id"):
            score += 1
        return score
    
    best = max(foods, key=_score)
    food_id = str(best.get("food_id", ""))
    
    if not food_id:
        logger.warning(f"No food_id found for best match: {best}")
        return None
    
    logger.info(f"Selected best food ID {food_id} for '{query}'")
    return await _fs_get_food(food_id)

def _fs_norm(food: Optional[Dict[str, Any]], grams: Optional[float], milli_l: Optional[float]) -> Optional[Dict[str, Any]]:
    """Normalize FatSecret food item to our internal format."""
    if not food:
        return None
    # ищем лучшую порцию с метрическими единицами
    servings = (food.get("servings") or {}).get("serving") or []
    if not isinstance(servings, list):
        servings = [servings]

    def _sv_score(s):
        unit = (s.get("metric_serving_unit") or s.get("serving_unit") or "").lower()
        sc = 0
        if unit in ("g","ml"): sc += 3
        if s.get("metric_serving_amount"): sc += 1
        return sc
    best_serving = max(servings, key=_sv_score) if servings else None

    if not best_serving:
        return None

    # данные на порцию
    kcal_p = _to_float(best_serving.get("calories"))
    p_p    = _to_float(best_serving.get("protein"))
    f_p    = _to_float(best_serving.get("fat"))
    c_p    = _to_float(best_serving.get("carbohydrate"))
    amt    = _to_float(best_serving.get("metric_serving_amount")) or _to_float(best_serving.get("serving_amount"))
    unit   = (best_serving.get("metric_serving_unit") or best_serving.get("serving_unit") or "").lower()
    portion_mass = None
    if amt:
        if unit in ("g","ml"):
            portion_mass = amt
        elif unit == "oz":
            portion_mass = amt * 28.3495
        elif unit == "lb":
            portion_mass = amt * 453.592

    # пересчёт к 100 г/мл
    kcal100 = p100 = f100 = c100 = None
    if portion_mass and portion_mass > 0:
        k = 100.0 / portion_mass
        kcal100 = kcal_p * k if kcal_p is not None else None
        p100    = p_p   * k if p_p   is not None else None
        f100    = f_p   * k if f_p   is not None else None
        c100    = c_p   * k if c_p   is not None else None

    return {
        "name": (food.get("food_name") or "—").strip(),
        "brand": (food.get("brand_name") or None),
        "kcal_100g": kcal100,
        "protein_100g": p100,
        "fat_100g": f100,
        "carbs_100g": c100,
        "source": "🧩 FatSecret",
        "portion_g": grams,
        "portion_ml": milli_l,
        "kcal_portion": (kcal100 * (grams/100.0)) if (kcal100 is not None and grams) else ((kcal100 * (milli_l/100.0)) if (kcal100 is not None and milli_l) else None),
        "protein_portion": (p100 * (grams/100.0)) if (p100 is not None and grams) else ((p100 * (milli_l/100.0)) if (p100 is not None and milli_l) else None),
        "fat_portion": (f100 * (grams/100.0)) if (f100 is not None and grams) else ((f100 * (milli_l/100.0)) if (f100 is not None and milli_l) else None),
        "carbs_portion": (c100 * (grams/100.0)) if (c100 is not None and grams) else ((c100 * (milli_l/100.0)) if (c100 is not None and milli_l) else None),
    }


async def search(
    query_text: str,
    grams: Optional[float] = None,
    milli_l: Optional[float] = None,
    lang: str = "en",
    country: str = "us",
    user_id: int = 0,
) -> List[Dict[str, Any]]:
    """Search for food data, prioritizing FatSecret and then Google CSE."""
    clean = _norm_text(query_text)
    query_text = query_text.strip()  # Keep original query for potential barcode extraction

    # Определяем категорию для фильтрации
    cat = _guess_category(query_text)

    candidates = []

    # ======= 0) FATSECRET — ПРИОРИТЕТНЫЙ ШАГ =======
    try:
        if FATSECRET_KEY and FATSECRET_SECRET:
            # 0a) штрих-код
            barcode = _extract_barcode(query_text)
            if barcode:
                ck = f"fs:bar:{CACHE_SCHEMA}:{barcode}:{grams}:{milli_l}"
                cached = _cache_get(ck)
                if cached:
                    logger.info(f"FatSecret cached by barcode {barcode}")
                    return cached if isinstance(cached, list) else [cached]
                
                # Поиск по штрих-коду
                food_id = await _fs_find_by_barcode(barcode)
                if food_id:
                    food = await _fs_get_food(str(food_id))
                    if food:
                        res = _fs_norm(food, grams, milli_l)
                        if res and res.get("kcal_100g") is not None:
                            _cache_put(ck, [res], ttl=SEARCH_CACHE_TTL)
                            return [res]
            
            # 0b) поиск по названию
            ck = f"fs:q:{CACHE_SCHEMA}:{clean}:{grams}:{milli_l}"
            cached = _cache_get(ck)
            if cached:
                logger.info(f"FatSecret cached by query {clean}")
                return cached if isinstance(cached, list) else [cached]
            
            food = await _fs_search_best(clean)
            if food:
                res = _fs_norm(food, grams, milli_l)
                if res and res.get("kcal_100g") is not None:
                    _cache_put(ck, [res], ttl=SEARCH_CACHE_TTL)
                    logger.info(f"FatSecret success for '{clean}': {res.get('name')}")
                    return [res]
    except Exception as e:
        logger.warning(f"FatSecret branch failed: {e}")
        import traceback
        traceback.print_exc()

    # ======= 1) GOOGLE CSE (fallback) =======
    # Включаем av.ru как сильный российский источник
    urls = await _google_cse_search_branded(
        clean + " калорийность белки жиры углеводы "
        "site:av.ru OR site:fatsecret.ru OR site:fatsecret.com OR site:ozon.ru OR site:wildberries.ru "
        "OR site:perekrestok.ru OR site:lenta.ru",
        num=8
    )

    # ====== 2) GOOGLE CSE (general search) ======
    # Если ничего не нашли, пробуем обычный поиск
    if not urls and len(clean) <= MAX_QUERY_LEN:
        urls = await _google_cse_search(clean, num=10)

    # ====== 3) Parse results ======
    # Приведём результаты CSE к единому виду: список dict {link,title,snippet}
    norm_urls: List[Dict[str, str]] = []
    for it in (urls or []):
        if isinstance(it, str):
            norm_urls.append({"link": it, "title": "", "snippet": ""})
        elif isinstance(it, dict):
            if it.get("link"):
                norm_urls.append({"link": it.get("link"), "title": it.get("title",""), "snippet": it.get("snippet","")})

    tasks: List[asyncio.Task] = []
    for url_data in norm_urls:
        url = url_data["link"]
        title = url_data.get("title","")
        snippet = url_data.get("snippet","")

        if "://av.ru/" in url:
            tasks.append(asyncio.create_task(_fetch_av_ru(url)))
        elif "fatsecret" in url and ("ru" in url or "com" in url):
            tasks.append(asyncio.create_task(_parse_fatsecret_item(url, title, snippet, grams, milli_l)))
        elif "ozon" in url or "wildberries" in url:
            tasks.append(asyncio.create_task(_parse_retail_item(url, title, snippet, grams, milli_l)))
        else:
            # CSE images → Vision OCR (с base64) — пробуем, если есть ключ
            if VISION_KEY:
                # Try to extract image URL from snippet or use cached image
                image_url = re.search(r"https?://[^\s]+?\.(?:jpe?g|png|gif|bmp)", snippet)
                if image_url:
                    tasks.append(asyncio.create_task(_parse_vision_ocr(image_url.group(0), url, title, grams, milli_l)))
                else:
                    # Fallback to image search if no image in snippet
                    tasks.append(asyncio.create_task(
                        _google_cse_search_images(
                            f"{clean} nutrition facts", url, title, grams, milli_l
                        )
                    ))

    # Execute parsing tasks concurrently
    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        # Flatten the list of results and filter out None values and exceptions
        for result in results:
            if isinstance(result, Exception):
                logger.warning(f"Task failed: {result}")
                continue
            if isinstance(result, list):
                candidates.extend([item for item in result if item])
            elif result:
                candidates.append(result)

    # ====== 4) Sort and cache ======
    # Sort by kcal_100g if available, otherwise by name
    candidates.sort(key=lambda x: (x.get("kcal_100g") is None, x.get("name", "").lower()))

    # Cache results for the query
    cache_key = f"search:{CACHE_SCHEMA}:{clean}:{grams}:{milli_l}:{lang}:{country}"
    _cache_put(cache_key, candidates, ttl=SEARCH_CACHE_TTL)

    return candidates


async def _parse_fatsecret_item(
    url: str, title: str, snippet: str, grams: Optional[float], milli_l: Optional[float]
) -> List[Dict[str, Any]]:
    """Parse a FatSecret item URL and return normalized data."""
    try:
        food_id = re.search(r"(?:foodid|food_id)=(\d+)", url)
        if not food_id:
            return []
        food = await _fs_get_food(food_id.group(1))
        if not food:
            return []
        res = _fs_norm(food, grams, milli_l)
        if res and res.get("kcal_100g") is not None:
            # Add source URL to the result
            res["source_url"] = url
            return [res]
    except Exception as e:
        logger.warning(f"Failed to parse FatSecret item from {url}: {e}")
    return []


async def _parse_retail_item(
    url: str, title: str, snippet: str, grams: Optional[float], milli_l: Optional[float]
) -> List[Dict[str, Any]]:
    """Parse a retail item URL (Ozon, Wildberries) and return normalized data."""
    data = None
    if "ozon" in url:
        data = _parse_link(url, title, snippet, "ozon")
    elif "wildberries" in url:
        data = _parse_link(url, title, snippet, "wildberries")

    if data and data.get("kcal_100g") is not None:
        # Use FatSecret normalization logic if possible
        # Try to find a matching food item on FatSecret by name
        fs_match = await _fs_search_best(data.get("name", ""))
        if fs_match:
            res = _fs_norm(fs_match, grams, milli_l)
            if res and res.get("kcal_100g") is not None:
                # Prioritize FatSecret data if available and valid
                res["source_url"] = url
                return [res]

        # If no FatSecret match, return the parsed retail data
        return [data]
    return []


async def _parse_vision_ocr(
    image_url: str, referer_url: str, title: str, grams: Optional[float], milli_l: Optional[float]
) -> List[Dict[str, Any]]:
    """Parse food data from an image using Google Vision OCR."""
    try:
        image_data = requests.get(image_url, timeout=10).content
        base64_image = _url_to_base64(image_data)
        if not base64_image:
            return []

        # Use Google Vision API for OCR
        if not VISION_KEY:
            return []
        response = requests.post(
            f"https://vision.googleapis.com/v1/images:annotate?key={VISION_KEY}",
            json={
                "requests": [
                    {
                        "image": {"content": base64_image},
                        "features": [{"type": "TEXT_DETECTION"}],
                    }
                ]
            },
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()

        if not data["responses"] or not data["responses"][0]["fullTextAnnotation"]:
            return []

        ocr_text = data["responses"][0]["fullTextAnnotation"]["text"]
        logger.info(f"Vision OCR result for {image_url}: {ocr_text[:200]}")

        # Try to parse nutrition data from OCR text
        parsed_data = _parse_link(None, title, ocr_text, "vision_ocr", referer_url=referer_url)
        if parsed_data and parsed_data.get("kcal_100g") is not None:
            return [parsed_data]

    except Exception as e:
        logger.warning(f"Vision OCR failed for {image_url}: {e}")
    return []


async def _google_cse_search_images(
    query: str, referer_url: str, title: str, grams: Optional[float], milli_l: Optional[float]
) -> List[Dict[str, Any]]:
    """Perform a Google Custom Search for images and parse results."""
    results = await _google_cse_search(query, search_type="image", num=1)
    if not results:
        return []
    image_url = results[0].get("link")
    if image_url:
        return await _parse_vision_ocr(image_url, referer_url, title, grams, milli_l)
    return []


def _parse_google_recipes(
    url: str, title: str, snippet: str, grams: Optional[float], milli_l: Optional[float]
) -> Optional[Dict[str, Any]]:
    """Parse recipe data from a Google search result."""
    try:
        # Basic attempt to find recipe data in snippet or title
        data = {}
        # Example: Extracting calories from snippet like "100g: 150 kcal"
        cal_match = re.search(r"(\d+)\s*(?:kcal|cal)\b", snippet, re.IGNORECASE)
        if cal_match:
            data["kcal_100g"] = int(cal_match.group(1))

        # Try to extract macros if available
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

    except Exception as e:
        logger.warning(f"Failed to parse recipe data from {url}: {e}")
    return None


def _guess_country(query_text: str) -> str:
    """Guess the country from the query text."""
    # Simple keyword-based guessing
    query_lower = query_text.lower()
    if "россия" in query_lower or "рф" in query_lower or "москва" in query_lower:
        return "ru"
    if "украина" in query_lower or "україна" in query_lower:
        return "ua"
    if "беларусь" in query_lower or "белоруссия" in query_lower:
        return "by"
    if "казахстан" in query_lower:
        return "kz"
    return "us"  # Default to US


def _guess_lang(query_text: str) -> str:
    """Guess the language from the query text."""
    # Simple keyword-based guessing
    query_lower = query_text.lower()
    if any(lang in query_lower for lang in ["апельсин", "банан", "яблоко", "молоко"]):
        return "ru"
    return "en"  # Default to English


def _extract_country(query_text: str) -> str:
    """Extract country from query if specified like 'apple in germany'."""
    match = re.search(r"in\s+([a-zA-Z]+)$", query_text)
    if match:
        return match.group(1).lower()
    return _guess_country(query_text)


def _extract_lang(query_text: str) -> str:
    """Extract language from query if specified like 'apple in french'."""
    match = re.search(r"in\s+([a-zA-Z]+)$", query_text)
    if match:
        lang_map = {"french": "fr", "german": "de", "spanish": "es", "russian": "ru"}
        return lang_map.get(match.group(1).lower(), _guess_lang(query_text))
    return _guess_lang(query_text)


def _extract_ml(query_text: str) -> str:
    """Extract preferred unit (ml or g) from query."""
    if "ml" in query_text.lower():
        return "ml"
    return "g"


def _extract_unit(query_text: str) -> Optional[str]:
    """Extract unit like 'cup', 'oz', 'tbsp' from query."""
    units = ["cup", "oz", "tbsp", "tsp", "lb", "kg", "g", "ml", "l"]
    for unit in units:
        if unit in query_text.lower():
            return unit
    return None


def _guess_category(query_text: str) -> str:
    """Guess the food category from the query text."""
    query_lower = query_text.lower()
    if any(word in query_lower for word in ["apple", "pear", "banana", "orange", "grape", "strawberry", "fruit"]):
        return "Fruits"
    if any(word in query_lower for word in ["carrot", "broccoli", "spinach", "potato", "tomato", "vegetable"]):
        return "Vegetables"
    if any(word in query_lower for word in ["chicken", "beef", "pork", "fish", "meat", "egg"]):
        return "Meat & Poultry"
    if any(word in query_lower for word in ["milk", "cheese", "yogurt", "butter"]):
        return "Dairy"
    if any(word in query_lower for word in ["bread", "rice", "pasta", "cereal", "flour", "oats"]):
        return "Grains & Pasta"
    if any(word in query_lower for word in ["oil", "butter", "sugar", "salt", "spice", "sauce", "dressing"]):
        return "Oils & Fats"
    if any(word in query_lower for word in ["water", "juice", "tea", "coffee", "soda", "drink"]):
        return "Beverages"
    return "Unknown"


def _norm_text(text: str) -> str:
    """Normalize text for searching."""
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)  # Remove punctuation
    text = re.sub(r"\s+", " ", text).strip()  # Normalize whitespace
    return text


async def _google_cse_search_branded(query: str, num: int = 10) -> List[Dict[str, Any]]:
    """Perform a Google Custom Search for branded products."""
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
    """Perform a Google Custom Search for images and parse results."""
    results = await _google_cse_search(query, search_type="image", num=1)
    if not results:
        return []
    image_url = results[0].get("link")
    if image_url:
        return await _parse_vision_ocr(image_url, referer_url, title, grams, milli_l)
    return []


# Example usage (assuming you have the necessary imports and setup)
# async def main():
#     results = await search("apple")
#     print(json.dumps(results, indent=2))
#
# if __name__ == "__main__":
#     asyncio.run(main())