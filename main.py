# healco lite (v1.2) — монетизация, мотивашки, рейтинг
# Требуемые пакеты в requirements.txt:
# python-telegram-bot==21.4
# openai==1.40.2
# pydantic==2.7.4
# pydantic-core==2.18.4
# aiohttp==3.9.5
# python-dotenv==1.0.1

import os
import re
import json
import asyncio
import logging
import random
import requests
import httpx
import fcntl
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
from requests_oauthlib import OAuth1
from bs4 import BeautifulSoup

from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI

# ========= ЛОГИ =========
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()],
)
logger = logging.getLogger("healco-lite")

# Импортируем Open Food Facts модуль
try:
    from openfood import off_by_barcode, off_search_by_name, set_user_agent
    # Настраиваем User-Agent для Open Food Facts
    set_user_agent("HealCoLite/1.2", "rafael.sayadi@gmail.com")
    HAS_OPENFOOD = True
except ImportError as e:
    logger.warning(f"Open Food Facts module not available: {e}")
    HAS_OPENFOOD = False

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    KeyboardButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
)
from telegram.ext import (
    Application,  # используем Application.builder() (PTB v21)
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    PreCheckoutQueryHandler,
)

LOCK_PATH = "/tmp/healco_bot.lock"

@contextmanager
def single_instance_lock():
    """Не даём запуститься второму poller'у — иначе Telegram 409 Conflict."""
    fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except BlockingIOError:
        print("Another bot instance is already running. Exit.")
        raise SystemExit(0)
    finally:
        # замок держим до завершения процесса; файл оставляем
        pass

def log_egress_ip_once():
    try:
        ip = requests.get("https://api.ipify.org", timeout=5).text
        logger.info("Public egress IP: %s", ip)
    except Exception as e:
        logger.info("Public egress IP: <unknown> (%s)", e)

VERSION = "healco lite v1.2"
PROJECT_NAME = "Healco Lite v1.2"
MODEL_NAME = "gpt-4o-mini"

# ========= ENV =========
load_dotenv()

# Функция для безопасного получения секретов из Replit Secrets
def get_secret(key: str, default: str = "") -> str:
    """Получает секрет из Replit Secrets с fallback на переменные окружения"""
    # Прямой fallback на переменные окружения
    return os.getenv(key, default)

OPENAI_API_KEY = get_secret("OPENAI_API_KEY", "")
BOT_TOKEN = get_secret("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_PAYMENT_PROVIDER_TOKEN = get_secret("TELEGRAM_PAYMENT_PROVIDER_TOKEN", "")
DEVELOPER_USER_ID = int(get_secret("DEVELOPER_USER_ID", "0").split(",")[0]) if get_secret("DEVELOPER_USER_ID", "0").split(",")[0].isdigit() else 0

# Admin users list - stored in database
def get_admin_users() -> List[int]:
    """Get list of admin user IDs"""
    admins = db_get("admin_users", [])
    if isinstance(admins, list):
        return [int(uid) for uid in admins if str(uid).isdigit()]
    return []

def add_admin_user(user_id: int) -> bool:
    """Add user to admin list"""
    admins = get_admin_users()
    if user_id not in admins:
        admins.append(user_id)
        db_set("admin_users", admins)
        return True
    return False

def remove_admin_user(user_id: int) -> bool:
    """Remove user from admin list"""
    admins = get_admin_users()
    if user_id in admins:
        admins.remove(user_id)
        db_set("admin_users", admins)
        return True
    return False

# ========= ПЛАТЁЖИ / ЦЕНЫ =========
PRICE_BASIC = 100       # Stars
PRICE_PREMIUM = 250     # Stars
PRICE_MAXIMUM = 500     # Stars
PRICE_MOTIVATION = 1    # Stars
FREE_DIARY_LIMIT = 2    # Лимит записей в free

# ========= DB =========
try:
    from replit import db as replit_db
    HAS_REPLIT = True
except Exception:
    HAS_REPLIT = False

# === НАСТРОЙКИ/КЛЮЧИ ===
USE_JSONL = os.getenv("USE_JSONL","0") == "1"   # по умолчанию НЕ грузим дампы
DISABLE_LOCAL_DB = True  # ← принудительно выключаем любые локальные БД
GOOGLE_CSE_KEY = get_secret("GOOGLE_CSE_KEY", "")
GOOGLE_CSE_CX  = get_secret("GOOGLE_CSE_CX", "")
VISION_KEY     = get_secret("VISION_KEY", "")        # опционально
USDA_API_KEY   = get_secret("USDA_FDC_API_KEY", "cOQTpuHzZ2aOOpixNXoi8f5n94nEu5RvRoGf3o88")

class LocalDB:
    def __init__(self, path="db.json"):
        self.path = path
        if not os.path.exists(self.path):
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({}, f, ensure_ascii=False, indent=2)
        self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.store = json.load(f)
        except Exception:
            self.store = {}

    def _save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.store, f, ensure_ascii=False, indent=2)

    def __getitem__(self, k):
        return self.store.get(k)

    def __setitem__(self, k, v):
        self.store[k] = v
        self._save()

    def __contains__(self, k):
        return k in self.store

    def keys(self):
        return list(self.store.keys())

local_db = LocalDB(os.environ.get("HLITE_DB_PATH", "db.json"))

def db_get(k, default=None):
    if HAS_REPLIT:
        try:
            return replit_db.get(k, default)
        except Exception:
            # Если Replit DB недоступна, используем локальную
            return local_db.store.get(k, default)
    # Если Replit недоступен, используем локальную
    return local_db.store.get(k, default)

def db_set(k, v):
    if HAS_REPLIT:
        try:
            replit_db[k] = v
            return
        except Exception:
            # Если Replit DB недоступна, записываем в локальную
            pass
    # Записываем в локальную БД
    local_db[k] = v

def db_keys_prefix(prefix: str) -> List[str]:
    try:
        # Пытаемся получить ключи из Replit DB, если доступно
        src = replit_db.keys() if HAS_REPLIT else local_db.keys()
        return [k for k in src if str(k).startswith(prefix)]
    except Exception:
        # Если Replit DB недоступна, возвращаем ключи из локальной БД
        return [k for k in local_db.keys() if str(k).startswith(prefix)]

# ========= КНОПКИ =========
MAIN_MENU = [
    [KeyboardButton("🥗 Нутрициолог"), KeyboardButton("🏋️ Фитнес-тренер")],
    [KeyboardButton("🍏 ПП‑рецепты"), KeyboardButton("📒 Мои дневники")],
    [KeyboardButton("🏆 Мои баллы"), KeyboardButton("⭐ Магазин")],
    [KeyboardButton("🛠 Обновить профиль")],
]
NUTRI_MENU = [
    [KeyboardButton("🍽️ Сгенерировать меню"), KeyboardButton("🔄 Изменить меню")],
    [KeyboardButton("📊 КБЖУ"), KeyboardButton("📏 ИМТ (BMI)")],
    [KeyboardButton("🍏 Обновить дневник"), KeyboardButton("🔍 Поиск продуктов")],
    [KeyboardButton("Задать вопрос ❓"), KeyboardButton("⭐ Получить мотивашку от нутрициолога")],
    [KeyboardButton("⬅️ Назад")],
]
TRAINER_MENU = [
    [KeyboardButton("📋 Сгенерировать план тренировки"), KeyboardButton("🔄 Изменить план")],
    [KeyboardButton("➕ Внести тренировку"), KeyboardButton("📈 Пульсовые зоны")],
    [KeyboardButton("🫁 МПК (VO2max)"), KeyboardButton("Задать вопрос ❓")],
    [KeyboardButton("⭐ Получить мотивашку от тренера")],
    [KeyboardButton("⬅️ Назад")],
]
LOCATION_KB = [[KeyboardButton("Дом"), KeyboardButton("Зал"), KeyboardButton("Улица")]]
ACTIVITY_KB = [[KeyboardButton("Низкая"), KeyboardButton("Умеренная"), KeyboardButton("Высокая")]]
GOAL_KB = [[KeyboardButton("Набрать массу"), KeyboardButton("Похудеть"), KeyboardButton("Поддерживать вес")]]
GENDER_KB = [[KeyboardButton("Женский"), KeyboardButton("Мужской")]]

def yes_no_kb(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Да", callback_data=f"{key}:yes"),
          InlineKeyboardButton("Нет", callback_data=f"{key}:no")]]
    )

# ---------- эвристики по категориям (правдоподобие) ----------
CATEGORY_RULES = {
  # сладкое / снеки
  "chocolate":      {"kcal_100g": (450, 650), "fat_100g": (25, 50), "carbs_100g": (30, 70)},
  "protein_bar":    {"kcal_100g": (300, 500), "protein_100g": (20, 50)},
  "candy":          {"kcal_100g": (300, 600), "fat_100g": (0, 25),  "carbs_100g": (60, 98)},
  "cookies":        {"kcal_100g": (380, 550), "fat_100g": (10, 35), "carbs_100g": (45, 75)},
  "chips":          {"kcal_100g": (450, 600), "fat_100g": (25, 45), "carbs_100g": (35, 60)},
  "granola":        {"kcal_100g": (380, 520), "fat_100g": (8, 25),  "carbs_100g": (50, 75)},

  # орехи/семечки/масла
  "nuts":           {"kcal_100g": (520, 720), "fat_100g": (40, 75), "protein_100g": (10, 30)},
  "seeds":          {"kcal_100g": (450, 700), "fat_100g": (30, 65), "protein_100g": (15, 35)},
  "nut_butter":     {"kcal_100g": (550, 700), "fat_100g": (40, 70), "protein_100g": (15, 30)},
  "oil":            {"kcal_100g": (800, 900), "fat_100g": (99, 100)},

  # молочка (твёрдые/ложкообразные — проверяем на 100 г; напитки — можно и на 100 мл)
  "yogurt":         {"kcal_100g": (40, 120),  "protein_100g": (2, 12),  "fat_100g": (0, 8)},
  "kefir":          {"kcal_100g": (35, 90),   "protein_100g": (2, 4),   "fat_100g": (0, 4)},
  "milk":           {"kcal_100ml": (35, 90),  "fat_100ml": (0, 6)},
  "cheese_hard":    {"kcal_100g": (280, 450), "protein_100g": (18, 35), "fat_100g": (18, 38)},
  "cottage_cheese": {"kcal_100g": (70, 220),  "protein_100g": (12, 22), "fat_100g": (0, 18)},

  # мясо/колбасы и хлеб
  "sausage":        {"kcal_100g": (180, 420), "protein_100g": (10, 22), "fat_100g": (10, 38)},
  "bread":          {"kcal_100g": (200, 320), "protein_100g": (5, 12),  "carbs_100g": (35, 60)},

  # заморозка/десерты
  "ice_cream":      {"kcal_100g": (150, 350), "fat_100g": (5, 25),  "carbs_100g": (15, 45)},

  # напитки
  "soda":           {"kcal_100ml": (0, 60),   "carbs_100ml": (0, 15)},
  "energy_drink":   {"kcal_100ml": (0, 60),   "carbs_100ml": (0, 15)},
  "juice":          {"kcal_100ml": (35, 70),  "carbs_100ml": (8, 18)},

  # крупы/готовые гарниры
  "cereal_flakes":  {"kcal_100g": (320, 420), "protein_100g": (6, 14),  "carbs_100g": (60, 80)},
  "pasta_cooked":   {"kcal_100g": (100, 180), "protein_100g": (3, 7),   "carbs_100g": (18, 32)},
  "rice_cooked":    {"kcal_100g": (90, 150),  "protein_100g": (1.5, 3.5),"carbs_100g": (18, 33)},
  "buckwheat_cooked":{"kcal_100g": (90, 150), "protein_100g": (3, 6),   "carbs_100g": (15, 30)},
  "oatmeal_cooked": {"kcal_100g": (60, 120),  "protein_100g": (2, 5),   "carbs_100g": (10, 20)},

  # соусы/подсластители
  "mayo":           {"kcal_100g": (500, 750), "fat_100g": (50, 85)},
  "ketchup":        {"kcal_100g": (60, 140),  "carbs_100g": (10, 35)},
  "soy_sauce":      {"kcal_100g": (40, 90),   "protein_100g": (5, 12)},
  "jam_honey":      {"kcal_100g": (250, 340), "carbs_100g": (60, 90)},
}

def _plausible(res: dict, cat: str | None) -> bool:
    """Проверяет правдоподобность питательных данных по категории"""
    if not cat or cat not in CATEGORY_RULES:
        return True
    
    rules = CATEGORY_RULES[cat]
    
    # Проверяем каждое правило
    for key, (min_val, max_val) in rules.items():
        value = res.get(key)
        if value is not None and not (min_val <= value <= max_val):
            return False
    
    return True

def get_typical_nutrition(product_name: str) -> Optional[Dict[str, Any]]:
    """Возвращает типичные питательные данные для популярных продуктов"""
    name_lower = product_name.lower()
    
    # Молочный шоколад
    if any(word in name_lower for word in ['молочный шоколад', 'milk chocolate']):
        return {
            'name': 'Молочный шоколад',
            'kcal_100g': 534,
            'protein_100g': 8.0,
            'fat_100g': 30.0,
            'carbs_100g': 57.0,
            'source': 'typical_values'
        }
    
    # Темный шоколад
    if any(word in name_lower for word in ['темный шоколад', 'dark chocolate', 'горький шоколад']):
        return {
            'name': 'Темный шоколад',
            'kcal_100g': 546,
            'protein_100g': 7.8,
            'fat_100g': 31.3,
            'carbs_100g': 48.2,
            'source': 'typical_values'
        }
    
    # Йогурт
    if 'йогурт' in name_lower or 'yogurt' in name_lower:
        return {
            'name': 'Йогурт',
            'kcal_100g': 63,
            'protein_100g': 5.0,
            'fat_100g': 1.5,
            'carbs_100g': 7.0,
            'source': 'typical_values'
        }
    
    return None

def _guess_category(q: str) -> str | None:
  s = q.lower()
  if any(w in s for w in ("шоколад", "chocolate")):                    return "chocolate"
  if any(w in s for w in ("батончик", "батонч", "protein bar", "bar")): return "protein_bar"
  if any(w in s for w in ("конфет", "candy", "ирис", "мармелад")):       return "candy"
  if any(w in s for w in ("печень", "cookie", "печиво", "галет")):      return "cookies"
  if any(w in s for w in ("чипс", "chips", "crisp")):                   return "chips"
  if any(w in s for w in ("гранол", "granola", "мюсли")):               return "granola"

  if any(w in s for w in ("орех", "nuts", "миндаль", "фундук", "грецк")): return "nuts"
  if any(w in s for w in ("семеч", "льнян", "кунжут", "тыкв", "seeds")):  return "seeds"
  if any(w in s for w in ("пастарахис", "арахисовая паста", "peanut butter", "almond butter")): return "nut_butter"
  if any(w in s for w in ("масло", "oil", "olive oil", "подсолнеч")):     return "oil"

  if any(w in s for w in ("йогурт", "yogurt", "йогур")):                 return "yogurt"
  if any(w in s for w in ("кефир", "kefir")):                            return "kefir"
  if any(w in s for w in ("молоко", "milk", "lactose-free")):            return "milk"
  if any(w in s for w in ("творог", "cottage cheese", "quark")):         return "cottage_cheese"
  if any(w in s for w in ("сыр", "cheese")) and "cottage" not in s:      return "cheese_hard"

  if any(w in s for w in ("колбас", "сосиск", "сардел", "sausage")):     return "sausage"
  if any(w in s for w in ("хлеб", "bread", "булк")):                     return "bread"

  if any(w in s for w in ("морож", "ice cream", "gelato")):              return "ice_cream"

  if any(w in s for w in ("газиров", "сода", "cola", "fanta", "sprite")):  return "soda"
  if any(w in s for w in ("энергет", "energy drink", "red bull", "monster")): return "energy_drink"
  if any(w in s for w in ("сок", "juice", "нектар")):                    return "juice"

  if any(w in s for w in ("хлопья", "flakes", "corn flakes", "cereal")): return "cereal_flakes"
  if any(w in s for w in ("паста", "макарон", "spaghetti", "penne")):    return "pasta_cooked"
  if any(w in s for w in ("рис", "rice")) and "варен" in s:              return "rice_cooked"
  if any(w in s for w in ("гречк", "buckwheat")) and "варен" in s:       return "buckwheat_cooked"
  if any(w in s for w in ("овсян", "oatmeal", "каша")) and "варен" in s: return "oatmeal_cooked"

  if any(w in s for w in ("майон", "mayo", "майонез")):                  return "mayo"
  if any(w in s for w in ("кетчуп", "ketchup")):                         return "ketchup"
  if any(w in s for w in ("соев", "soy sauce")):                         return "soy_sauce"
  if any(w in s for w in ("варень", "джем", "мёд", "мед", "honey", "jam")): return "jam_honey"

  return None

# ========= УТИЛИТЫ =========
def sanitize_ai(text: str) -> str:
    if not text:
        return text

    # Убираем markdown символы, но оставляем решетки если они в начале строки (заголовки)
    cleaned = re.sub(r"[*_`]+", "", text)

    # Убираем решетки только если они используются как markdown заголовки
    cleaned = re.sub(r"^#{1,6}\s*", "", cleaned, flags=re.MULTILINE)

    # Подсчитываем существующие смайлики
    emoji_count = len(re.findall(r"[😀-🙏🌟-🧩🔥💪🍏🥦🏃‍♂️🏃‍♀️🧘‍♂️🧘‍♀️🫡🤝✅❗️⚠️🤔❤️🙂👍👏🚀🍽️📊🫀]", cleaned))
    text_length = len(cleaned)
    max_emojis = max(1, text_length // 1000)  # Не более 1 смайла на 1000 символов

    if emoji_count >= max_emojis:
        return cleaned

    # Добавляем смайлики только к строкам с итогами
    def add_emoji(line: str) -> str:
        nonlocal emoji_count, max_emojis
        if not line.strip() or emoji_count >= max_emojis:
            return line
        if re.search(r"[😀-🙏🌟-🧩🔥💪🍏🥦🏃‍♂️🏃‍♀️🧘‍♂️🧘‍♀️🫡🤝✅❗️⚠️🤔❤️🙂👍👏🚀🍽️📊🫀]", line):
            return line
        # Добавляем смайлик ТОЛЬКО к строкам с итогами
        if any(word in line.lower() for word in ["итого", "итого за день", "итого за неделю"]):
            emoji_count += 1
            return line + (" 🍽️" if "день" in line.lower() else " 💪" if "неделю" in line.lower() else " 📊")
        return line

    lines = [add_emoji(l) for l in cleaned.split("\n")]
    return "\n".join(lines)

def role_keyboard(role: Optional[str]) -> ReplyKeyboardMarkup:
    if role == "nutri":
        return ReplyKeyboardMarkup(NUTRI_MENU, resize_keyboard=True)
    if role == "trainer":
        return ReplyKeyboardMarkup(TRAINER_MENU, resize_keyboard=True)
    return ReplyKeyboardMarkup(MAIN_MENU, resize_keyboard=True)

def now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def today_key() -> str:
    return datetime.now().strftime("%Y-%m-%d")

def state_key(uid: int) -> str:
    return f"user:{uid}"

def default_state() -> Dict[str, Any]:
    return {
        "profile": {
            "gender": None,
            "age": None,
            "height_cm": None,
            "weight_kg": None,
            "activity": None,
            "goal": None,
            "allergies": "",
            "conditions": "",
            "injuries": "",
            "preferences": {"menu_notes": "", "workout_notes": ""},
        },
        "diaries": {"food": [], "train": [], "metrics": []},
        "daily_energy": {},
        "awards": {},
        "points": 0,
        "access_level": "free",  # free/basic/premium/maximum
        "current_role": None,
        "awaiting": None,
        "tmp": {},
    }

def load_state(uid: int) -> Dict[str, Any]:
    s = db_get(state_key(uid))
    if not s:
        s = default_state()
        db_set(state_key(uid), s)
    s.setdefault("profile", {}).setdefault("preferences", {})
    s.setdefault("diaries", {"food": [], "train": [], "metrics": []})
    s.setdefault("daily_energy", {})
    s.setdefault("awards", {})
    s.setdefault("points", 0)
    s.setdefault("access_level", "free")
    s.setdefault("tmp", {})
    return s

def save_state(uid: int, s: Dict[str, Any]):
    db_set(state_key(uid), s)

def is_developer(user_id: int) -> bool:
    return user_id == DEVELOPER_USER_ID

def is_admin_user(user_id: int) -> bool:
    return user_id in get_admin_users()

def has_full_access(user_id: int) -> bool:
    return is_developer(user_id) or is_admin_user(user_id)

def get_user_access(state: Dict[str, Any], user_id: int) -> str:
    if has_full_access(user_id):
        return "maximum"
    return state.get("access_level", "free")

def check_feature_access(state: Dict[str, Any], user_id: int, feature: str) -> bool:
    level = get_user_access(state, user_id)
    if level == "maximum":
        return True
    if feature == "diaries":
        return level in ["basic", "premium", "maximum"]
    if feature == "recipes":
        return level in ["premium", "maximum"]
    if feature == "calories_ai":
        return level in ["premium", "maximum"]
    if feature == "analytics":
        return level in ["maximum"]
    return False

def add_points(st: Dict[str, Any], amount: int) -> int:
    st["points"] = int(st.get("points", 0)) + int(amount)
    return st["points"]

def ensure_day(s: Dict[str, Any]) -> Dict[str, int]:
    dk = today_key()
    s["daily_energy"].setdefault(dk, {"in": 0, "out": 0})
    return s["daily_energy"][dk]

def add_kcal_in(st: Dict[str, Any], kcal: int):
    day = ensure_day(st)
    day["in"] += int(max(0, kcal))

def add_kcal_out(st: Dict[str, Any], kcal: int):
    day = ensure_day(st)
    day["out"] += int(max(0, kcal))

def day_totals(st: Dict[str, Any]) -> Tuple[int, int]:
    d = st["daily_energy"].get(today_key(), {"in": 0, "out": 0})
    return d.get("in", 0), d.get("out", 0)

def award_once(st: Dict[str, Any], metric: str) -> bool:
    d = today_key()
    st["awards"].setdefault(d, {})
    if st["awards"][d].get(metric):
        return False
    st["awards"][d][metric] = True
    return True

def profile_complete(p: Dict[str, Any]) -> bool:
    return all(
        [
            p.get("gender") in ("Женский", "Мужской"),
            isinstance(p.get("age"), int) and 10 <= p["age"] <= 100,
            isinstance(p.get("height_cm"), int) and 100 <= p["height_cm"] <= 250,
            isinstance(p.get("weight_kg"), (int, float)) and 30 <= float(p["weight_kg"]) <= 300,
            p.get("activity") in ("Низкая", "Умеренная", "Высокая"),
            p.get("goal") in ("Набрать массу", "Похудеть", "Поддерживать вес"),
        ]
    )

# ========= КАЛЬКУЛЯТОРЫ =========
def calc_bmi(w_kg: float, h_cm: int):
    h = h_cm / 100.0
    bmi = w_kg / (h * h)
    if bmi < 18.5:
        cat = "Недостаточная масса"
    elif bmi < 25:
        cat = "Норма"
    elif bmi < 30:
        cat = "Избыточная масса"
    else:
        cat = "Ожирение"
    return round(bmi, 1), cat

def mifflin_st_jeor(g: str, age: int, h: int, w: float) -> float:
    return 10 * w + 6.25 * h - 5 * age + (5 if g == "Мужской" else -161)

def activity_multiplier_profile(level: str) -> float:
    mapping = {"Низкая": 1.2, "Умеренная": 1.55, "Высокая": 1.725}
    return mapping.get(level, 1.2)

def recommend_deficit_percent(bmi: float) -> int:
    if bmi >= 35:
        return 25
    if bmi >= 30:
        return 22
    if bmi >= 27:
        return 20
    if bmi >= 25:
        return 17
    return 15

def calc_kbju_weight_loss(profile: Dict[str, Any]) -> Dict[str, Any]:
    g, age, h, w = profile["gender"], profile["age"], profile["height_cm"], float(profile["weight_kg"])
    goal = profile.get("goal", "Поддерживать вес")

    bmr = mifflin_st_jeor(g, age, h, w)
    tdee = bmr * activity_multiplier_profile(profile["activity"])
    bmi, bmi_cat = calc_bmi(w, h)

    # Расчет целевых показателей в зависимости от цели
    if goal == "Похудеть":
        deficit_pct = recommend_deficit_percent(bmi)
        deficit_kcal = int(round(tdee * deficit_pct / 100.0))
        target = max(int(round(tdee - deficit_kcal)), int(round(bmr)))
        protein_multiplier = 1.8  # Повышенный белок для сохранения мышц при похудении

        # Рекомендации по темпу похудения
        weekly_loss = deficit_kcal * 7 / 7700  # кг в неделю (7700 ккал = 1 кг жира)
        recommendations = [
            f"Безопасный темп похудения: {weekly_loss:.1f} кг в неделю",
            "Обязательно употребляйте достаточно белка для сохранения мышечной массы",
            "Включайте силовые тренировки 2-3 раза в неделю"
        ]
    elif goal == "Набрать массу":
        surplus_kcal = int(round(tdee * 0.10))  # 10% профицит
        target = int(round(tdee + surplus_kcal))
        protein_multiplier = 2.0  # Высокий белок для роста мышц
        deficit_pct = -10  # Показываем профицит как отрицательный дефицит
        deficit_kcal = -surplus_kcal

        weekly_gain = surplus_kcal * 7 / 7700  # кг в неделю
        recommendations = [
            f"Целевой набор веса: {weekly_gain:.1f} кг в неделю",
            "Употребляйте белок каждые 3-4 часа",
            "Обязательны силовые тренировки для качественного набора мышечной массы"
        ]
    else:  # Поддерживать вес
        target = int(round(tdee))
        protein_multiplier = 1.6
        deficit_pct = 0
        deficit_kcal = 0

        recommendations = [
            "Поддерживайте стабильный вес через сбалансированное питание",
            "Включайте разнообразные тренировки для поддержания формы",
            "Контролируйте вес еженедельно"
        ]

    protein_g = int(round(protein_multiplier * w))
    fat_kcal = 0.25 * target  # 25% от калорий на жиры
    fat_g = int(round(fat_kcal / 9.0))
    carbs_kcal = max(0, target - (protein_g * 4 + fat_kcal))
    carbs_g = int(round(carbs_kcal / 4.0))

    # Микронутриенты на основе данных USDA
    micronutrient_needs = get_micronutrient_recommendations(g, age, goal)

    return {
        "bmr": int(round(bmr)),
        "tdee": int(round(tdee)),
        "deficit_pct": deficit_pct,
        "deficit_kcal": deficit_kcal,
        "target_kcal": target,
        "protein_g": protein_g,
        "fat_g": fat_g,
        "carbs_g": carbs_g,
        "bmi": bmi,
        "bmi_category": bmi_cat,
        "goal": goal,
        "recommendations": recommendations,
        "micronutrients": micronutrient_needs,
        "note": "Расчеты основаны на данных USDA FDC и научных исследованиях. Итоговую калорийность не опускайте ниже BMR.",
    }

def get_micronutrient_recommendations(gender: str, age: int, goal: str) -> Dict[str, str]:
    """Рекомендации по микронутриентам на основе данных USDA"""
    recs = {}

    if gender == "Мужской":
        recs["iron"] = "8 мг/день"
        recs["calcium"] = "1000 мг/день" if age < 70 else "1200 мг/день"
        recs["vitamin_d"] = "15 мкг/день" if age < 70 else "20 мкг/день"
    else:  # Женский
        recs["iron"] = "18 мг/день" if age < 51 else "8 мг/день"
        recs["calcium"] = "1000 мг/день" if age < 51 else "1200 мг/день"
        recs["vitamin_d"] = "15 мкг/день" if age < 70 else "20 мкг/день"

    # Общие для всех
    recs["vitamin_c"] = "90 мг/день" if gender == "Мужской" else "75 мг/день"
    recs["magnesium"] = "400-420 мг/день" if gender == "Мужской" else "310-320 мг/день"
    recs["omega3"] = "1.6 г/день" if gender == "Мужской" else "1.1 г/день"

    if goal == "Похудеть":
        recs["note"] = "При похудении особенно важны железо, кальций и витамины группы B"
    elif goal == "Набрать массу":
        recs["note"] = "Для набора мышечной массы увеличьте потребление магния, цинка и креатина"

    return recs

def vo2_category(g: str, vo2: float) -> str:
    if g == "Мужской":
        return "Низкий" if vo2 < 35 else "Ниже среднего" if vo2 < 43 else "Средний" if vo2 < 51 else "Выше среднего" if vo2 < 58 else "Высокий"
    return "Низкий" if vo2 < 28 else "Ниже среднего" if vo2 < 35 else "Средний" if vo2 < 42 else "Выше среднего" if vo2 < 49 else "Высокий"

def pulse_zones(age: int, hr_rest: int = 60) -> Dict[str, Tuple[int, int]]:
    hr_max = 208 - 0.7 * age
    hrr = hr_max - hr_rest

    def rng(a, b):
        lo = int(round(hr_rest + a * hrr))
        hi = int(round(hr_rest + b * hrr))
        return (min(lo, hi), max(lo, hi))

    return {
        "recovery": rng(0.50, 0.60),
        "aerobic": rng(0.60, 0.70),
        "tempo": rng(0.70, 0.80),
        "vo2": rng(0.80, 0.90),
        "anaer": rng(0.90, 1.00),
    }

def zones_text(z: Dict[str, Tuple[int, int]]) -> str:
    return (
        f"Восстановление {z['recovery'][0]}–{z['recovery'][1]}, "
        f"Аэробная {z['aerobic'][0]}–{z['aerobic'][1]}, "
        f"Темповая {z['tempo'][0]}–{z['tempo'][1]}, "
        f"VO2max {z['vo2'][0]}–{z['vo2'][1]}, "
        f"Анаэробная {z['anaer'][0]}–{z['anaer'][1]} уд/мин"
    )

# ========= ИИ-НОРМАЛИЗАЦИЯ ЗАПРОСОВ =========
_SYSTEM_PROMPT = """You are a strict nutrition query normalizer. Output valid JSON ONLY.
Schema: {"clean_text_original": "string", "portion_grams": null, "portion_ml": null,
"query_type":"brand|natural|unknown","brand_text":null,"base_en":null,"method_en":null,
"usda_queries":[],"brand_queries":[]}
Rules:
- Extract grams/ml anywhere in text: (\\d+(?:[.,]\\d+)?)\\s*(г|гр|g|grams?|кг|kg|мл|ml|л|l|литр(?:а|ов)?) ; sum if multiple; remove tokens from text.
- RU→EN bases for natural: chicken breast, chicken, turkey, beef, pork, salmon, tuna, bulgur, buckwheat, rice, oat, barley, quinoa, milk, kefir, yogurt, whey.
- Methods: grilled,fried,boiled,roasted,stewed,smoked.
- If brand cues (latin brand words, UPC/EAN 8-14 digits, tokens like protein/pancake/bar/bombbar/danone etc.): query_type=brand.
- For natural: base_en must exist; make usda_queries in priority: "base_en [method] cooked", "base_en cooked", "base_en".
- No nutrition values in output. JSON only."""

_FEWSHOTS = [
    ("Жареная куриная грудка 120г", {
        "portion_grams": 120, "query_type":"natural",
        "base_en":"chicken breast","method_en":"fried",
        "usda_queries":["chicken breast fried cooked","chicken breast cooked","chicken breast"],
        "brand_queries":[]
    }),
    ("булгур варёный 112г", {
        "portion_grams": 112, "query_type":"natural",
        "base_en":"bulgur","method_en":"boiled",
        "usda_queries":["bulgur boiled cooked","bulgur cooked","bulgur"],
        "brand_queries":[]
    }),
    ("Bombbar protein pancake 40г", {
        "portion_grams": 40, "query_type":"brand",
        "brand_text":"bombbar protein pancake",
        "usda_queries":[], "brand_queries":["bombbar protein pancake","protein pancake bombbar"]
    }),
    ("на гриле 200г", {
        "portion_grams": 200, "query_type":"unknown",
        "usda_queries":[], "brand_queries":[]
    }),
]

def _heuristic_normalize(text: str) -> dict:
    """Запасной путь, если ИИ недоступен: простая евристика."""
    s = text.strip()
    # граммы
    m = re.search(r'(\d+(?:[.,]\d+)?)\s*(?:г|гр|g|grams?)\b', s, re.I)
    grams = float(m.group(1).replace(',', '.')) if m else None
    if m:
        s = (s[:m.start()] + s[m.end():]).strip()

    brand_hints = {"bombbar","danone","activia","nestle","milka","protein","pancake","bar","snickers","mars","йогурт","творожок","батончик"}
    if re.search(r"\b\d{8,14}\b", s) or any(h in s.lower() for h in brand_hints):
        return {
            "clean_text_original": text, "portion_grams": grams, "query_type":"brand",
            "brand_text": s, "base_en": None, "method_en": None,
            "skinless": None, "usda_queries": [], "brand_queries":[s]
        }

    cook_map = {
        r"\bна\s+грил[е|я]\b|\bгрилл?\b": "grilled",
        r"\bжарен(ая|ый|ое|ые)\b": "fried",
        r"\bварен(ая|ый|ое|ые)\b|\bотварн": "boiled",
        r"\bзапеченн": "roasted",
        r"\bтушен": "stewed",
        r"\bкопчен": "smoked",
    }
    method = next((en for rx,en in cook_map.items() if re.search(rx, " "+s.lower()+" ")), None)

    base_map = {
        r"\bкурин(ая|ый)\s+грудк": "chicken breast",
        r"\bкуриц": "chicken",
        r"\bиндейк": "turkey",
        r"\bговядин": "beef",
        r"\bсвин(ин|ина)": "pork",
        r"\bлосось|\bсемг": "salmon",
        r"\bтунец": "tuna",
        r"\bбулгур\b|\bbulgur\b": "bulgur",
        r"\bгречк": "buckwheat",
        r"\bрис\b": "rice",
        r"\bовсян": "oat",
        r"\bперловк": "barley",
        r"\bкиноа|\bquinoa\b": "quinoa",
        r"\bяблок": "apple",
        r"\bкартоф|картош": "potato",
        r"\bяйц": "egg",
    }
    base = next((en for rx,en in base_map.items() if re.search(rx, s.lower())), None)
    if base:
        queries = []
        if method:
            queries.append(f"{base} {method} cooked")
        queries.extend([f"{base} cooked", base])
        return {
            "clean_text_original": text, "portion_grams": grams, "query_type":"natural",
            "brand_text": None, "base_en": base, "method_en": method,
            "skinless": None, "usda_queries": queries, "brand_queries":[]
        }

    return {
        "clean_text_original": text, "portion_grams": grams, "query_type":"unknown",
        "brand_text": None, "base_en": None, "method_en": None, "skinless": None,
        "usda_queries": [], "brand_queries": []
    }

async def call_llm_normalizer(user_text: str) -> dict:
    """Если есть OPENAI_API_KEY — используем LLM, иначе евристику."""
    if not OPENAI_API_KEY:
        return _heuristic_normalize(user_text)

    messages = [{"role":"system","content":_SYSTEM_PROMPT}]
    # few-shot examples
    for u, js in _FEWSHOTS:
        messages.append({"role":"user","content":u})
        messages.append({"role":"assistant","content":json.dumps(js, ensure_ascii=False)})
    messages.append({"role":"user","content":user_text})

    try:
        if client:  # Используем существующий OpenAI клиент
            def _call_openai():
                return client.chat.completions.create(
                    model="gpt-4o",
                    temperature=0,
                    messages=messages,
                    timeout=20
                )
            response = await asyncio.to_thread(_call_openai)
            content = response.choices[0].message.content
            return json.loads(content)
    except Exception as e:
        logger.warning(f"LLM normalizer failed: {e}")
        return _heuristic_normalize(user_text)

def route_query_with_ai(info: dict, original_text: str) -> dict:
    """Роутинг на основе результатов ИИ-анализа"""
    # защита от «cooked» без базы
    if info.get("query_type") == "natural" and not info.get("base_en"):
        info["query_type"] = "unknown"
        info["usda_queries"] = []

    # итоговый роут
    if info.get("query_type") == "brand" and (info.get("brand_queries") or info.get("brand_text")):
        queries = info.get("brand_queries") or [info.get("brand_text")]
        return {"path":"brand", "queries": queries, "grams":info.get("portion_grams"), "base_en": None}

    if info.get("query_type") == "natural" and info.get("usda_queries"):
        return {"path":"usda", "queries": info["usda_queries"], "grams":info.get("portion_grams"), "base_en": info.get("base_en")}

    return {"path":"fallback", "queries":[original_text], "grams":info.get("portion_grams"), "base_en": None}

# ========= GOOGLE CUSTOM SEARCH API =========
# GOOGLE_CSE_KEY = get_secret("GOOGLE_CSE_KEY", "") # Перенесено выше
# GOOGLE_CSE_CX = get_secret("GOOGLE_CSE_CX", "")   # Перенесено выше
# VISION_KEY = get_secret("VISION_KEY", "")         # Перенесено выше

# === Лёгкий кэш (SQLite) для оптимизации поиска ===
import sqlite3
import base64
import time

CACHE_SCHEMA = "r4"  # ↑ поменяешь — старый кэш будет игнориться

os.makedirs("./data", exist_ok=True)
_con = sqlite3.connect("./data/cache.db")
_con.execute("""CREATE TABLE IF NOT EXISTS nutri_cache(
  key TEXT PRIMARY KEY,
  payload TEXT NOT NULL,
  last_used INTEGER NOT NULL,
  size_bytes INTEGER NOT NULL
)""")
_con.commit()

def _cache_get(k: str):
    r = _con.execute("SELECT payload FROM nutri_cache WHERE key=?", (k,)).fetchone()
    if not r:
        return None
    _con.execute("UPDATE nutri_cache SET last_used=? WHERE key=?", (int(time.time()), k))
    _con.commit()
    return json.loads(r[0])

def _cache_put(k: str, obj: dict, limit_mb: int = 50):
    data = json.dumps(obj, ensure_ascii=False)
    _con.execute("INSERT OR REPLACE INTO nutri_cache(key,payload,last_used,size_bytes) VALUES (?,?,?,?)",
                 (k, data, int(time.time()), len(data)))
    _con.commit()
    total = _con.execute("SELECT COALESCE(SUM(size_bytes),0) FROM nutri_cache").fetchone()[0] or 0
    while total > limit_mb*1024*1024:
        _con.execute("DELETE FROM nutri_cache WHERE key IN (SELECT key FROM nutri_cache ORDER BY last_used ASC LIMIT 50)")
        _con.commit()
        total = _con.execute("SELECT COALESCE(SUM(size_bytes),0) FROM nutri_cache").fetchone()[0] or 0

# ========================= УЛУЧШЕННЫЙ GOOGLE CSE ПОИСК =========================
def _extract_portions(text: str) -> Tuple[str, Optional[float], Optional[float]]:
    """
    Ищет все числа с единицами массы/объёма независимо от позиции:
    - г/гр/g/gram/grams, кг/kg
    - мл/ml, л/l/литр
    Возвращает (очищенный_текст, grams|None, ml|None).
    """
    s = text
    grams = None
    ml = None

    def _to_float(m):
        return float(m.replace(',', '.'))

    # соберём все матчи, потом удалим из строки
    matches = []
    for m in re.finditer(r'(\d+(?:[.,]\d+)?)\s*(кг|kg)\b', s, flags=re.I):
        grams = (grams or 0) + _to_float(m.group(1)) * 1000
        matches.append((m.start(), m.end()))
    for m in re.finditer(r'(\d+(?:[.,]\d+)?)\s*(?:г|гр|g|grams?)\b', s, flags=re.I):
        grams = (grams or 0) + _to_float(m.group(1))
        matches.append((m.start(), m.end()))

    # l / ml
    for m in re.finditer(r'(\d+(?:[.,]\d+)?)\s*(?:л|l|литр(?:а|ов)?)\b', s, flags=re.I):
        ml = (ml or 0) + _to_float(m.group(1)) * 1000
        matches.append((m.start(), m.end()))
    for m in re.finditer(r'(\d+(?:[.,]\d+)?)\s*(?:мл|ml|milliliter[s]?)\b', s, flags=re.I):
        ml = (ml or 0) + _to_float(m.group(1))
        matches.append((m.start(), m.end()))

    # вырезаем найденные фрагменты
    if matches:
        parts = []
        last = 0
        for a, b in sorted(matches):
            parts.append(s[last:a])
            last = b
        parts.append(s[last:])
        s = " ".join("".join(parts).split())

    return s.strip(), grams, ml

async def _google_cse_search_branded(q: str, num: int = 8) -> List[str]:
    """Optimized Google CSE search for branded products with targeted parameters"""
    if not GOOGLE_CSE_KEY or not GOOGLE_CSE_CX:
        logger.warning("Google CSE credentials not configured")
        return []
    
    try:
        # Extract brand tokens for exact matching
        brand_tokens = [t for t in re.split(r"[\s,]+", q) if len(t) > 2]
        exact = " ".join(brand_tokens[:4])  # короткая фраза в exactTerms
        or_terms = "калории|пищевая ценность|КБЖУ|nutrition facts|питательная ценность \"на 100 г\""
        domains = "site:ozon.ru OR site:wildberries.ru OR site:vkusvill.ru OR site:perekrestok.ru OR site:lenta.com OR site:5ka.ru OR site:metro-cc.ru OR site:auchan.ru"
        negative = "-inurl:questions -inurl:reviews -inurl:otzyv -inurl:forum"

        params = {
            "key": GOOGLE_CSE_KEY,
            "cx": GOOGLE_CSE_CX,
            "q": f"{domains} {negative}",
            "num": min(10, num),
            "exactTerms": exact,
            "orTerms": or_terms,
            "safe": "off",
            "lr": "lang_ru|lang_en",
            "hl": "ru"
        }
        
        logger.info(f"Google CSE branded search: '{exact}' with nutrition terms")
        
        async with httpx.AsyncClient(timeout=20.0) as cli:
            r = await cli.get("https://www.googleapis.com/customsearch/v1", params=params)
            r.raise_for_status()
            items = (r.json().get("items") or [])
        
        urls = [item["link"] for item in items if "link" in item]
        
        # Filter out unwanted subpages
        deny = ("/questions", "/reviews", "otzyv", "/forum")
        filtered_urls = [u for u in urls if not any(d in u for d in deny)]
        
        logger.info(f"Found {len(filtered_urls)} filtered URLs via optimized Google CSE")
        return filtered_urls
        
    except Exception as e:
        logger.warning(f"Google CSE branded search failed: {e}")
        return []

def _google_cse_search(q: str, num: int = 6, site_filter: str = None) -> List[str]:
    """Legacy Google Custom Search для получения URL (fallback)"""
    if not GOOGLE_CSE_KEY or not GOOGLE_CSE_CX:
        logger.warning("Google CSE credentials not configured")
        return []
    
    try:
        search_query = q + " калории белки жиры углеводы"
        if site_filter:
            search_query = f"{q} {site_filter}"
            
        logger.info(f"Google CSE legacy search: '{search_query}'")
        
        response = requests.get("https://www.googleapis.com/customsearch/v1",
                              params={"q": search_query,
                                     "key": GOOGLE_CSE_KEY,
                                     "cx": GOOGLE_CSE_CX,
                                     "num": num},
                              timeout=20)
        
        if response.status_code == 200:
            items = response.json().get("items", [])
            urls = [item["link"] for item in items if "link" in item]
            logger.info(f"Found {len(urls)} URLs via legacy Google CSE")
            return urls
        else:
            logger.warning(f"Google CSE returned status {response.status_code}")
            return []
            
    except Exception as e:
        logger.warning(f"Google CSE search failed: {e}")
        return []

def _abs_url(base: str, u: str) -> str:
    """Создает абсолютный URL из базового и относительного"""
    import urllib.parse as _urlparse
    return u if u.startswith("http") else _urlparse.urljoin(base, u)

def _pick_nutrition_images(html: str, base_url: str) -> list[str]:
    """Извлекает URL изображений с nutrition labels со страницы с умной сортировкой"""
    try:
        # Вытаскиваем <img ... src="..."> и альтернативные атрибуты
        imgs = re.findall(r'<img[^>]+(?:src|data-src|data-original)=["\']([^"\']+)["\']', html, flags=re.I)
        alts = re.findall(r'<img[^>]+alt=["\']([^"\']+)["\']', html, flags=re.I)
        
        # Создаем абсолютные URL
        urls = [_abs_url(base_url, u) for u in imgs]
        
        def score(u: str, alt: str = "") -> int:
            """Оценивает релевантность изображения для nutrition facts"""
            s = u.lower() + " " + alt.lower()
            keys = ("nutrition", "пищева", "кбжу", "питат", "энергети", "facts", 
                   "100г", "100 г", "100ml", "100 мл", "label", "ценность", "состав")
            return sum(k in s for k in keys)
        
        # Сопоставляем alt с URL (безопасно по длине списков)
        scored = [(urls[i], score(urls[i], alts[i] if i < len(alts) else "")) 
                 for i in range(len(urls))]
        
        # Сортируем по релевантности
        scored.sort(key=lambda x: x[1], reverse=True)
        
        # Возвращаем до 12 наиболее релевантных изображений
        return [u for u, _ in scored][:12]
        
    except Exception as e:
        logger.warning(f"Error picking nutrition images: {e}")
        return []

def _cand_score(candidate: dict, category: str = None) -> float:
    """Оценивает качество кандидата на основе полноты БЖУ и согласованности Атватера"""
    try:
        score = 0
        
        # Базовые баллы за наличие данных
        if candidate.get('kcal_100g') is not None:
            score += 10
        if candidate.get('protein_100g') is not None:
            score += 8
        if candidate.get('fat_100g') is not None:
            score += 8
        if candidate.get('carbs_100g') is not None:
            score += 8
            
        # Бонус за полноту БЖУ
        macro_count = sum(1 for key in ['protein_100g', 'fat_100g', 'carbs_100g'] 
                         if candidate.get(key) is not None)
        score += macro_count * 5
        
        # Проверка согласованности с формулой Атватера
        kcal = candidate.get('kcal_100g')
        protein = candidate.get('protein_100g', 0) or 0
        fat = candidate.get('fat_100g', 0) or 0
        carbs = candidate.get('carbs_100g', 0) or 0
        
        if kcal and (protein or fat or carbs):
            atwater_kcal = protein * 4 + fat * 9 + carbs * 4
            if atwater_kcal > 0:
                deviation = abs(kcal - atwater_kcal) / atwater_kcal
                if deviation <= 0.2:  # Отклонение менее 20%
                    score += 15
                elif deviation <= 0.4:  # Отклонение менее 40%
                    score += 5
                else:  # Большое отклонение
                    score -= 10
        
        # Бонус за соответствие категории
        if category and _plausible(candidate, category):
            score += 10
            
        # Штраф за неразумные значения
        if kcal and (kcal < 5 or kcal > 1000):
            score -= 20
        if protein and protein > 100:
            score -= 15
        if fat and fat > 100:
            score -= 15
        if carbs and carbs > 100:
            score -= 10
            
        return max(0, score)
        
    except Exception as e:
        logger.warning(f"Error scoring candidate: {e}")
        return 0

def _google_cse_images(q: str, num: int = 4) -> List[str]:
    """Google Custom Search для получения изображений с nutrition labels"""
    if not GOOGLE_CSE_KEY or not GOOGLE_CSE_CX:
        return []
    try:
        # Используем переданный запрос напрямую
        response = requests.get("https://www.googleapis.com/customsearch/v1",
                              params={"q": q,
                                     "key": GOOGLE_CSE_KEY,
                                     "cx": GOOGLE_CSE_CX,
                                     "searchType": "image",
                                     "num": num},
                              timeout=20)
        if response.status_code == 200:
            return [item["link"] for item in response.json().get("items", []) if "link" in item]
        else:
            logger.warning(f"Google CSE images returned status {response.status_code}")
            return []
    except Exception as e:
        logger.warning(f"Google CSE images search failed: {e}")
        return []

def _num_from_str(x: Any) -> Optional[float]:
    """Извлекает число из строки"""
    if x is None:
        return None
    m = re.search(r'(-?\d+(?:[.,]\d+)?)', str(x))
    return float(m.group(1).replace(',', '.')) if m else None

def _convert_kj_to_kcal(energy_str: str, energy_val: float) -> float:
    """Конвертирует kJ в kcal если нужно"""
    if not energy_str:
        return energy_val

    energy_str_lower = str(energy_str).lower()

    # Ищем явные указания на kJ
    if re.search(r'\bkj\b|\bkilojoul', energy_str_lower):
        return energy_val / 4.184  # kJ → kcal

    # Эвристика: если энергия > 500 и нет явного "kcal", возможно это kJ
    if energy_val > 500 and not re.search(r'\bkcal\b|\bcalorie', energy_str_lower):
        return energy_val / 4.184

    return energy_val

def _atwater_energy(protein_g: float, fat_g: float, carbs_g: float) -> float:
    """Расчёт энергии по формуле Атватера"""
    if protein_g is None:
        protein_g = 0
    if fat_g is None:
        fat_g = 0
    if carbs_g is None:
        carbs_g = 0
    return protein_g * 4 + fat_g * 9 + carbs_g * 4

def _validate_and_fix_energy(kcal: Optional[float], protein: Optional[float],
                           fat: Optional[float], carbs: Optional[float]) -> float:
    """Проверяет и исправляет энергию по формуле Атватера если нужно"""
    if kcal is None or kcal <= 0:
        if protein or fat or carbs:
            return _atwater_energy(protein or 0, fat or 0, carbs or 0)
        return 0

    # Если есть БЖУ, проверим соответствие
    if protein is not None or fat is not None or carbs is not None:
        atwater_kcal = _atwater_energy(protein or 0, fat or 0, carbs or 0)
        if atwater_kcal > 0:
            deviation = abs(kcal - atwater_kcal) / atwater_kcal
            # Если отклонение > 40%, используем расчёт Атватера
            if deviation > 0.4:
                logger.info(f"Energy mismatch: declared {kcal} vs Atwater {atwater_kcal:.1f}, using Atwater")
                return atwater_kcal

    return kcal

def _unify(n: dict, user_g: Optional[float], user_ml: Optional[float]) -> dict:
    """Унификация результатов поиска с масштабированием на 100г/100мл и пользовательскую порцию"""
    kcal_s = n.get("kcal_serv")
    p_s = n.get("protein_serv")
    f_s = n.get("fat_serv")
    c_s = n.get("carb_serv")
    sg = n.get("serving_g")
    sml = n.get("serving_ml")

    kcal_100g = p_100g = f_100g = c_100g = None
    kcal_100ml = p_100ml = f_100ml = c_100ml = None

    if sg and sg > 0:
        k = 100 / sg
        kcal_100g = kcal_s * k if kcal_s is not None else None
        p_100g = p_s * k if p_s is not None else None
        f_100g = f_s * k if f_s is not None else None
        c_100g = c_s * k if c_s is not None else None

    if sml and sml > 0:
        k = 100 / sml
        kcal_100ml = kcal_s * k if kcal_s is not None else None
        p_100ml = p_s * k if p_s is not None else None
        f_100ml = f_s * k if f_s is not None else None
        c_100ml = c_s * k if c_s is not None else None

    # на порцию пользователя
    kcal_port = p_port = f_port = c_port = None
    if user_g and kcal_100g is not None:
        k = user_g / 100
        kcal_port = kcal_100g * k
        p_port = p_100g * k if p_100g is not None else None
        f_port = f_100g * k if f_100g is not None else None
        c_port = c_100g * k if c_100g is not None else None
    elif user_ml and kcal_100ml is not None:
        k = user_ml / 100
        kcal_port = kcal_100ml * k
        p_port = p_100ml * k if p_100ml is not None else None
        f_port = f_100ml * k if f_100ml is not None else None
        c_port = c_100ml * k if c_100ml is not None else None

    return {"name": n.get("name"), "brand": n.get("brand"), "source": n.get("source"), "url": n.get("url"),
            "kcal_100g": kcal_100g, "protein_100g": p_100g, "fat_100g": f_100g, "carbs_100g": c_100g,
            "kcal_100ml": kcal_100ml, "protein_100ml": p_100ml, "fat_100ml": f_100ml, "carbs_100ml": c_100ml,
            "portion_g": user_g, "portion_ml": user_ml,
            "kcal_portion": kcal_port, "protein_portion": p_port, "fat_portion": f_port, "carbs_portion": c_port}

def _unify_and_scale(nut: Dict[str, Any], user_g: Optional[float], user_ml: Optional[float]) -> Dict[str, Any]:
    """
    Преобразует значения «на порцию» → на 100 г/100 мл и, если задана порция пользователя,
    считает КБЖУ на неё.
    """
    kcal_s = nut.get("kcal_serv")
    p_s = nut.get("protein_serv")
    f_s = nut.get("fat_serv")
    c_s = nut.get("carb_serv")
    serv_g = nut.get("serving_g")
    serv_ml = nut.get("serving_ml")
    source = nut.get("source")
    name = nut.get("name")
    brand = nut.get("brand")
    url = nut.get("url")

    kcal_100g = p_100g = f_100g = c_100g = None
    kcal_100ml = p_100ml = f_100ml = c_100ml = None

    if serv_g and serv_g > 0:
        factor = 100.0 / serv_g
        kcal_100g = kcal_s * factor if kcal_s is not None else None
        p_100g    = p_s    * factor if p_s    is not None else None
        f_100g    = f_s    * factor if f_s    is not None else None
        c_100g    = c_s    * factor if c_s    is not None else None

    if serv_ml and serv_ml > 0:
        factor = 100.0 / serv_ml
        kcal_100ml = kcal_s * factor if kcal_s is not None else None
        p_100ml    = p_s    * factor if p_s    is not None else None
        f_100ml    = f_s    * factor if f_s    is not None else None
        c_100ml    = c_s    * factor if c_s    is not None else None

    # КБЖУ на пользовательскую порцию
    kcal_portion = p_portion = f_portion = c_portion = None
    if user_g and kcal_100g is not None:
        k = user_g / 100.0
        kcal_portion = kcal_100g * k
        p_portion    = p_100g    * k if p_100g is not None else None
        f_portion    = f_100g    * k if f_100g is not None else None
        c_portion    = c_100g    * k if c_100g is not None else None
    elif user_ml and kcal_100ml is not None:
        k = user_ml / 100.0
        kcal_portion = kcal_100ml * k
        p_portion    = p_100ml    * k if p_100ml is not None else None
        f_portion    = f_100ml    * k if f_100ml is not None else None
        c_portion    = c_100ml    * k if c_100ml is not None else None

    return {
        "name": name, "brand": brand, "source": source, "url": url,
        "kcal_100g": kcal_100g, "protein_100g": p_100g, "fat_100g": f_100g, "carbs_100g": c_100g,
        "kcal_100ml": kcal_100ml, "protein_100ml": p_100ml, "fat_100ml": f_100ml, "carbs_100ml": c_100ml,
        "portion_g": user_g, "portion_ml": user_ml,
        "kcal_portion": kcal_portion, "protein_portion": p_portion,
        "fat_portion": f_portion, "carbs_portion": c_portion
    }

async def search_branded_product_via_google(
    query_text: str,
    *,
    forced_urls: Optional[list[str]] = None
) -> Optional[dict]:
    """Брендовый поиск через Google CSE с кэшированием"""
    if not GOOGLE_CSE_KEY or not GOOGLE_CSE_CX:
        logger.warning("Google CSE credentials not configured")
        return None
        
    # кэш с версионированием
    ck = f"brand:{CACHE_SCHEMA}:{query_text.lower()}"
    cached = _cache_get(ck)
    if cached:
        logger.info(f"Found cached result for: {query_text}")
        return cached

    clean, g, ml = _extract_portions(query_text)
    logger.info(f"Branded search: clean='{clean}', grams={g}, ml={ml}")
    
    # ========= 0) FATSECRET — ПРИОРИТЕТНЫЙ ШАГ =========
    if FATSECRET_KEY and FATSECRET_SECRET:
        logger.info("Trying FatSecret API...")
        try:
            # 0a) если это штрих-код — пытаемся напрямую
            barcode = _extract_barcode(query_text)
            if barcode:
                logger.info(f"Searching FatSecret by barcode: {barcode}")
                ck_fs_bar = f"fs:bar:{CACHE_SCHEMA}:{barcode}:{g}:{ml}"
                c = _cache_get(ck_fs_bar)
                if c:
                    logger.info(f"FatSecret cache hit by barcode {barcode}")
                    return c
                
                fid = await _fs_find_by_barcode(barcode)
                if fid:
                    logger.info(f"Found FatSecret food ID by barcode: {fid}")
                    food = await _fs_get_food(fid)
                    res = _fs_norm(food, g, ml) if food else None
                    if res and (res.get('kcal_100g') is not None):
                        logger.info(f"FatSecret barcode result: {res.get('name', 'Unknown')}")
                        _cache_put(ck_fs_bar, res)
                        return res
                
            # 0b) поиск по названию/бренду
            logger.info(f"Searching FatSecret by name: {clean}")
            ck_fs_q = f"fs:q:{CACHE_SCHEMA}:{clean}:{g}:{ml}"
            c = _cache_get(ck_fs_q)
            if c:
                logger.info(f"FatSecret cache hit by query {clean}")
                return c
                
            food = await _fs_search_best(clean)
            if food:
                logger.info(f"Found FatSecret food: {food.get('food_name', 'Unknown')}")
                res = _fs_norm(food, g, ml)
                if res and (res.get('kcal_100g') is not None):
                    logger.info(f"FatSecret search result: {res.get('name', 'Unknown')}")
                    _cache_put(ck_fs_q, res)
                    return res
                    
        except Exception as e:
            logger.warning(f"FatSecret search failed: {e}")
    else:
        logger.info("FatSecret credentials not configured")
    
    # Определяем категорию для фильтрации
    cat = _guess_category(query_text)
    
    candidates: list[dict] = []

    # 0) если пришли ссылки «в обход» (например, из Vision WEB_DETECTION) — используем их первыми
    urls: list[str] = []
    if forced_urls:
        urls.extend(forced_urls)

    # 1) Optimized CSE search for branded products
    if not urls:
        urls = await _google_cse_search_branded(clean, num=10)
    
    # Fallback to legacy search if optimized search fails
    if not urls:
        search_queries = [
            f"{clean} калорийность КБЖУ",
            f"{clean} состав nutrition facts",
            f"{clean} пищевая ценность"
        ]
        
        for search_query in search_queries:
            urls = _google_cse_search(search_query, num=6)
            if urls:
                break
    
    # лёгкая дедупликация и отсев мусора (вопросы/отзывы)
    deny = ("/questions", "/reviews", "otzyv", "/forum")
    seen = set()
    urls = [u for u in urls if not any(d in u for d in deny)]
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        try:
            def _fetch_html():
                return requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"}).text
            
            html = await asyncio.to_thread(_fetch_html)
            logger.info(f"Parsing HTML from: {url}")
            
        except Exception as e:
            logger.warning(f"Failed to fetch {url}: {e}")
            continue

        # 1a) JSON-LD → regex → GPT
        d = _jsonld(html)
        if not d:
            d = _regex_nutrition(html)
            logger.info(f"Regex nutrition result: {d}")
        
        if not d and OPENAI_API_KEY:   # если ничего не нашли — пробуем GPT
            logger.info("Trying GPT extraction...")
            d = await _gpt_extract_nutrition(html)
            if d:
                logger.info(f"GPT extraction successful: {d}")
            else:
                logger.info("GPT extraction failed or returned empty")
        if not d:
            # 1b) OCR по картинкам на странице (nutrition label)
            for img_url in _pick_nutrition_images(html, base_url=url)[:12]:
                txt = await _vision_ocr_text(img_url) if VISION_KEY else ""
                if not txt:
                    continue
                d = _parse_ocr(txt)
                if d:
                    d["url"] = img_url
                    break
        if not d:
            logger.info(f"No nutrition data found on: {url}")
            continue

        d["url"] = d.get("url", url)
        res = normalize_result(_unify_and_scale(d, g, ml))
        res = _fix_portion_leak(res)
        candidates.append(res)

    # CSE images → Vision OCR (с base64) — всегда пробуем, если есть ключ
    if VISION_KEY:
        logger.info(f"Healco: trying Vision OCR on image search for: {clean}")
        img_query = f"{clean} nutrition facts пищевая ценность"
        img_urls = _google_cse_images(img_query, num=12)
        
        for img in img_urls:
            txt = await _vision_ocr_text(img)
            if not txt:
                continue
                
            d = _parse_ocr(txt)
            if not d:
                continue
                
            d["url"] = img
            res = normalize_result(_unify_and_scale(d, g, ml))
            res = _fix_portion_leak(res)
            candidates.append(res)

    logger.info(f"Found {len(candidates)} candidates before filtering")
    
    # Для брендов используем мягкий фильтр
    valid_candidates = [c for c in candidates if _plausible_branded(c)]
    logger.info(f"After branded plausibility filter: {len(valid_candidates)} candidates")
    
    if not valid_candidates:
        # Если фильтр слишком строгий, пробуем без него
        logger.info("No candidates passed plausibility filter, trying without filter")
        valid_candidates = candidates

    if not valid_candidates:
        logger.info(f"No branded product found for: {query_text}")
        # Попробуем более общий поиск как fallback
        if re.search(r'\b\d{8,14}\b', query_text):
            logger.info("Trying fallback search for barcode-like query")
            fallback_result = await search_google_for_product(query_text)
            if fallback_result:
                _cache_put(ck, fallback_result)
                return fallback_result
        return None

    # Выбираем лучшего кандидата (по _cand_score)
    valid_candidates.sort(key=lambda r: _cand_score(r, cat), reverse=True)
    best = valid_candidates[0]
    _cache_put(ck, best)
    return best

async def _old_search_branded_product_via_google(
    query_text: str,
    google_cse_key: str,
    google_cse_cx: str,
    vision_key: Optional[str] = None,
    site_filter: Optional[str] = None,
    cse_results: int = 6,
    image_results: int = 4
) -> Optional[Dict[str, Any]]:
    """
    Улучшенный брендовый поиск через Google CSE + Vision OCR.
    Возвращает словарь с КБЖУ на 100 г/100 мл и на пользовательскую порцию.
    """
    # извлекаем порцию (г/мл) и чистим строку
    clean_text, grams, ml = _extract_portions(query_text)
    search_q = clean_text.strip()

    logger.info(f"Branded search: '{query_text}' → clean='{search_q}', grams={grams}, ml={ml}")

    # 1) веб-страницы через CSE
    urls = _google_cse_search(search_q, google_cse_key, google_cse_cx, num=cse_results, site_filter=site_filter)

    for url in urls:
        try:
            def _fetch_page():
                response = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
                response.raise_for_status()
                return response.text

            html = await asyncio.to_thread(_fetch_page)
        except Exception as e:
            logger.debug(f"Failed to fetch {url}: {e}")
            continue

        data = _jsonld(html) or _regex_nutrition(html)
        if not data:
            continue

        data["url"] = url
        unified = _unify(data, grams, ml)

        # успех, если есть хотя бы ккал и что-то из Б/Ж/У на 100г/100мл
        have_base = any(unified.get(k) is not None for k in ("kcal_100g", "kcal_100ml"))
        have_macros = any(unified.get(k) is not None for k in ("protein_100g", "fat_100g", "carbs_100g",
                                                               "protein_100ml", "fat_100ml", "carbs_100ml"))
        if have_base and have_macros:
            logger.info(f"Found branded product via CSE: {unified.get('name', 'Unknown')}")
            return unified

    # 2) картинки + OCR (если Vision API доступен)
    if vision_key:
        logger.info("Trying Vision OCR for images...")
        img_urls = _google_cse_images(search_q, google_cse_key, google_cse_cx, num=image_results)

        for img in img_urls:
            text = await _vision_ocr_text(img, vision_key)
            if not text:
                continue

            data = _extract_from_ocr_text(text)
            if not data:
                continue

            data["url"] = img
            unified = _unify(data, grams, ml)

            have_base = any(unified.get(k) is not None for k in ("kcal_100g", "kcal_100ml"))
            have_macros = any(unified.get(k) is not None for k in ("protein_100g", "fat_100g", "carbs_100g",
                                                                   "protein_100ml", "fat_100ml", "carbs_100ml"))
            if have_base and have_macros:
                logger.info(f"Found branded product via Vision OCR: {img}")
                return unified

    logger.info(f"No branded product found for: {query_text}")
    return None

async def search_google_for_product(query: str) -> Optional[Dict[str, Any]]:
    """Улучшенный поиск продукта через Google CSE с поддержкой брендовых продуктов и Vision OCR"""
    if not GOOGLE_CSE_KEY or not GOOGLE_CSE_CX:
        logger.warning("Google API credentials not configured")
        return None

    try:
        # Сначала пробуем улучшенный брендовый поиск
        if is_branded_product(query):
            logger.info(f"Detected branded product: {query}")
            result = search_branded_product_via_google(query)
            if result:
                logger.info(f"Found branded product: {result.get('name', 'Unknown')}")
                return result

        # Fallback к обычному поиску для натуральных продуктов
        original_grams = 100
        grams_pattern = r'(\d{1,4})\s*(?:г|гр|гр\.|g|gr|gram|grams|грамм|граммов)\b'
        grams_match = re.search(grams_pattern, query, re.IGNORECASE)
        if grams_match:
            original_grams = int(grams_match.group(1))

        clean_query = re.sub(grams_pattern, '', query, flags=re.IGNORECASE)
        clean_query = ' '.join(clean_query.split()).strip()

        logger.info(f"Google fallback search: original='{query}' | grams={original_grams} | clean='{clean_query}'")

        if len(clean_query) < 2:
            return None

        # Обычный поиск для натуральных продуктов
        search_variations = [
            f"{clean_query} калорийность КБЖУ",
            f"{clean_query} nutrition facts calories protein",
            f"{clean_query} состав пищевая ценность",
        ]

        url = "https://www.googleapis.com/customsearch/v1"

        for search_attempt, search_query in enumerate(search_variations, 1):
            logger.info(f"Search attempt {search_attempt}: {search_query}")

            params = {
                'key': GOOGLE_CSE_KEY,
                'cx': GOOGLE_CSE_CX,
                'q': search_query,
                'num': 6
            }

            def _make_request():
                response = requests.get(url, params=params, timeout=15)
                return response.json() if response.status_code == 200 else None

            data = await asyncio.to_thread(_make_request)

            if not data or not data.get('items'):
                continue

            for item in data['items']:
                title = item.get('title', '')
                snippet = item.get('snippet', '')
                text_content = f"{title} {snippet}"

                relevance_score = calculate_relevance_score(text_content, clean_query)
                if relevance_score < 0.3:
                    continue

                nutrition_data = extract_nutrition_from_text(text_content.lower(), clean_query)
                if nutrition_data and nutrition_data.get('kcal_100g', 0) > 0:
                    logger.info(f"Found fallback result: {nutrition_data['name']}")
                    return nutrition_data

        logger.info(f"No results found for: {query}")
        return None

    except Exception as e:
        logger.error(f"Google search error: {e}")
        return None

def calculate_relevance_score(text: str, query: str) -> float:
    """Вычисляет релевантность найденного текста к поисковому запросу"""
    text_lower = text.lower()
    query_words = query.lower().split()

    if not query_words:
        return 0.0

    # Подсчитываем совпадения слов
    matches = 0
    for word in query_words:
        if len(word) >= 3 and word in text_lower:
            matches += 1

    # Вычисляем базовую релевантность
    relevance = matches / len(query_words)

    # Бонусы за наличие ключевых слов питательности
    nutrition_keywords = ['калори', 'ккал', 'белк', 'жир', 'углевод', 'protein', 'fat', 'carb', 'kcal', 'nutrition']
    nutrition_bonus = sum(0.1 for keyword in nutrition_keywords if keyword in text_lower)

    return min(1.0, relevance + nutrition_bonus)

def extract_nutrition_from_ai_response(ai_text: str, product_name: str) -> Optional[Dict[str, Any]]:
    """Извлекает питательные данные из ответа ИИ"""
    try:
        # Паттерны для извлечения данных из ответа ИИ
        kcal_patterns = [
            r'(\d{1,4})\s*(?:ккал|калори|kcal|cal)',
            r'калорийность[:\s]*(\d{1,4})',
            r'энергетическая\s+ценность[:\s]*(\d{1,4})'
        ]

        protein_patterns = [
            r'белк[иао][:\s]*(\d{1,3}(?:[.,]\d{1,2})?)',
            r'протеин[:\s]*(\d{1,3}(?:[.,]\d{1,2})?)',
            r'(\d{1,3}(?:[.,]\d{1,2})?)\s*г?\s*белк'
        ]

        fat_patterns = [
            r'жир[ыао][:\s]*(\d{1,3}(?:[.,]\d{1,2})?)',
            r'(\d{1,3}(?:[.,]\d{1,2})?)\s*г?\s*жир'
        ]

        carbs_patterns = [
            r'углевод[ыао][:\s]*(\d{1,3}(?:[.,]\d{1,2})?)',
            r'(\d{1,3}(?:[.,]\d{1,2})?)\s*г?\s*углевод'
        ]

        # Извлекаем калории
        kcal = 0
        for pattern in kcal_patterns:
            match = re.search(pattern, ai_text, re.IGNORECASE)
            if match:
                try:
                    potential_kcal = int(match.group(1))
                    if 10 <= potential_kcal <= 900:
                        kcal = potential_kcal
                        break
                except ValueError:
                    continue

        # Функция для безопасного извлечения числа
        def extract_number(match_group):
            if match_group:
                try:
                    return float(str(match_group).replace(',', '.'))
                except (ValueError, AttributeError):
                    return 0
            return 0

        # Извлекаем белки
        protein = 0
        for pattern in protein_patterns:
            matches = re.findall(pattern, ai_text, re.IGNORECASE)
            for match in matches:
                if isinstance(match, tuple):
                    match = match[0] if match else ""
                potential_protein = extract_number(str(match))
                if 0 <= potential_protein <= 100:
                    protein = potential_protein
                    break
            if protein > 0:
                break

        # Извлекаем жиры
        fat = 0
        for pattern in fat_patterns:
            matches = re.findall(pattern, ai_text, re.IGNORECASE)
            for match in matches:
                if isinstance(match, tuple):
                    match = match[0] if match else ""
                potential_fat = extract_number(str(match))
                if 0 <= potential_fat <= 100:
                    fat = potential_fat
                    break
            if fat > 0:
                break

        # Извлекаем углеводы
        carbs = 0
        for pattern in carbs_patterns:
            matches = re.findall(pattern, ai_text, re.IGNORECASE)
            for match in matches:
                if isinstance(match, tuple):
                    match = match[0] if match else ""
                potential_carbs = extract_number(str(match))
                if 0 <= potential_carbs <= 100:
                    carbs = potential_carbs
                    break
            if carbs > 0:
                break

        # Если ИИ не дал конкретных чисел, пробуем найти их другим способом
        if kcal == 0 and protein == 0 and fat == 0 and carbs == 0:
            # Пытаемся найти любые числа в ответе
            numbers = re.findall(r'\b(\d{1,3}(?:[.,]\d{1,2})?)\b', ai_text)
            if len(numbers) >= 4:
                try:
                    kcal = int(float(numbers[0].replace(',', '.')))
                    protein = float(numbers[1].replace(',', '.'))
                    fat = float(numbers[2].replace(',', '.'))
                    carbs = float(numbers[3].replace(',', '.'))
                except (ValueError, IndexError):
                    pass

        # Проверяем, что получили хотя бы калории
        if kcal > 0:
            return {
                'kcal_100g': int(kcal),
                'protein_100g': round(protein, 1),
                'fat_100g': round(fat, 1),
                'carbs_100g': round(carbs, 1)
            }

        return None

    except Exception as e:
        logger.warning(f"Error extracting nutrition from AI response: {e}")
        return None

def _to_float_or_none(x):
    """Безопасное преобразование в float, возвращает None если невозможно"""
    if x is None:
        return None
    try:
        val = float(str(x).replace(',', '.'))
        return val if val > 0 else None
    except (ValueError, TypeError):
        return None

def extract_nutrition_from_text(text: str, product_name: str) -> Optional[Dict[str, Any]]:
    """Улучшенное извлечение данных о питательности из текста"""
    try:
        # Расширенные паттерны для поиска калорийности
        kcal_patterns = [
            r'(\d{1,4})\s*ккал(?:/100\s*г|на\s*100\s*г|per\s*100g)?',
            r'калорийность[:\s]*(\d{1,4})\s*ккал',
            r'энергетическая\s+ценность[:\s]*(\d{1,4})\s*ккал',
            r'(\d{1,4})\s*ккал\s*(?:на|per)\s*100',
            r'calories[:\s]*(\d{1,4})',
            r'(\d{1,4})\s*kcal',
            r'energy[:\s]*(\d{1,4})\s*kcal',
            r'пищевая\s+ценность[:\s]*(\d{1,4})\s*ккал',
            r'(\d{1,4})\s*cal(?:ories)?'
        ]

        # Расширенные паттерны для БЖУ с более гибкими форматами
        protein_patterns = [
            r'белк[иао][:\s]*(\d{1,3}(?:[.,]\d{1,2})?)\s*г',
            r'protein[:\s]*(\d{1,3}(?:[.,]\d{1,2})?)\s*g?',
            r'б[:\s]*(\d{1,3}(?:[.,]\d{1,2})?)\s*г',
            r'белок[:\s]*(\d{1,3}(?:[.,]\d{1,2})?)',
            r'протеин[:\s]*(\d{1,3}(?:[.,]\d{1,2})?)',
            r'proteins?[:\s-]*(\d{1,3}(?:[.,]\d{1,2})?)',
            r'(\d{1,3}(?:[.,]\d{1,2})?)\s*г\s*белк',
            r'(\d{1,3}(?:[.,]\d{1,2})?)\s*g?\s*protein'
        ]

        fat_patterns = [
            r'жир[ыао][:\s]*(\d{1,3}(?:[.,]\d{1,2})?)\s*г',
            r'fat[:\s]*(\d{1,3}(?:[.,]\d{1,2})?)\s*g?',
            r'ж[:\s]*(\d{1,3}(?:[.,]\d{1,2})?)\s*г',
            r'липиды[:\s]*(\d{1,3}(?:[.,]\d{1,2})?)',
            r'жиры[:\s]*(\d{1,3}(?:[.,]\d{1,2})?)',
            r'fats?[:\s-]*(\d{1,3}(?:[.,]\d{1,2})?)',
            r'(\d{1,3}(?:[.,]\d{1,2})?)\s*г\s*жир',
            r'(\d{1,3}(?:[.,]\d{1,2})?)\s*g?\s*fat'
        ]

        carbs_patterns = [
            r'углевод[ыао][:\s]*(\d{1,3}(?:[.,]\d{1,2})?)\s*г',
            r'carbohydrate[s]?[:\s]*(\d{1,3}(?:[.,]\d{1,2})?)\s*g?',
            r'carb[s]?[:\s]*(\d{1,3}(?:[.,]\d{1,2})?)\s*g?',
            r'у[:\s]*(\d{1,3}(?:[.,]\d{1,2})?)\s*г',
            r'сахара?[:\s]*(\d{1,3}(?:[.,]\d{1,2})?)',
            r'углеводы[:\s]*(\d{1,3}(?:[.,]\d{1,2})?)',
            r'carbs?[:\s-]*(\d{1,3}(?:[.,]\d{1,2})?)',
            r'(\d{1,3}(?:[.,]\d{1,2})?)\s*г\s*углевод',
            r'(\d{1,3}(?:[.,]\d{1,2})?)\s*g?\s*carb'
        ]

        # Ищем калорийность
        kcal = None
        for pattern in kcal_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                try:
                    potential_kcal = int(match.group(1))
                    if 10 <= potential_kcal <= 900:  # Разумные пределы
                        kcal = potential_kcal
                        break
                except ValueError:
                    continue

        if kcal is None:
            return None

        # Функция для извлечения числового значения из группы совпадения
        def extract_number(match_group):
            if match_group:
                try:
                    val = float(match_group.replace(',', '.'))
                    return val if 0 <= val <= 100 else None
                except (ValueError, AttributeError):
                    return None
            return None

        # Ищем белки
        protein = None
        for pattern in protein_patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            for match in matches:
                if isinstance(match, tuple):
                    match = match[0]
                potential_protein = extract_number(str(match))
                if potential_protein is not None:
                    protein = potential_protein
                    break
            if protein is not None:
                break

        # Ищем жиры
        fat = None
        for pattern in fat_patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            for match in matches:
                if isinstance(match, tuple):
                    match = match[0]
                potential_fat = extract_number(str(match))
                if potential_fat is not None:
                    fat = potential_fat
                    break
            if fat is not None:
                break

        # Ищем углеводы
        carbs = None
        for pattern in carbs_patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            for match in matches:
                if isinstance(match, tuple):
                    match = match[0]
                potential_carbs = extract_number(str(match))
                if potential_carbs is not None:
                    carbs = potential_carbs
                    break
            if carbs is not None:
                break

        # Проверка соответствия БЖУ и калорий (только если все данные есть)
        if protein is not None and fat is not None and carbs is not None:
            calculated_kcal = protein * 4 + fat * 9 + carbs * 4
            if calculated_kcal > 0:
                deviation = abs(kcal - calculated_kcal) / kcal
                if deviation > 0.4:
                    # Если отклонение большое, оставляем только калории
                    protein = fat = carbs = None

        return {
            'name': product_name.strip().title(),
            'brand': '',
            'kcal_100g': int(kcal),
            'protein_100g': protein,
            'fat_100g': fat,
            'carbs_100g': carbs,
            'url': 'smart_search'
        }

    except Exception as e:
        logger.warning(f"Error extracting nutrition from text: {e}")
        return None

def calculate_nutrition_from_internet_search(product_data: Dict[str, Any], grams: int) -> Dict[str, Any]:
    """Рассчитать питательность для указанного количества граммов из интернет-поиска"""
    factor = grams / 100.0

    # Округляем до разумной точности
    calculated_kcal = round(product_data['kcal_100g'] * factor)
    calculated_protein = round(product_data['protein_100g'] * factor, 1)
    calculated_fat = round(product_data['fat_100g'] * factor, 1)
    calculated_carbs = round(product_data['carbs_100g'] * factor, 1)

    # Формируем описание источника без упоминания "Google Search"
    source_description = f"Умный поиск: {product_data['name']}"
    if grams != 100:
        source_description += f" ({grams}г)"

    return {
        'kcal': calculated_kcal,
        'protein_g': calculated_protein,
        'fat_g': calculated_fat,
        'carbs_g': calculated_carbs,
        'notes': source_description
    }

# ========= EXTERNAL JSONL DATABASE =========
EXTERNAL_JSONL_URL = get_secret("EXTERNAL_JSONL_URL", "")

# Google Drive настройки
GDRIVE_ID = get_secret("GDRIVE_ID", "1nasoharfXMPV41QX6WWmxtiwk-L_TcCQ")  # можно и через ENV

def _file_id_from_url(url_or_id: str) -> str:
    """Извлекает file_id из Google Drive URL"""
    import re
    if re.fullmatch(r"[A-Za-z0-9_-]{25,}", url_or_id):
        return url_or_id
    m = re.search(r"/d/([A-Za-z0-9_-]{25,})/", url_or_id) or re.search(r"[?&]id=([A-Za-z0-9_-]{25,})", url_or_id)
    return m.group(1) if m else url_or_id

def _direct_url(file_id: str) -> str:
    return f"https://drive.google.com/uc?export=download&id={file_id}"

async def download_jsonl_from_gdrive(file_id: str, dest_path: str):
    """Улучшенная загрузка JSONL файла с Google Drive"""
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)

    # 1) gdown (лучше для больших файлов)
    try:
        import gdown
        gdown.download(id=file_id, output=dest_path, quiet=False)
        logger.info("Successfully downloaded with gdown")
        return
    except Exception as e:
        logger.warning(f"gdown failed: {e}, trying direct download")

    # 2) fallback: uc?export=download
    import aiohttp
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(_direct_url(file_id)) as r:
                r.raise_for_status()
                data = await r.read()

        # простая защита от HTML
        head = data[:200].lower()
        if b"<html" in head:
            raise RuntimeError("Google Drive вернул HTML (не файл). Проверьте: 'Доступ по ссылке' и попробуйте gdown.")

        with open(dest_path, "wb") as f:
            f.write(data)

        logger.info("Successfully downloaded with direct method")
    except Exception as e:
        logger.error(f"Failed to download from Google Drive: {e}")
        raise

async def load_external_jsonl_database(url_or_id: str = None) -> List[Dict[str, Any]]:
    """Загружает базу данных из внешнего JSONL файла"""
    if not url_or_id:
        url_or_id = GDRIVE_ID

    if not url_or_id:
        return []

    try:
        temp_path = "./data/products.jsonl.temp"

        if "drive.google.com" in url_or_id or len(url_or_id) < 50:  # Если это ID или URL Google Drive
            file_id = _file_id_from_url(url_or_id)
            await download_jsonl_from_gdrive(file_id, temp_path)
        else:
            # Обычная загрузка для других URL
            def _download():
                headers = {
                    'User-Agent': 'Healco-Bot/1.0 (https://replit.com)',
                    'Accept': 'application/json, text/plain'
                }
                response = requests.get(url_or_id, headers=headers, timeout=30)
                if response.status_code == 200:
                    with open(temp_path, 'w', encoding='utf-8') as f:
                        f.write(response.text)
                else:
                    logger.warning(f"Failed to download JSONL: {response.status_code}")
                    return False
                return True

            success = await asyncio.to_thread(_download)
            if not success:
                return []

        # Читаем и парсим JSONL
        products = []
        if os.path.exists(temp_path):
            with open(temp_path, 'r', encoding='utf-8') as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        if isinstance(data, dict) and data.get('name'):
                            products.append(data)
                    except json.JSONDecodeError as e:
                        logger.warning(f"Invalid JSON on line {line_num}: {e}")
                        continue

            # Удаляем временный файл
            os.unlink(temp_path)

        logger.info(f"Loaded {len(products)} products from external JSONL database")
        return products

    except Exception as e:
        logger.error(f"Error loading external JSONL database: {e}")
        return []

async def search_external_jsonl_product(query: str, products: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Ищет продукт в загруженной JSONL базе"""
    if not products:
        return None

    try:
        # Очищаем запрос
        clean_query = re.sub(r'\d+\s*(?:г|гр|гр\.|g|gr|gram|grams|грамм|граммов)', '', query, flags=re.IGNORECASE)
        # Убираем только специальные символы, но сохраняем пробелы и дефисы
        clean_query = re.sub(r'[^\w\s\-а-яё]', ' ', clean_query, flags=re.UNICODE)
        clean_query = ' '.join(clean_query.split()).lower()

        if len(clean_query) < 2:
            return None

        query_words = [word for word in clean_query.split() if len(word) >= 2]
        if not query_words:
            return None

        best_product = None
        best_score = 0

        for product in products:
            product_name = (product.get('name') or '').lower()
            brand = (product.get('brand') or '').lower()

            # Проверяем наличие питательных данных
            kcal = float(product.get('kcal_100g', 0))
            proteins = float(product.get('protein_100g', 0))
            fat = float(product.get('fat_100g', 0))
            carbs = float(product.get('carbs_100g', 0))

            if kcal <= 0 and proteins <= 0 and fat <= 0 and carbs <= 0:
                continue

            # Подсчет релевантности
            score = 0

            # Точные совпадения слов
            for word in query_words:
                if word in product_name:
                    score += 10
                    if product_name.startswith(word) or product_name.endswith(word):
                        score += 5
                if word in brand:
                    score += 6

            # Частичные совпадения для длинных слов
            for word in query_words:
                if len(word) >= 4:
                    for product_word in product_name.split():
                        if len(product_word) >= 4:
                            if word in product_word or product_word in word:
                                score += 4

            # Бонусы за качество данных
            if kcal > 0:
                score += 5
            if proteins > 0:
                score += 3
            if fat >= 0:
                score += 2
            if carbs >= 0:
                score += 2

            # Бонус за краткое название
            if len(product_name.split()) <= 5:
                score += 3

            logger.debug(f"External JSONL: {product_name[:50]}..., Score: {score}")

            if score > best_score and score >= 8:
                best_score = score
                best_product = product

        if best_product:
            logger.info(f"Found in external JSONL: {best_product['name']} (score: {best_score})")
            return {
                'name': best_product['name'],
                'brand': best_product.get('brand', ''),
                'kcal_100g': int(float(best_product.get('kcal_100g', 0))),
                'protein_100g': float(best_product.get('protein_100g', 0)),
                'fat_100g': float(best_product.get('fat_100g', 0)),
                'carbs_100g': float(best_product.get('carbs_100g', 0)),
                'url': 'external_database'
            }

        return None

    except Exception as e:
        logger.error(f"Error searching external JSONL: {e}")
        return None

def calculate_nutrition_from_external_jsonl(product_data: Dict[str, Any], grams: int) -> Dict[str, Any]:
    """Рассчитать питательность для указанного количества граммов из внешней базы"""
    factor = grams / 100.0

    return {
        'kcal': int(product_data['kcal_100g'] * factor),
        'protein_g': int(product_data['protein_100g'] * factor),
        'fat_g': int(product_data['fat_100g'] * factor),
        'carbs_g': int(product_data['carbs_100g'] * factor),
        'notes': f"Внешняя база: {product_data['name']}" + (f" ({product_data['brand']})" if product_data['brand'] else "")
    }

# ========= USDA FDC API =========
USDA_FDC_API_KEY = USDA_API_KEY

# ===================== FATSECRET CONFIG =====================
FATSECRET_KEY    = get_secret("FATSECRET_KEY", "")
FATSECRET_SECRET = get_secret("FATSECRET_SECRET", "")
FS_BASE = "https://platform.fatsecret.com/rest/server.api"

def _fatsecret_auth():
    if not FATSECRET_KEY or not FATSECRET_SECRET:
        return None
    return OAuth1(FATSECRET_KEY, FATSECRET_SECRET)

async def _fs_request(method: str, params: dict | None = None) -> dict | None:
    """Universal FatSecret REST call (OAuth1 signed)."""
    auth = _fatsecret_auth()
    if not auth:
        logger.warning("FatSecret credentials are not set (FATSECRET_KEY/SECRET empty). Skipping FS call.")
        return None
    
    # Правильные параметры для FatSecret API
    p = {"method": method, "format": "json"}
    if params:
        p.update(params)
    
    def _do():
        try:
            # FatSecret требует GET запросы с OAuth1 подписью
            response = requests.get(FS_BASE, params=p, auth=auth, timeout=25)
            return response
        except Exception as e:
            logger.error(f"FatSecret request exception: {e}")
            raise
    
    try:
        r = await asyncio.to_thread(_do)
        logger.info(f"FatSecret API call: {method}, status: {r.status_code}")
        
        if r.status_code != 200:
            body = r.text[:1000]
            if r.status_code in (401, 403):
                logger.error(
                    "FatSecret auth/permission error %s. "
                    "Check: consumer key/secret, API plan, IP whitelist. "
                    "Your IP: 34.47.244.213. Response: %s", r.status_code, body
                )
            else:
                logger.warning("FatSecret HTTP %s. Response: %s", r.status_code, body)
            return None
        
        try:
            result = r.json()
            logger.info(f"FatSecret response received: {type(result)}")
            return result
        except Exception as je:
            logger.warning("FatSecret JSON decode failed: %s... (%s)", r.text[:300], je)
            return None
    except Exception as e:
        logger.warning("FatSecret request failed: %s", e)
        return None

def _fs_to_float(x):
    try:
        return float(str(x).replace(",", ".")) if x is not None else None
    except Exception:
        return None

def _fs_pick_serving(servings) -> dict | None:
    """Pick best serving preferring metric g/ml."""
    if not servings:
        return None
    if isinstance(servings, dict):
        servings = [servings]
    def _score(s):
        unit = (s.get("metric_serving_unit") or s.get("serving_unit") or "").lower()
        score = 0
        if unit in ("g","ml"): score += 3
        if _fs_to_float(s.get("metric_serving_amount")): score += 1
        return score
    return max(servings, key=_score)

def _fs_norm(food: dict, grams: float | None, ml: float | None) -> dict | None:
    """
    Normalize FatSecret → unified dict (per 100 g/ml + per user portion).
    """
    if not food:
        return None
    name  = (food.get("food_name") or "—").strip()
    brand = (food.get("brand_name") or food.get("brand_name_ru") or "").strip() or None
    s = _fs_pick_serving((food.get("servings") or {}).get("serving"))
    if not s:
        return None
    kcal_p = _fs_to_float(s.get("calories"))
    p_p    = _fs_to_float(s.get("protein"))
    f_p    = _fs_to_float(s.get("fat"))
    c_p    = _fs_to_float(s.get("carbohydrate"))
    amount = _fs_to_float(s.get("metric_serving_amount")) or _fs_to_float(s.get("serving_amount"))
    unit   = (s.get("metric_serving_unit") or s.get("serving_unit") or "").lower()
    portion_mass = None
    if amount:
        if unit in ("g","ml"):
            portion_mass = amount
        elif unit == "oz":
            portion_mass = amount * 28.3495
        elif unit == "lb":
            portion_mass = amount * 453.592
    kcal100 = p100 = f100 = c100 = None
    if portion_mass and portion_mass > 0:
        k = 100.0 / portion_mass
        kcal100 = kcal_p * k if kcal_p is not None else None
        p100    = p_p   * k if p_p   is not None else None
        f100    = f_p   * k if f_p   is not None else None
        c100    = c_p   * k if c_p   is not None else None
    out = {
        "name": name, "brand": brand, "source": "🧩 FatSecret",
        "kcal_100g": kcal100, "protein_100g": p100, "fat_100g": f100, "carbs_100g": c100,
        "kcal_100ml": None, "protein_100ml": None, "fat_100ml": None, "carbs_100ml": None,
        "portion_g": grams, "portion_ml": ml,
        "kcal_portion": None, "protein_portion": None, "fat_portion": None, "carbs_portion": None
    }
    scale = grams if grams is not None else ml
    if scale and kcal100 is not None:
        k = float(scale)/100.0
        if kcal100 is not None: out["kcal_portion"]   = kcal100 * k
        if p100   is not None:  out["protein_portion"]= p100 * k
        if f100   is not None:  out["fat_portion"]    = f100 * k
        if c100   is not None:  out["carbs_portion"]  = c100 * k
    return out

async def _fs_get_food(food_id: str) -> dict | None:
    """Get detailed food information by ID."""
    logger.info(f"Getting FatSecret food details for ID: {food_id}")
    
    # Используем правильный метод API
    data = await _fs_request("food.get", {"food_id": food_id})
    
    if not data:
        logger.warning(f"No data returned for food_id: {food_id}")
        return None
    
    food = data.get("food")
    if food:
        logger.info(f"Successfully got FatSecret food: {food.get('food_name', 'Unknown')}")
    else:
        logger.warning(f"No food object in response for food_id: {food_id}")
    
    return food

async def _fs_search_best(query: str) -> dict | None:
    """Search by name → best food with metric serving."""
    # Используем правильный параметр для FatSecret API
    data = await _fs_request("foods.search", {"search_expression": query, "max_results": 5})
    
    if not data:
        logger.info(f"No FatSecret data returned for query: {query}")
        return None
    
    foods = data.get("foods", {})
    if isinstance(foods, dict):
        food_list = foods.get("food", [])
    else:
        food_list = foods
        
    if isinstance(food_list, dict):
        food_list = [food_list]
    
    if not food_list:
        logger.info(f"No foods found in FatSecret response for: {query}")
        return None
    
    logger.info(f"Found {len(food_list)} foods in FatSecret for: {query}")
    
    def _score(fd):
        s = ((fd.get("servings") or {}).get("serving")) or []
        if isinstance(s, dict): 
            s = [s]
        metr = 0
        for it in s:
            u = (it.get("metric_serving_unit") or it.get("serving_unit") or "").lower()
            if u in ("g","ml"): 
                metr += 1
        return (2 if fd.get("brand_name") else 0) + metr
    
    best = max(food_list, key=_score)
    food_id = best.get("food_id")
    if food_id:
        logger.info(f"Getting detailed info for FatSecret food_id: {food_id}")
        return await _fs_get_food(str(food_id))
    
    return None

async def _fs_find_by_barcode(barcode: str) -> Optional[str]:
    """Find food ID by barcode (if available in FatSecret plan)"""
    data = await _fs_request("food.find_id_for_barcode", {"barcode": barcode})
    try:
        fid = (data or {}).get("food_id")
        return str(fid) if fid else None
    except Exception:
        return None

def _extract_barcode(text: str) -> Optional[str]:
    """Extract barcode from text"""
    match = re.search(r'\b\d{8,14}\b', text)
    return match.group() if match else None

# --- RU → EN нормализация для USDA ------------------------------------------
_COOK_MAP = [
    (r"\bна\s+грил[е|я]\b|\bгрилл?\b|\bгрил[ья]\b|\bбарбекю\b", "grilled"),
    (r"\bжарен(ая|ый|ое|ые)\b|\bобжарен(а|о|ы)\b|\bна\s+сковород[е|ке]\b", "fried"),
    (r"\bварен(ая|ый|ое|ые)\b|\bотварн(ая|ый|ое|ые)\b", "boiled"),
    (r"\bзапеченн(ая|ый|ое|ые)\b|\bв\s+духовк[е|у]\b|\bзапекан[ка|ку]\b", "roasted"),
    (r"\bтушен(ая|ый|ое|ые)\b", "stewed"),
    (r"\bкопчен(ая|ый|ое|ые)\b|\bкопч[её]ност[ьи]\b", "smoked"),
]

# базовая карта по самым частым продуктам (можно расширять по мере надобности)
_BASE_MAP = [
    (r"\bкурин(ая|ый|ое)\s+грудк[а-я]*\b|\bгрудка\s+кур[ицы|иная]\b|\bфиле\s+куриц[ы|ы]\b", "chicken breast"),
    (r"\bкуриц[аы]\b|\bцыпл[её]нок\b", "chicken"),
    (r"\bиндейк[ае]\b|\bиндюшк[ае]\b", "turkey"),
    (r"\bговядин[аы]\b", "beef"),
    (r"\bсвин(ин[аы]|ина)\b", "pork"),
    (r"\bлосось\b|\bсемг[аы]\b", "salmon"),
    (r"\bтунец\b", "tuna"),
    (r"\bяйц(о|а)\b", "egg"),
    (r"\bяблок[оа]\b|\bapple\b", "apple"),
    (r"\bкартоф[её]ль\b|\bкартошка\b", "potato"),
    (r"\bрис\b", "rice"),
    (r"\bгречк[аы]\b", "buckwheat"),
]

def _ru_has_skinless_hint(s: str) -> bool | None:
    s = s.lower()
    if re.search(r"\bбез\s+кож[иы]\b|\bskinless\b|\bфиле\b", s):  # «филе» обычно без кожи/костей
        return True
    if re.search(r"\bс\s+кож[еи]\b|\bskin\b", s):
        return False
    return None

def ru_to_usda_query(ru_text: str) -> str:
    """Переводит русский запрос в английский для USDA с учетом кулинарной обработки"""
    s = ru_text.lower().strip()

    # 1) Способ приготовления
    cooking_method = ""
    for pattern, english in _COOK_MAP:
        if re.search(pattern, s):
            cooking_method = english
            s = re.sub(pattern, "", s)  # убираем из текста
            break

    # 2) Основной продукт
    base_product = ""
    for pattern, english in _BASE_MAP:
        if re.search(pattern, s):
            base_product = english
            s = re.sub(pattern, "", s)  # убираем из текста
            break

    # 3) Проверяем skinless
    skinless_hint = _ru_has_skinless_hint(ru_text)
    skinless_part = ""
    if skinless_hint is True:
        skinless_part = "without skin"
    elif skinless_hint is False:
        skinless_part = "with skin"

    # Собираем итоговый запрос
    parts = [p for p in [base_product, skinless_part, cooking_method, "cooked"] if p]
    if not parts:
        # Если ничего не нашли в картах, используем базовый перевод
        return s.strip()

    return " ".join(parts)

async def ai_translate_to_english(ru_text: str) -> str:
    """ИИ-перевод русского названия продукта на английский для USDA"""
    if not client:
        return ru_text

    try:
        prompt = f"""Переведи название продукта с русского на английский для поиска в базе USDA FDC.
Используй точные термины, принятые в американской кулинарии.

Примеры:
- "куриная грудка на гриле" → "chicken breast grilled cooked"
- "жареная картошка" → "potato fried"
- "вареная гречка" → "buckwheat cooked"
- "творог 5%" → "cottage cheese"

Переведи: "{ru_text}"

Ответ дай только переведенное название без объяснений."""

        response = await chat_llm([
            {"role": "system", "content": "Ты переводчик кулинарных терминов с русского на английский для научной базы данных USDA FDC."},
            {"role": "user", "content": prompt}
        ])

        # Очищаем ответ
        translation = response.strip().lower()
        translation = re.sub(r'[^\w\s]', ' ', translation)
        translation = ' '.join(translation.split())

        logger.info(f"AI translation: '{ru_text}' → '{translation}'")
        return translation

    except Exception as e:
        logger.warning(f"AI translation failed: {e}")
        return ru_text

_NUT_IDS = {  # FDC nutrient IDs (обновленные)
    "kcal": 1008, "protein": 1003, "fat": 1004, "carb": 1005
}

# Альтернативные IDs для разных типов данных
_ALT_NUT_IDS = {
    "kcal": [1008, 2047],  # Energy kcal, Energy kJ converted
    "protein": [1003],      # Protein
    "fat": [1004],          # Total lipid (fat)  
    "carb": [1005, 1050]    # Carbohydrate, Total carbohydrate
}

def _pick_nutr(food, nid):
    """Извлекает питательное вещество из USDA FDC food объекта"""
    # Получаем список возможных ID для поиска
    search_ids = [nid]
    for nut_type, ids in _ALT_NUT_IDS.items():
        if nid in ids:
            search_ids.extend(ids)
    
    # foods/search -> each food has foodNutrients list
    for n in food.get("foodNutrients", []):
        # Проверяем различные варианты структуры данных
        nutrient_id = n.get("nutrientId") or n.get("nutrientNumber")
        if nutrient_id in search_ids or str(nutrient_id) in [str(x) for x in search_ids]:
            value = n.get("value") or n.get("amount")
            if value is not None and value != "":
                try:
                    result = float(value)
                    # Конвертируем kJ в kcal если нужно
                    if nutrient_id == 2047 and nid == 1008:  # kJ -> kcal
                        result = result / 4.184
                    return result
                except (ValueError, TypeError):
                    continue
        
        # Проверяем вложенный объект nutrient
        nutrient = n.get("nutrient", {})
        if nutrient:
            nutrient_id = nutrient.get("id") or nutrient.get("number")
            if nutrient_id in search_ids or str(nutrient_id) in [str(x) for x in search_ids]:
                value = n.get("value") or n.get("amount")
                if value is not None and value != "":
                    try:
                        result = float(value)
                        # Конвертируем kJ в kcal если нужно
                        if nutrient_id == 2047 and nid == 1008:  # kJ -> kcal
                            result = result / 4.184
                        return result
                    except (ValueError, TypeError):
                        continue
    
    # branded sometimes in labelNutrients
    ln = food.get("labelNutrients") or {}
    m = {"kcal":"calories","protein":"protein","fat":"fat","carb":"carbohydrates"}
    for k,v in m.items():
        if nid == _NUT_IDS[k] and v in ln and "value" in ln[v]:
            try:
                return float(ln[v]["value"])
            except (ValueError, TypeError):
                continue
    
    return None

def _desc_ok_for_base(desc: str, base_en: str) -> bool:
    """Проверяет, соответствует ли описание продукта базовому продукту"""
    if not base_en or not desc:
        return True
    dl = desc.lower()
    return all(tok in dl for tok in base_en.lower().split())

async def search_usda_fdc_product(query: str, base_en: str = None) -> Optional[Dict[str, Any]]:
    """Улучшенный поиск продукта в USDA FDC API с фильтрацией по базовому продукту"""
    if not USDA_FDC_API_KEY:
        logger.warning("USDA FDC API key not configured")
        return None

    try:
        # Защита от запросов только с методом приготовления
        if query.strip().lower() in {"cooked","boiled","fried","grilled","roasted","stewed"}:
            logger.info(f"Skipping method-only query: '{query}'")
            return None

        logger.info(f"Searching USDA FDC for: '{query}'" + (f" (base: {base_en})" if base_en else ""))

        url = "https://api.nal.usda.gov/fdc/v1/foods/search"
        params = {
            'api_key': USDA_FDC_API_KEY,
            'query': query,
            'dataType': ['Foundation', 'SR Legacy', 'FNDDS'],
            'pageSize': 25
        }

        def _make_request():
            response = requests.get(url, params=params, timeout=20)
            return response.json() if response.status_code == 200 else None

        data = await asyncio.to_thread(_make_request)

        if not data or not data.get('foods'):
            return None

        foods = data['foods']

        # Фильтр по base_en (чтобы bulgur != asparagus)
        if base_en:
            filtered_foods = [f for f in foods if _desc_ok_for_base(f.get("description",""), base_en)]
            if filtered_foods:
                foods = filtered_foods

        def score_food(f):
            # Проверяем наличие основных макронутриентов
            protein_val = _pick_nutr(f, _NUT_IDS["protein"])
            fat_val = _pick_nutr(f, _NUT_IDS["fat"])
            carb_val = _pick_nutr(f, _NUT_IDS["carb"])
            kcal_val = _pick_nutr(f, _NUT_IDS["kcal"])

            # Подсчитываем количество доступных макронутриентов
            has_macros = sum(x is not None and x > 0 for x in [protein_val, fat_val, carb_val])
            has_kcal = 1 if (kcal_val is not None and kcal_val > 0) else 0

            name = (f.get("description") or "").lower()
            
            # Логируем для отладки
            logger.debug(f"Scoring food: {name[:50]}, kcal: {kcal_val}, protein: {protein_val}, fat: {fat_val}, carbs: {carb_val}")

            # Обязательно должны быть данные о питательности
            if not has_kcal or has_macros == 0:
                return 0

            # Проверяем соответствие базовому продукту
            base_match_bonus = 0
            if base_en:
                base_words = base_en.lower().split()
                for base_word in base_words:
                    if base_word in name:
                        base_match_bonus += 20

            # Бонус за совпадение методов приготовления
            cooking_bonus = 0
            query_words = query.lower().split()
            for word in query_words:
                if word in ["cooked", "grilled", "fried", "roasted", "boiled", "stewed"] and word in name:
                    cooking_bonus += 15

            # Бонус за точные совпадения слов
            word_bonus = 0
            for word in query_words:
                if len(word) >= 3 and word in name:
                    word_bonus += 3

            # Штраф за неподходящие продукты
            penalty = 0
            bad_words = ["salami", "sausage", "ham", "bacon", "jerky", "dried"]
            for bad_word in bad_words:
                if bad_word in name and base_en and bad_word not in base_en:
                    penalty += 50

            # Штраф за очень длинные названия
            length_penalty = max(0, len(name) - 100) // 20

            score = has_kcal * 100 + has_macros * 10 + base_match_bonus + cooking_bonus + word_bonus - penalty - length_penalty
            logger.debug(f"Final score for {name[:30]}: {score}")
            
            return score

        # Сортируем продукты по качеству
        foods.sort(key=score_food, reverse=True)

        if foods:
            food = foods[0]

            # Извлекаем питательные вещества
            kcal = _pick_nutr(food, _NUT_IDS["kcal"])
            protein = _pick_nutr(food, _NUT_IDS["protein"])
            fat = _pick_nutr(food, _NUT_IDS["fat"])
            carbs = _pick_nutr(food, _NUT_IDS["carb"])
            
            logger.info(f"USDA extraction results: kcal={kcal}, protein={protein}, fat={fat}, carbs={carbs}")

            # Проверяем качество результата
            if kcal and kcal > 0 and (protein or fat or carbs):
                result = {
                    'name': food.get('description', query),
                    'brand': food.get('brandOwner', ''),
                    'kcal_100g': int(kcal) if kcal else 0,
                    'protein_100g': float(protein) if protein else 0,
                    'fat_100g': float(fat) if fat else 0,
                    'carbs_100g': float(carbs) if carbs else 0,
                    'url': f"https://fdc.nal.usda.gov/fdc-app.html#/food-details/{food.get('fdcId', '')}/nutrients",
                    'source': 'usda'
                }

                logger.info(f"Found USDA result: {food.get('description', 'Unknown')} with score {score_food(food)}")
                return result
            else:
                logger.warning(f"USDA result has insufficient nutrition data: {food.get('description', 'Unknown')}")
                # Пробуем следующий результат если первый неполный
                for alt_food in foods[1:3]:  # проверяем еще 2 варианта
                    alt_kcal = _pick_nutr(alt_food, _NUT_IDS["kcal"])
                    alt_protein = _pick_nutr(alt_food, _NUT_IDS["protein"])
                    alt_fat = _pick_nutr(alt_food, _NUT_IDS["fat"])
                    alt_carbs = _pick_nutr(alt_food, _NUT_IDS["carb"])
                    
                    if alt_kcal and alt_kcal > 0 and (alt_protein or alt_fat or alt_carbs):
                        result = {
                            'name': alt_food.get('description', query),
                            'brand': alt_food.get('brandOwner', ''),
                            'kcal_100g': int(alt_kcal) if alt_kcal else 0,
                            'protein_100g': float(alt_protein) if alt_protein else 0,
                            'fat_100g': float(alt_fat) if alt_fat else 0,
                            'carbs_100g': float(alt_carbs) if alt_carbs else 0,
                            'url': f"https://fdc.nal.usda.gov/fdc-app.html#/food-details/{alt_food.get('fdcId', '')}/nutrients",
                            'source': 'usda'
                        }
                        logger.info(f"Found alternative USDA result: {alt_food.get('description', 'Unknown')}")
                        return result

    except Exception as e:
        logger.error(f"USDA FDC API error: {e}")

    return None

def calculate_nutrition_from_usda_fdc(product_data: Dict[str, Any], grams: int) -> Dict[str, Any]:
    """Рассчитать питательность для указанного количества граммов"""
    factor = grams / 100.0

    return {
        'kcal': int(product_data['kcal_100g'] * factor),
        'protein_g': int(product_data['protein_100g'] * factor),
        'fat_g': int(product_data['fat_100g'] * factor),
        'carbs_g': int(product_data['carbs_100g'] * factor),
        'notes': f"USDA FDC: {product_data['name']}" + (f" ({product_data['brand']})" if product_data['brand'] else "") + f" | Ссылка: {product_data['url']}"
    }

# ========= OPEN FOOD FACTS API =========
async def search_openfoodfacts_product(query: str) -> Optional[Dict[str, Any]]:
    """Поиск продукта в Open Food Facts API"""
    try:
        # Очищаем запрос от лишних символов
        original_query = query.strip()

        # Более гибкая очистка - сохраняем структуру запроса
        clean_query = re.sub(r'\d+\s*(?:г|гр|гр\.|g|gr|gram|grams|грамм|граммов)', '', original_query, flags=re.IGNORECASE)
        # Убираем только специальные символы, но сохраняем пробелы и дефисы
        clean_query = re.sub(r'[^\w\s\-а-яё]', ' ', clean_query, flags=re.UNICODE)
        clean_query = ' '.join(clean_query.split())

        # Пробуем несколько вариантов поиска
        search_queries = []
        if len(clean_query.strip()) >= 3:
            search_queries.append(clean_query.strip())
        if original_query != clean_query and len(original_query.strip()) >= 3:
            search_queries.append(original_query.strip())

        # Добавляем варианты на английском если запрос на русском
        if any(ord(c) >= 1040 for c in original_query):  # Проверяем наличие кириллицы
            # Простой словарь для перевода часто встречающихся продуктов
            translations = {
                'молоко': 'milk', 'хлеб': 'bread', 'мясо': 'meat', 'курица': 'chicken',
                'рыба': 'fish', 'яблоко': 'apple', 'банан': 'banana', 'рис': 'rice',
                'гречка': 'buckwheat', 'овсянка': 'oats', 'творог': 'cottage cheese',
                'сыр': 'cheese', 'яйцо': 'egg', 'картофель': 'potato', 'морковь': 'carrot',
                'капуста': 'cabbage', 'лук': 'onion', 'помидор': 'tomato', 'огурец': 'cucumber'
            }
            for rus_word, eng_word in translations.items():
                if rus_word in clean_query.lower():
                    eng_query = clean_query.lower().replace(rus_word, eng_word)
                    search_queries.append(eng_query)

        if not search_queries:
            return None

        headers = {
            'User-Agent': 'Healco-Bot/1.0 (https://replit.com)',
            'Accept': 'application/json'
        }

        best_result = None
        best_overall_score = 0

        # Пробуем каждый вариант запроса
        for search_query in search_queries[:3]:  # Ограничиваем количество попыток
            url = "https://world.openfoodfacts.org/api/v2/search"
            params = {
                'q': search_query,
                'page_size': 20,
                'fields': 'product_name,brands,code,nutriments,categories',
                'sort_by': 'unique_scans_n'
            }

            def _make_request():
                import time
                time.sleep(0.3)
                response = requests.get(url, params=params, headers=headers, timeout=20)
                if response.status_code == 200:
                    return response.json()
                else:
                    logger.warning(f"Open Food Facts API returned status {response.status_code}")
                    return None

            data = await asyncio.to_thread(_make_request)

            if not data or not data.get('products'):
                continue

            # Анализируем продукты с более строгой фильтрацией
            query_words = [word.lower() for word in search_query.split() if len(word) >= 2]

            for product in data['products'][:12]:
                nutriments = product.get('nutriments', {})
                product_name = (product.get('product_name') or '').lower()
                brand = (product.get('brands') or '').lower()
                categories = (product.get('categories') or '').lower()

                # Пропускаем продукты без названия или со слишком коротким названием
                if not product_name or len(product_name) < 3:
                    continue

                # Функция для безопасного получения числового значения
                def safe_float(value, default=0):
                    if value is None:
                        return default
                    try:
                        return float(value)
                    except (ValueError, TypeError):
                        return default

                # Получаем питательные значения
                energy = safe_float(nutriments.get('energy-kcal_100g'))
                proteins = safe_float(nutriments.get('proteins_100g'))
                fat = safe_float(nutriments.get('fat_100g'))
                carbs = safe_float(nutriments.get('carbohydrates_100g'))

                # Более строгие требования к данным - должна быть хоть какая-то информация
                if energy <= 0 and proteins <= 0 and fat <= 0 and carbs <= 0:
                    continue

                # Строгая система подсчета релевантности
                score = 0

                # Обязательно должно быть хотя бы одно прямое совпадение слова
                has_direct_match = False
                exact_matches = 0

                for word in query_words:
                    # Точные совпадения слов (более строгие)
                    product_words = product_name.split()
                    for product_word in product_words:
                        if word == product_word:  # Полное совпадение слова
                            exact_matches += 1
                            has_direct_match = True
                            score += 20
                            break
                        elif len(word) >= 4 and len(product_word) >= 4:
                            # Совпадение начала для длинных слов
                            if word.startswith(product_word[:4]) or product_word.startswith(word[:4]):
                                score += 8
                                has_direct_match = True

                    # Совпадения в бренде (только точные)
                    if brand and word in brand.split():
                        score += 12
                        has_direct_match = True

                # Если нет прямых совпадений, пропускаем продукт
                if not has_direct_match:
                    continue

                # Бонус за качество данных
                if energy > 0:
                    score += 8
                if proteins > 0:
                    score += 4
                if fat >= 0:
                    score += 2
                if carbs >= 0:
                    score += 2

                # Бонус за полноту совпадений (должно быть минимум 50% слов)
                if exact_matches >= max(1, len(query_words) // 2):
                    score += 20

                # Минимальный порог повышен для более точных результатов
                min_threshold = 15

                logger.debug(f"Query '{search_query}': {product_name[:50]}..., Score: {score}, Energy: {energy}, Direct match: {has_direct_match}")

                if score > best_overall_score and score >= min_threshold:
                    best_overall_score = score
                    best_result = {
                        'name': product.get('product_name', original_query) or original_query,
                        'brand': product.get('brands', '') or '',
                        'kcal_100g': int(energy) if energy > 0 else 0,
                        'protein_100g': proteins,
                        'fat_100g': fat,
                        'carbs_100g': carbs,
                        'url': f"https://world.openfoodfacts.org/product/{product.get('code', '')}"
                    }

        if best_result:
            logger.info(f"Found product: {best_result['name']} with {best_result['kcal_100g']} kcal (score: {best_overall_score})")
            return best_result

        logger.info(f"No suitable product found for any query variant")

    except Exception as e:
        logger.error(f"Open Food Facts API error: {e}")

    return None

def calculate_nutrition_from_openfoodfacts(product_data: Dict[str, Any], grams: int) -> Dict[str, Any]:
    """Рассчитать питательность для указанного количества граммов"""
    factor = grams / 100.0

    return {
        'kcal': int(product_data['kcal_100g'] * factor),
        'protein_g': int(product_data['protein_100g'] * factor),
        'fat_g': int(product_data['fat_100g'] * factor),
        'carbs_g': int(product_data['carbs_100g'] * factor),
        'notes': f"Open Food Facts: {product_data['name']}" + (f" ({product_data['brand']})" if product_data['brand'] else "") + f" | Ссылка: {product_data['url']}"
    }

# ========= ПП‑МЕНЮ 60 ДНЕЙ =========
def load_pp_menu_60() -> List[Dict[str, Any]]:
    """Загружает меню на 60 дней из pp_menu_60.json"""
    try:
        with open(MENU_60_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Could not load pp_menu_60.json: {e}")
        return []

def get_menu_for_day(day: int, variant: str = "a") -> Optional[Dict[str, str]]:
    """Получить меню для конкретного дня (1-60) и варианта (a/b)"""
    menu_data = load_pp_menu_60()
    if not menu_data:
        return None

    # Найти день в данных
    day_data = next((d for d in menu_data if d.get("day") == day), None)
    if not day_data:
        return None

    variant_key = f"variant_{variant}"
    return {
        "breakfast": day_data.get("breakfast", {}).get(variant_key, ""),
        "snack_1": day_data.get("snack_1", {}).get(variant_key, ""),
        "lunch": day_data.get("lunch", {}).get(variant_key, ""),
        "snack_2": day_data.get("snack_2", {}).get(variant_key, ""),
        "dinner": day_data.get("dinner", {}).get(variant_key, "")
    }

def get_current_menu_day(st: Dict[str, Any]) -> int:
    """Получить текущий день меню для пользователя (циклично 1-60)"""
    # Используем количество сгенерированных меню как основу для дня
    menu_count = st["tmp"].get("menu_day_counter", 0)
    return (menu_count % 60) + 1

def increment_menu_day(st: Dict[str, Any]):
    """Увеличить счетчик дня меню"""
    st["tmp"]["menu_day_counter"] = st["tmp"].get("menu_day_counter", 0) + 1

async def generate_menu_with_nutrition(profile: Dict[str, Any], menu_items: Dict[str, str], target_kcal: int, changes: str = "") -> str:
    """Генерирует меню с рассчитанной нутрициологом граммовкой и КБЖУ"""
    allergies = profile.get("allergies", "нет")
    conditions = profile.get("conditions", "нет")
    goal = profile.get("goal", "Поддерживать вес")

    sys = (
        "Вы профессиональный нутрициолог. Рассчитайте граммовку, калорийность и КБЖУ для готового меню. "
        "Используйте официальный деловой стиль. НЕ используйте смайлики в тексте меню. "
        "НЕ используйте символы # (решетки) для заголовков - используйте простой текст с заглавными буквами. "
        "При возможности используйте доступные рецепты из базы или создавайте аналогичные блюда. "
        "Для каждого приема пищи укажите граммовку ингредиентов, калорийность и КБЖУ. "
        "Фрукты и орехи указывайте в штуках (например: яблоко 150г (1 шт), грецкие орехи 30г (6 шт)). "
        "В конце каждого приема пищи указывайте общий итог: 'Итого: ~X ккал, Б: Y г, Ж: Z г, У: W г'"
        f"\nПрофиль: пол={profile.get('gender')}, возраст={profile.get('age')}, "
        f"рост={profile.get('height_cm')} см, вес={profile.get('weight_kg')} кг, "
        f"цель={goal}, аллергии={allergies}, заболевания={conditions}."
    )

    user_prompt = (
        f"Рассчитайте граммовки и КБЖУ для меню на {target_kcal} ккал:\n\n"
        f"ЗАВТРАК: {menu_items['breakfast']}\n"
        f"ПЕРЕКУС 1: {menu_items['snack_1']}\n"
        f"ОБЕД: {menu_items['lunch']}\n"
        f"ПЕРЕКУС 2: {menu_items['snack_2']}\n"
        f"УЖИН: {menu_items['dinner']}\n\n"
        f"Учесть пожелания: {changes or 'стандартные порции'}\n\n"
        f"ВАЖНО: НЕ используйте символы # (решетки). Используйте обычный текст.\n"
        f"ОБЯЗАТЕЛЬНО завершите ответ полным подсчетом: 'Итого за день: ~X ккал, Б: Y г, Ж: Z г, У: W г'"
    )

    result = await chat_llm([{"role": "system", "content": sys}, {"role": "user", "content": user_prompt}])

    # Убираем решетки из результата если они есть
    result = result.replace("###", "").replace("##", "").replace("#", "")

    # Проверяем есть ли полный итог с БЖУ, если нет - добавляем
    if not re.search(r"Итого за день:.*?Б:.*?Ж:.*?У:", result):
        # Рассчитываем примерные БЖУ
        protein_g = int(target_kcal * 0.25 / 4)  # 25% от калорий на белки
        fat_g = int(target_kcal * 0.25 / 9)      # 25% от калорий на жиры
        carbs_g = int(target_kcal * 0.50 / 4)    # 50% от калорий на углеводы

        # Добавляем корректный итог
        if "Итого за день:" in result:
            # Заменяем неполный итог на полный
            result = re.sub(r"Итого за день:.*?ккал.*?(?=\n|$)",
                          f"Итого за день: ~{target_kcal} ккал, Б: {protein_g} г, Ж: {fat_g} г, У: {carbs_g} г",
                          result)
        else:
            # Добавляем полный итог
            result += f"\n\nИтого за день: ~{target_kcal} ккал, Б: {protein_g} г, Ж: {fat_g} г, У: {carbs_g} г"

    # Добавляем смайлики только в самом конце
    if not result.endswith((" 🍽️🥗🍎", " 🍽️", " 🥗", " 🍎")):
        result += " 🍽️🥗🍎"

    return sanitize_ai(result)

# ========= ПП‑РЕЦЕПТЫ =========
RECIPES_PATH = os.getenv("RECIPES_PATH", str(Path(__file__).with_name("recipes.json")))
MENU_60_PATH = os.getenv("MENU_60_PATH", str(Path(__file__).with_name("pp_menu_60.json")))

@dataclass
class Recipe:
    id: str
    title: str
    category: str
    kcal: int
    protein_g: int
    fat_g: int
    carbs_g: int
    steps: List[str]
    photo: Optional[str] = None
    brand: Optional[Dict[str, str]] = None
    ingredients: Optional[List[str]] = None

def _default_recipes() -> List[Dict[str, Any]]:
    try:
        with open(RECIPES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.error(f"Could not load recipes.json: {e}")
        data = {}
    out: List[Dict[str, Any]] = []

    def _norm_cat(s: str) -> str:
        s = (s or "").strip().lower()
        mapping = {"завтраки": "завтрак", "обеды": "обед", "ужины": "ужин", "перекусы": "перекус", "десерты": "десерт"}
        return mapping.get(s, s or "разное")

    if isinstance(data, dict) and "sections" in data:
        for sec in (data.get("sections") or []):
            cat = _norm_cat(sec.get("section") or "разное")
            for r in (sec.get("recipes") or []):
                nutr = r.get("nutrition") or {}
                ingr_list = []
                for it in (r.get("ingredients") or []):
                    if isinstance(it, dict):
                        prod, amt = str(it.get("product", "")).strip(), str(it.get("amount", "")).strip()
                        ingr_list.append(f"{prod} — {amt}" if prod and amt else prod)
                    elif isinstance(it, str):
                        ingr_list.append(it.strip())
                out.append(
                    {
                        "id": r.get("id") or f"rec_{len(out)+1}",
                        "title": r.get("name") or "Рецепт",
                        "category": cat,
                        "kcal": int(nutr.get("calories", 0)),
                        "protein_g": int(float(nutr.get("protein", 0))),
                        "fat_g": int(float(nutr.get("fat", 0))),
                        "carbs_g": int(float(nutr.get("carbs", 0))),
                        "steps": list(r.get("steps", [])),
                        "photo": r.get("photo"),
                        "brand": r.get("brand"),
                        "ingredients": ingr_list or None,
                    }
                )
    elif isinstance(data, list):
        for r in data:
            out.append(
                {
                    "id": r.get("id") or f"rec_{len(out)+1}",
                    "title": r.get("title", "Рецепт"),
                    "category": _norm_cat(r.get("category", "разное")),
                    "kcal": int(r.get("kcal", 0)),
                    "protein_g": int(r.get("protein_g", 0)),
                    "fat_g": int(r.get("fat_g", 0)),
                    "carbs_g": int(r.get("carbs_g", 0)),
                    "steps": list(r.get("steps", [])),
                    "photo": r.get("photo"),
                    "brand": r.get("brand"),
                    "ingredients": r.get("ingredients"),
                }
            )
    if not out:
        out = [
            {
                "id": "demo_oat",
                "title": "Овсянка с ягодами",
                "category": "завтрак",
                "kcal": 350,
                "protein_g": 20,
                "fat_g": 9,
                "carbs_g": 50,
                "steps": ["Смешай хлопья с молоком", "Вари 5–7 мин", "Добавь ягоды и мёд"],
                "ingredients": ["Овсянка — 50 г", "Молоко — 200 мл", "Ягоды — 50 г"],
            }
        ]
    return out

def load_recipes() -> List[Recipe]:
    return [
        Recipe(
            id=r["id"],
            title=r["title"],
            category=r["category"],
            kcal=int(r["kcal"]),
            protein_g=int(r["protein_g"]),
            fat_g=int(r["fat_g"]),
            carbs_g=int(r["carbs_g"]),
            steps=list(r.get("steps", [])),
            photo=r.get("photo"),
            brand=r.get("brand"),
            ingredients=r.get("ingredients"),
        )
        for r in _default_recipes()
    ]

def recipe_categories(recs: List[Recipe]) -> List[str]:
    cats = sorted({r.category for r in recs})
    order = ["завтрак", "перекус", "обед", "ужин", "десерт"]
    return sorted(cats, key=lambda x: (order.index(x) if x in order else 999, x))

def kb_recipe_cats(cats: List[str]):
    rows, row = [], []
    for i, c in enumerate(cats, 1):
        row.append(InlineKeyboardButton(c.capitalize(), callback_data=f"rcat:{c}"))
        if i % 3 == 0:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("✨ 1 случайный рецепт", callback_data="rshow:random")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="rcat:back")])
    return InlineKeyboardMarkup(rows)

def kb_recipe_root(cats: List[str], has_access: bool = True):
    if has_access:
        return kb_recipe_cats(cats)
    else:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✨ 1 случайный рецепт", callback_data="rshow:random")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="rroot")]
        ])

def meal_from_hour(hour: int) -> str:
    if 6 <= hour < 11:
        return "завтрак"
    elif 11 <= hour < 16:
        return "обед"
    elif 16 <= hour < 20:
        return "ужин"
    else:
        return "перекус"

def filter_recipes_by_meal(meal: str, recipes: List[Recipe]) -> List[Recipe]:
    return [r for r in recipes if r.category == meal]

def kb_recipe_list_meal(meal: str, recs: List[Recipe], page: int = 0, per: int = 6) -> InlineKeyboardMarkup:
    items = filter_recipes_by_meal(meal, recs)
    start = page * per
    chunk = items[start: start + per]
    rows = [[InlineKeyboardButton(r.title, callback_data=f"rshow:{r.id}")] for r in chunk]
    nav = []
    if start > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"rpage_meal:{meal}:{page-1}"))
    if start + per < len(items):
        nav.append(InlineKeyboardButton("▶️", callback_data=f"rpage_meal:{meal}:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="rroot")])
    return InlineKeyboardMarkup(rows)

def kb_recipe_list(cat: str, recs: List[Recipe], page: int = 0, per: int = 6):
    items = [r for r in recs if r.category == cat]
    start = page * per
    chunk = items[start : start + per]
    rows = [[InlineKeyboardButton(r.title, callback_data=f"rshow:{r.id}")] for r in chunk]
    nav = []
    if start > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"rpage:{cat}:{page-1}"))
    if start + per < len(items):
        nav.append(InlineKeyboardButton("▶️", callback_data=f"rpage:{cat}:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("⬅️ Категории", callback_data="rroot")])
    return InlineKeyboardMarkup(rows)

def kb_recipe_actions(rid: str):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("➕ Добавить в дневник", callback_data=f"radd:{rid}")],
         [InlineKeyboardButton("⬅️ Список", callback_data="rback")]]
    )

def format_recipe_card(r: Recipe) -> str:
    lines = [
        f"{r.title} 🍽️",
        f"Категория: {r.category.capitalize()}",
        f"Калорийность: ~{r.kcal} ккал; Б{r.protein_g}/Ж{r.fat_g}/У{r.carbs_g}",
    ]
    if r.steps:
        lines.append("Как приготовить:")
        lines.extend(f"{i}) {step}" for i, step in enumerate(r.steps, 1))
    elif r.ingredients:
        lines.append("Ингредиенты:")
        lines.extend(f"— {ing}" for ing in r.ingredients)
    if r.brand:
        lines.append(f"\nСовет: можно использовать продукт партнёра {r.brand.get('name')} — {r.brand.get('note') or ''}")
    return "\n".join(lines)

async def recipes_root(update: Update, context: ContextTypes.DEFAULT_TYPE, st: Dict[str, Any]):
    recs = load_recipes()
    cats = recipe_categories(recs)
    txt = "Выбери категорию ПП‑рецептов или получи случайный. Я укажу КБЖУ и смогу сразу добавить блюдо в дневник."
    has_access = check_feature_access(st, u.id, "recipes")
    markup = kb_recipe_root(cats, has_access)
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(txt, reply_markup=markup)
    else:
        await update.message.reply_text(txt, reply_markup=markup)

async def show_recipe_category(update: Update, context: ContextTypes.DEFAULT_TYPE, category: str, st: Dict[str, Any]):
    """Показать рецепты определенной категории"""
    recs = load_recipes()
    category_recipes = [r for r in recs if r.category == category]

    if not category_recipes:
        await update.message.reply_text(
            f"В категории '{category.capitalize()}' пока нет рецептов.",
            reply_markup=ReplyKeyboardMarkup([
                [KeyboardButton("🌅 Завтраки"), KeyboardButton("🍽️ Обеды")],
                [KeyboardButton("🌙 Ужины"), KeyboardButton("✨ Случайный рецепт")],
                [KeyboardButton("⬅️ Назад")]
            ], resize_keyboard=True)
        )
        return

    # Показать первые 5 рецептов
    lines = [f"📋 {category.capitalize()}:"]
    for i, recipe in enumerate(category_recipes[:5], 1):
        lines.append(f"{i}. {recipe.title} (~{recipe.kcal} ккал)")

    if len(category_recipes) > 5:
        lines.append(f"...и ещё {len(category_recipes) - 5} рецептов")

    lines.append("\nВведите номер рецепта (1-5) или выберите другую категорию:")

    st["tmp"]["current_category"] = category
    # Конвертируем Recipe объекты в словари для JSON сериализации
    st["tmp"]["category_recipes"] = [
        {
            "id": r.id,
            "title": r.title,
            "category": r.category,
            "kcal": r.kcal,
            "protein_g": r.protein_g,
            "fat_g": r.fat_g,
            "carbs_g": r.carbs_g,
            "steps": r.steps,
            "ingredients": r.ingredients
        } for r in category_recipes
    ]
    st["awaiting"] = "recipe_number"

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=ReplyKeyboardMarkup([
            [KeyboardButton("🌅 Завтраки"), KeyboardButton("🍽️ Обеды")],
            [KeyboardButton("🌙 Ужины"), KeyboardButton("✨ Случайный рецепт")],
            [KeyboardButton("⬅️ Назад")]
        ], resize_keyboard=True)
    )

async def show_random_recipe(update: Update, context: ContextTypes.DEFAULT_TYPE, st: Dict[str, Any]):
    """Показать случайный рецепт"""
    recs = load_recipes()
    if not recs:
        await update.message.reply_text("Рецепты не найдены.")
        return

    import random as _r
    recipe = _r.choice(recs)
    await show_recipe_detail(update, context, recipe, st)

async def show_recipe_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, recipe: Recipe, st: Dict[str, Any]):
    """Показать подробную информацию о рецепте"""
    lines = [
        f"🍽️ {recipe.title}",
        f"Категория: {recipe.category.capitalize()}",
        f"Калорийность: ~{recipe.kcal} ккал; Б{recipe.protein_g}/Ж{recipe.fat_g}/У{recipe.carbs_g}",
    ]

    if recipe.ingredients:
        lines.append("\n📝 Ингредиенты:")
        for ingredient in recipe.ingredients:
            lines.append(f"• {ingredient}")

    if recipe.steps:
        lines.append("\n👨‍🍳 Приготовление:")
        for i, step in enumerate(recipe.steps, 1):
            lines.append(f"{i}. {step}")

    lines.append("\nДобавить в дневник? (да/нет)")

    st["tmp"]["current_recipe"] = {
        "title": recipe.title,
        "kcal": recipe.kcal,
        "protein_g": recipe.protein_g,
        "fat_g": recipe.fat_g,
        "carbs_g": recipe.carbs_g
    }
    st["awaiting"] = "add_recipe_to_diary"

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=ReplyKeyboardMarkup([
            [KeyboardButton("Да"), KeyboardButton("Нет")],
            [KeyboardButton("⬅️ Назад")]
        ], resize_keyboard=True)
    )

async def recipes_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query, u = update.callback_query, update.effective_user
    data = query.data or ""
    st = load_state(u.id)
    try:
        await query.answer()
        if data in ("rroot", "rback", "rcat:back"):
            await recipes_root(update, context, st)
            return

        if data.startswith("rcat:"):
            _, cat = data.split(":", 1)
            if not check_feature_access(st, u.id, "recipes"):
                await query.edit_message_text(
                    "Полный доступ к рецептам доступен на тарифе Премиум и выше. ⭐",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [InlineKeyboardButton("В магазин", callback_data="shop_open")],
                            [InlineKeyboardButton("⬅️ Назад", callback_data="rroot")],
                        ]
                    ),
                )
                return
            await query.edit_message_text(f"Категория: {cat.capitalize()}", reply_markup=kb_recipe_list(cat, recs, page=0))
            return

        if data.startswith("rshow:"):
            _, rid = data.split(":", 1)
            import datetime
            rlist = recs
            if rid == "random":
                if get_user_access(st, u.id) == "free":
                    meal = meal_from_hour(datetime.datetime.now().hour)
                    rlist = filter_recipes_by_meal(meal, recs)
                import random as _r
                r = _r.choice(rlist) if rlist else None
            else:
                r = next((x for x in recs if x.id == rid), None)
            if not r:
                await query.edit_message_text("Рецепт не найден")
                return

            if rid != "random" and not check_feature_access(st, u.id, "recipes"):
                await query.edit_message_text(
                    "Этот рецепт доступен в тарифах Премиум и выше. ⭐",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [InlineKeyboardButton("В магазин", callback_data="shop_open")],
                            [InlineKeyboardButton("⬅️ Назад", callback_data="rback")],
                        ]
                    ),
                )
                return

            if rid == "random" and get_user_access(st, u.id) == "free":
                st["tmp"]["used_random_recipe"] = True
                save_state(u.id, st)

            await query.edit_message_text(format_recipe_card(r), reply_markup=kb_recipe_actions(r.id))
            return

        if data.startswith("radd:"):
            _, rid = data.split(":", 1)
            r = next((x for x in recs if x.id == rid), None)
            if not r:
                await query.answer("Рецепт не найден")
                return
            st["diaries"]["food"].append(
                {"ts": now_ts(), "text": f"Рецепт: {r.title}", "kcal": r.kcal, "p": r.protein_g, "f": r.fat_g, "c": r.carbs_g}
            )
            add_kcal_in(st, r.kcal)
            add_points(st, 2)
            save_state(u.id, st)
            await query.answer("Добавлено в дневник ✅")
            await query.edit_message_reply_markup(kb_recipe_actions(r.id))
            return

        if data == "shop_open":
            await query.edit_message_reply_markup(None)
            await shop_command(update, context)

    except Exception as e:
        logger.error(f"recipes_callbacks error: {e}")
        await query.answer("Ошибка")

# ========= ЛИДЕРБОРД =========
def leaderboard_all() -> List[Dict[str, Any]]:
    arr = []
    for k in db_keys_prefix("user:"):
        st = db_get(k, {})
        if isinstance(st, dict):
            pts = int(st.get("points", 0))
            arr.append({"user_id": k.split(":", 1)[-1], "points": pts})
    arr.sort(key=lambda x: x["points"], reverse=True)
    return arr

# ========= КОМАНДЫ =========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    st = load_state(u.id)
    if not profile_complete(st["profile"]):
        st["awaiting"] = "onb_gender"
        save_state(u.id, st)
        await update.message.reply_text(
            f"Привет, {u.first_name or 'друг'}! Я {PROJECT_NAME} — про питание и тренировки. 🚀\n\n"
            "Сначала заполним короткую анкету — и я настрою меню и планы под твою цель. ",
            reply_markup=ReplyKeyboardMarkup(GENDER_KB, resize_keyboard=True),
        )
        await update.message.reply_text("Пол: выбери «Женский» или «Мужской». 🙂")
        return
    st["awaiting"] = None
    st["current_role"] = None
    save_state(u.id, st)
    await update.message.reply_text(
        "Готово! Что умею:\n"
        "• Нутрициолог — меню (базовое) под цель, КБЖУ, дневник питания\n"
        "• Тренер — планы с ЧСС, дневник и подсчёт ккал, восстановление/профилактика травм\n"
        "• Сводка за день: съедено/сожжено/рекомендация\n\nВыбирай раздел 👇",
        reply_markup=role_keyboard(None),
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    st = load_state(u.id)
    help_text = (
        "/start — меню\n/help — помощь\n/whoami — ваш ID\n/health — 200 OK\n/version — текущая версия\n/shop — магазин\n\n"
        "Любой текст в выбранной роли — вопрос соответствующему специалисту. 💬"
    )

    if is_developer(u.id):
        help_text += (
            "\n\n👑 Команды разработчика:\n"
            "/add_admin <user_id> — добавить администратора\n"
            "/remove_admin <user_id> — удалить администратора\n"
            "/list_admins — список администраторов"
        )

    await update.message.reply_text(help_text, reply_markup=role_keyboard(st.get("current_role")))

async def version_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(VERSION)

async def health_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("200 OK")

async def whoami_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_dev = "✅ (полный доступ)" if is_developer(user.id) else "❌"
    is_admin = "✅ (полный доступ)" if is_admin_user(user.id) else "❌"
    lines = [f"👤 Твой ID: {user.id}", f"Статус разработчика: {is_dev}", f"Статус администратора: {is_admin}"]
    if user.first_name:
        lines.append(f"Имя: {user.first_name}")
    if user.username:
        lines.append(f"Ник: @{user.username}")
    await update.message.reply_text("\n".join(lines))

async def add_admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_developer(user.id):
        await update.message.reply_text("❌ Только разработчик может добавлять администраторов.")
        return

    try:
        if not context.args:
            await update.message.reply_text("Использование: /add_admin <user_id>\nПример: /add_admin 123456789")
            return

        target_user_id = int(context.args[0])
        if add_admin_user(target_user_id):
            await update.message.reply_text(f"✅ Пользователь {target_user_id} добавлен в администраторы.")
        else:
            await update.message.reply_text(f"⚠️ Пользователь {target_user_id} уже является администратором.")
    except ValueError:
        await update.message.reply_text("❌ Неверный ID пользователя. Используйте числовой ID.")

async def remove_admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_developer(user.id):
        await update.message.reply_text("❌ Только разработчик может удалять администраторов.")
        return

    try:
        if not context.args:
            await update.message.reply_text("Использование: /remove_admin <user_id>\nПример: /remove_admin 123456789")
            return

        target_user_id = int(context.args[0])
        if remove_admin_user(target_user_id):
            await update.message.reply_text(f"✅ Пользователь {target_user_id} удалён из администраторов.")
        else:
            await update.message.reply_text(f"⚠️ Пользователь {target_user_id} не является администратором.")
    except ValueError:
        await update.message.reply_text("❌ Неверный ID пользователя. Используйте числовой ID.")

async def list_admins_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_developer(user.id):
        await update.message.reply_text("❌ Только разработчик может просматривать список администраторов.")
        return

    admins = get_admin_users()
    if not admins:
        await update.message.reply_text("📝 Список администраторов пуст.")
        return

    lines = ["👥 Администраторы с полным доступом:"]
    for admin_id in admins:
        lines.append(f"• {admin_id}")

    await update.message.reply_text("\n".join(lines))

async def refresh_database_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для обновления базы данных продуктов"""
    user = update.effective_user
    if not (is_developer(user.id) or is_admin_user(user.id)):
        await update.message.reply_text("❌ Только администраторы могут обновлять базу данных.")
        return

    await update.message.reply_text("🔄 Скачиваю JSONL с Google Drive…")

    try:
        temp_path = "./data/products.jsonl.part"
        final_path = "./data/products.jsonl"

        # Создаем директорию если не существует
        os.makedirs("./data", exist_ok=True)

        # Скачиваем файл
        await download_jsonl_from_gdrive(GDRIVE_ID, temp_path)

        # Проверяем что файл скачался корректно
        if os.path.exists(temp_path) and os.path.getsize(temp_path) > 0:
            # Заменяем основной файл
            if os.path.exists(final_path):
                os.replace(temp_path, final_path)
            else:
                os.rename(temp_path, final_path)

            # Подсчитываем количество записей
            products = await load_external_jsonl_database()
            product_count = len(products)

            await update.message.reply_text(
                f"✅ База данных обновлена успешно!\n"
                f"📊 Загружено продуктов: {product_count}"
            )
        else:
            await update.message.reply_text("❌ Ошибка: файл не скачался или пустой")

    except Exception as e:
        logger.error(f"Error refreshing database: {e}")
        await update.message.reply_text(f"❌ Ошибка скачивания: {e}")

        # Удаляем временный файл если что-то пошло не так
        temp_path = "./data/products.jsonl.part"
        if os.path.exists(temp_path):
            os.unlink(temp_path)

# ========= ДНЕВНИК/СВОДКИ =========
def _safe_list(v):
    return v if isinstance(v, list) else []

def _aggregate_food_day(food_entries: List[Dict[str, Any]], day: str) -> Optional[Dict[str, int]]:
    kcal = p = f = c = 0
    has = False
    for x in food_entries:
        if isinstance(x, dict) and isinstance(x.get("ts", ""), str) and x["ts"][:10] == day:
            has = True
            try:
                # Более безопасное извлечение числовых значений
                kcal += float(x.get("kcal", 0)) if x.get("kcal") is not None else 0
                p += float(x.get("p", 0)) if x.get("p") is not None else 0
                f += float(x.get("f", 0)) if x.get("f") is not None else 0
                c += float(x.get("c", 0)) if x.get("c") is not None else 0
            except (ValueError, TypeError):
                pass
    return {"kcal": int(kcal), "p": int(p), "f": int(f), "c": int(c)} if has else None

def format_diary_entries_for_editing(entries: List[Dict[str, Any]], entry_type: str) -> str:
    """Форматирует записи дневника для редактирования"""
    if not entries:
        return f"Нет записей в дневнике {entry_type}."

    lines = [f"📋 Записи в дневнике ({entry_type}):"]
    display_entries = entries[-10:]  # Показываем последние 10 записей

    for i, entry in enumerate(display_entries, 1):
        ts = entry.get("ts", "")
        date_part = ts[:10] if len(ts) >= 10 else ts
        time_part = ts[11:16] if len(ts) >= 16 else ""

        if entry_type == "питания":
            text = entry.get("text", "Запись без описания")
            # Обрезаем очень длинный текст
            if len(text) > 100:
                text = text[:97] + "..."
            kcal = entry.get("kcal", 0)
            p = entry.get("p", 0)
            f = entry.get("f", 0)
            c = entry.get("c", 0)
            lines.append(f"{i}. [{date_part} {time_part}] {text} - {kcal} ккал (Б{p}/Ж{f}/У{c})")
        else:  # тренировки
            text = entry.get("text", "Тренировка")
            # Обрезаем очень длинный текст
            if len(text) > 80:
                text = text[:77] + "..."
            workout_type = entry.get("type", "тренировка")
            kcal = entry.get("kcal", 0)
            avg_hr = entry.get("avg_hr")
            hr_text = f", ср. пульс {avg_hr}" if avg_hr else ""
            lines.append(f"{i}. [{date_part} {time_part}] {workout_type}: {text}{hr_text} - {kcal} ккал")

    lines.append(f"\nВсего записей: {len(entries)}")
    lines.append("Введите номер записи для удаления (1-10), 'все' для удаления всех записей или 'отмена':")
    return "\n".join(lines)

async def show_diaries(update: Update, st: Dict[str, Any]):
    try:
        diaries, daily_energy = st.get("diaries", {}), st.get("daily_energy", {})
        foods, trains, metrics = _safe_list(diaries.get("food")), _safe_list(diaries.get("train")), _safe_list(diaries.get("metrics"))
        days_set = {
            ts[:10]
            for ts in [x.get("ts") for lst in (foods, trains, metrics) for x in lst if isinstance(x, dict) and isinstance(x.get("ts"), str) and len(x["ts"]) >= 10]
        }
        days_set.update(k for k in daily_energy.keys() if isinstance(k, str) and len(k) == 10)
        if not days_set:
            days_set.add(today_key())
        days = sorted(days_set, reverse=True)[:7]
        lines = ["Сводка последних дней: 📅"]
        for d in days:
            agg = _aggregate_food_day(foods, d)
            day_trains = [t for t in trains if isinstance(t, dict) and isinstance(t.get("ts"), str) and t["ts"].startswith(d)]
            total_train_kcal = sum(int(t.get("kcal", 0)) for t in day_trains)
            lines.append(f"\n{d}")
            if agg and agg['kcal'] > 0:
                lines.append(f"🍏 Еда: ~{agg['kcal']} ккал; Б{agg['p']}/Ж{agg['f']}/У{agg['c']}")
            else:
                de = daily_energy.get(d) or {}
                eaten_kcal = int(de.get('in', 0))
                if eaten_kcal > 0:
                    lines.append(f"🍏 Еда: ~{eaten_kcal} ккал")
                else:
                    lines.append("🍏 Еда: пусто")
            if day_trains:
                lines.append("💪 Тренировки:")
                # Группируем тренировки и убираем дубликаты
                unique_trains = []
                seen_descriptions = set()
                for t in day_trains:
                    description = t.get('text', '').strip()
                    train_type = t.get('type', 'тренировка')
                    kcal = int(t.get('kcal', 0))

                    # Пропускаем записи с нулевой калорийностью и дубликаты
                    if kcal > 0 and description not in seen_descriptions:
                        unique_trains.append(t)
                        seen_descriptions.add(description)

                if unique_trains:
                    for t in unique_trains[:3]:  # Показываем максимум 3 уникальные тренировки за день
                        train_type = t.get('type', 'тренировка')
                        kcal = int(t.get('kcal', 0))
                        hr_info = f", ср. пульс {t['avg_hr']}" if t.get('avg_hr') else ""
                        lines.append(f"— {train_type}{hr_info}, ~{kcal} ккал")

                    if len(unique_trains) > 3:
                        lines.append(f"...и ещё {len(unique_trains)-3} тренировок")
                    lines.append(f"Всего сожжено: ~{total_train_kcal} ккал")
                else:
                    lines.append("💪 Тренировки: пусто")
            else:
                de = daily_energy.get(d) or {}
                burned_kcal = int(de.get('out', 0))
                if burned_kcal > 0:
                    lines.append(f"💪 Тренировки: ~{burned_kcal} ккал")
                else:
                    lines.append("💪 Тренировки: пусто")
        eat, burn = day_totals(st)
        if profile_complete(st["profile"]):
            k = calc_kbju_weight_loss(st["profile"])
            lines.append(
                f"\nСегодняшняя сводка:\n— съедено: ~{eat} ккал; сожжено: ~{burn} ккал\n— рекомендация на похудение: ~{k['target_kcal']} ккал/сут"
            )
            # Добавляем остаток калорий и БЖУ
            remaining_kcal = k['target_kcal'] - eat
            today_agg = _aggregate_food_day(foods, today_key())
            consumed_p = today_agg['p'] if today_agg and today_agg['kcal'] > 0 else 0
            consumed_f = today_agg['f'] if today_agg and today_agg['kcal'] > 0 else 0
            consumed_c = today_agg['c'] if today_agg and today_agg['kcal'] > 0 else 0

            remaining_p = max(0, k['protein_g'] - consumed_p)
            remaining_f = max(0, k['fat_g'] - consumed_f)
            remaining_c = max(0, k['carbs_g'] - consumed_c)

            lines.append(f"— Осталось на сегодня: ~{max(0, remaining_kcal)} ккал")
            if remaining_kcal > 0: # Показываем БЖУ только если есть что есть
                lines.append(f"  Б: ~{remaining_p} г, Ж: ~{remaining_f} г, У: ~{remaining_c} г")
        else:
            lines.append("\nСегодняшняя сводка: заполните анкету, чтобы получить рекомендации. 🙂")

        # Добавляем кнопки редактирования
        keyboard = [
            [KeyboardButton("✏️ Редактировать питание"), KeyboardButton("✏️ Редактировать тренировки")],
            [KeyboardButton("⬅️ Назад")]
        ]

        # Проверяем длину сообщения и разбиваем на части если нужно
        full_text = "\n".join(lines)
        max_length = 4000  # Telegram limit is 4096, оставляем запас

        if len(full_text) <= max_length:
            await update.message.reply_text(
                full_text,
                reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
            )
        else:
            # Разбиваем на части
            parts = []
            current_part = []
            current_length = 0

            for line in lines:
                if current_length + len(line) + 1 > max_length:
                    if current_part:
                        parts.append("\n".join(current_part))
                        current_part = [line]
                        current_length = len(line)
                else:
                    current_part.append(line)
                    current_length += len(line) + 1

            if current_part:
                parts.append("\n".join(current_part))

            # Отправляем части сообщения
            for i, part in enumerate(parts):
                if i == len(parts) - 1:  # Последняя часть с кнопками
                    await update.message.reply_text(
                        part,
                        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
                    )
                else:
                    await update.message.reply_text(part)
    except Exception as e:
        logger.exception(f"show_diaries error: {e}")
        await update.message.reply_text("Не удалось показать дневники. 🙏", reply_markup=role_keyboard(st.get("current_role")))

async def show_points(update: Update, st: Dict[str, Any]):
    u = update.effective_user
    if not check_feature_access(st, u.id, "analytics"):
        await update.message.reply_text(
            f"Ваши баллы: {st.get('points', 0)} 🏅\n\n"
            "Рейтинг, топ-10 и аналитика доступны на тарифе «Максимум». Это поможет отслеживать прогресс и соревноваться с другими! 🚀",
            reply_markup=role_keyboard(st.get("current_role")),
        )
        return

    pts = int(st.get("points", 0))
    board_all = leaderboard_all()
    total = len(board_all)
    rank = None
    uid_str = str(u.id)
    for i, user_data in enumerate(board_all, 1):
        if str(user_data["user_id"]) == uid_str:
            rank = i
            break
    top = board_all[:10]
    lines = [f"Ваши баллы: {pts} 🏅"]
    if rank:
        lines.append(f"Ваше место: {rank} из {total} 🙂")
    else:
        lines.append(f"Ваше место: вне общего рейтинга ({total} участников).")
    if top:
        lines.append("\nТоп‑10:")
        for i, user_data in enumerate(top, 1):
            mark = " 👑" if i == 1 else " 🔥" if i <= 3 else " 🎉" if i <= 10 else ""
            you_mark = " (вы)" if str(user_data["user_id"]) == uid_str else ""
            lines.append(f"{i}. Участник {user_data['user_id'][:5]}...: {user_data['points']}{mark}{you_mark}")
    if rank == 1:
        lines.append("\nТы на первом месте! 👑 Огромное спасибо за усердие и дисциплину. Ты пример для всех! 🥇")
    elif rank and rank <= 3:
        lines.append("\nОтличный темп, ты в тройке лидеров! 🔥 Чуть-чуть стабильности — и вершина совсем рядом. Вперёд! 🚀")
    elif rank and rank <= 10:
        lines.append("\nПоздравляю с топ‑10! 🎉 Ещё немного регулярности — и попадешь в призы. Я рядом и помогу. 👏")
    await update.message.reply_text("\n".join(lines), reply_markup=role_keyboard(st.get("current_role")))

# ========= МАГАЗИН / ПЛАТЕЖИ =========
async def shop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    st = load_state(u.id)
    level = get_user_access(st, u.id)

    status_map = {"free": "Бесплатный", "basic": "Базовый", "premium": "Премиум", "maximum": "Максимум"}

    text = (
        f"⭐ Магазин\n\nВаш текущий тариф: {status_map.get(level, 'Неизвестен')}\n\n"
        "Выберите тариф для покупки или улучшения:"
    )

    keyboard = []
    if level not in ("basic", "premium", "maximum"):
        keyboard.append([InlineKeyboardButton(f"Базовый ({PRICE_BASIC}⭐) — Безлимитные дневники", callback_data="buy:basic")])
    if level not in ("premium", "maximum"):
        keyboard.append([InlineKeyboardButton(f"Премиум ({PRICE_PREMIUM}⭐) — Дневники, рецепты, КБЖУ+", callback_data="buy:premium")])
    if level != "maximum":
        keyboard.append([InlineKeyboardButton(f"Максимум ({PRICE_MAXIMUM}⭐) — Всё + рейтинг и аналитика", callback_data="buy:maximum")])

    if not keyboard:
        text = f"⭐ Магазин\n\nВаш текущий тариф: {status_map.get(level, 'Неизвестен')}\n\nУ вас максимальный тариф. Спасибо за поддержку! 👑"

    await context.bot.send_message(chat_id=u.id, text=text, reply_markup=InlineKeyboardMarkup(keyboard))

async def send_invoice_for_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE, tier: str):
    u = update.effective_user
    prices = {"basic": PRICE_BASIC, "premium": PRICE_PREMIUM, "maximum": PRICE_MAXIMUM}
    titles = {"basic": "Тариф «Базовый»", "premium": "Тариф «Премиум»", "maximum": "Тариф «Максимум»"}
    descriptions = {
        "basic": "Неограниченные записи в дневниках.",
        "premium": "Всё из Базового + рецепты и точный расчёт калорий.",
        "maximum": "Всё из Премиум + доступ к рейтингу и аналитике.",
    }
    price = prices.get(tier)
    if not price:
        return
    payload = f"subscribe_{tier}_{u.id}"
    await context.bot.send_invoice(
        chat_id=u.id,
        title=titles[tier],
        description=descriptions[tier],
        payload=payload,
        provider_token=TELEGRAM_PAYMENT_PROVIDER_TOKEN,
        currency="XTR",
        prices=[LabeledPrice("Цена", price)],
    )

async def send_invoice_for_motivation(update: Update, context: ContextTypes.DEFAULT_TYPE, role: str):
    u = update.effective_user
    title = "Мотивационное сообщение"
    description = f"Случайное сообщение от {'тренера' if role == 'trainer' else 'нутрициолога'}."
    payload = f"motivation_{role}_{u.id}"
    await context.bot.send_invoice(
        chat_id=u.id,
        title=title,
        description=description,
        payload=payload,
        provider_token=TELEGRAM_PAYMENT_PROVIDER_TOKEN,
        currency="XTR",
        prices=[LabeledPrice("Цена", PRICE_MOTIVATION)],
    )

async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    if query.invoice_payload.startswith(("subscribe_", "motivation_")):
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="Что-то пошло не так...")

async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    st = load_state(u.id)
    payment_info = update.message.successful_payment
    payload = payment_info.invoice_payload

    if payload.startswith("subscribe_"):
        _, tier, user_id = payload.split("_")
        st["access_level"] = tier
        save_state(u.id, st)
        await context.bot.send_message(chat_id=u.id, text=f"Оплата прошла успешно! Ваш тариф обновлён до «{tier.capitalize()}». Спасибо за поддержку! 🎉")
    elif payload.startswith("motivation_"):
        _, role, user_id = payload.split("_")
        msg_list = load_motivations().get(role, [])
        if msg_list:
            await context.bot.send_message(chat_id=u.id, text=random.choice(msg_list))
        else:
            await context.bot.send_message(chat_id=u.id, text="Не удалось найти сообщение, но спасибо за поддержку!")

# ========= МОТИВАЦИИ =========
def load_motivations() -> Dict[str, List[str]]:
    try:
        with open("motivations.json", "r", encoding="utf-8") as f:
            data = json.load(f)
            return {"trainer": data.get("coach", []), "nutri": data.get("nutritionist", [])}
    except Exception as e:
        logger.error(f"Could not load motivations.json: {e}")
        return {"trainer": ["Держись, ты можешь всё! 💪"], "nutri": ["Правильный выбор сегодня — залог здоровья завтра! 🍏"]}

# ========= КНОПКИ/ТЕКСТ =========
async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE, st: Dict[str, Any], text: str) -> bool:
    u = update.effective_user
    if text == "🛠 Обновить профиль":
        st["awaiting"] = "onb_gender"
        await update.message.reply_text("Обновим профиль. Пол:", reply_markup=ReplyKeyboardMarkup(GENDER_KB, resize_keyboard=True))
        return True
    if text == "⭐ Магазин":
        await shop_command(update, context)
        return True
    if text == "🥗 Нутрициолог":
        st["current_role"] = "nutri"
        st["awaiting"] = None
        await update.message.reply_text(
            "Привет! Я твой нутрициолог 🍏 Помогу с меню, КБЖУ и дневником. Пиши вопрос — отвечу по делу! 🙂",
            reply_markup=role_keyboard("nutri"),
        )
        return True
    if text == "🏋️ Фитнес-тренер":
        st["current_role"] = "trainer"
        st["awaiting"] = None
        await update.message.reply_text(
            "Я твой тренер 💪 Программа, техника, восстановление и профилактика травм по NASM — всё сделаем чётко! 🔥",
            reply_markup=role_keyboard("trainer"),
        )
        return True
    if text == "🍏 ПП‑рецепты":
        if has_full_access(u.id):
            # Показать кнопки категорий для пользователей с полным доступом
            recipe_keyboard = ReplyKeyboardMarkup([
                [KeyboardButton("🌅 Завтраки"), KeyboardButton("🍽️ Обеды")],
                [KeyboardButton("🌙 Ужины"), KeyboardButton("✨ Случайный рецепт")],
                [KeyboardButton("⬅️ Назад")]
            ], resize_keyboard=True)
            await update.message.reply_text(
                "Выберите категорию ПП-рецептов:",
                reply_markup=recipe_keyboard
            )
            st["awaiting"] = "recipe_category"
            return True
        elif get_user_access(st, u.id) == "free" and st["tmp"].get("used_random_recipe"):
            await update.message.reply_text(
                "Вы уже использовали свой бесплатный случайный рецепт на сегодня. Полный доступ к рецептам — в тарифе Премиум и выше. ⭐",
                reply_markup=role_keyboard("nutri"),
            )
            return True
        else:
            await recipes_root(update, context, st)
        return True
    if text == "📒 Мои дневники":
        await show_diaries(update, st)
        return True
    if text == "✏️ Редактировать питание":
        foods = _safe_list(st["diaries"].get("food", []))
        if not foods:
            await update.message.reply_text("Дневник питания пуст.", reply_markup=role_keyboard(st.get("current_role")))
            return True

        diary_text = format_diary_entries_for_editing(foods, "питания")
        st["awaiting"] = "edit_food_diary"
        await update.message.reply_text(
            diary_text,
            reply_markup=ReplyKeyboardMarkup([[KeyboardButton("Отмена")]], resize_keyboard=True)
        )
        return True
    if text == "✏️ Редактировать тренировки":
        trains = _safe_list(st["diaries"].get("train", []))
        if not trains:
            await update.message.reply_text("Дневник тренировок пуст.", reply_markup=role_keyboard(st.get("current_role")))
            return True

        diary_text = format_diary_entries_for_editing(trains, "тренировок")
        st["awaiting"] = "edit_train_diary"
        await update.message.reply_text(
            diary_text,
            reply_markup=ReplyKeyboardMarkup([[KeyboardButton("Отмена")]], resize_keyboard=True)
        )
        return True
    if text == "🏆 Мои баллы":
        await show_points(update, st)
        return True
    if text == "⬅️ Назад":
        st["current_role"] = None
        st["awaiting"] = None
        st["tmp"] = {}
        await update.message.reply_text("Главное меню:", reply_markup=role_keyboard(None))
        return True

    if st.get("current_role") == "nutri":
        if text == "⭐ Получить мотивашку от нутрициолога":
            if has_full_access(u.id):
                msg_list = load_motivations().get("nutri", [])
                if msg_list:
                    await update.message.reply_text(random.choice(msg_list), reply_markup=role_keyboard("nutri"))
                else:
                    await update.message.reply_text("Не удалось найти мотивационное сообщение 🙏", reply_markup=role_keyboard("nutri"))
            else:
                await send_invoice_for_motivation(update, context, "nutri")
            return True
        if text == "📏 ИМТ (BMI)":
            if not profile_complete(st["profile"]):
                await update.message.reply_text("Сначала заполните анкету. 🙂")
                return True
            bmi, cat = calc_bmi(float(st["profile"]["weight_kg"]), int(st["profile"]["height_cm"]))
            st["diaries"]["metrics"].append({"ts": now_ts(), "type": "bmi", "data": {"bmi": bmi, "cat": cat}})
            if award_once(st, "bmi"):
                add_points(st, 2)
            await update.message.reply_text(f"BMI: {bmi} — {cat}. Записано. ✅", reply_markup=role_keyboard("nutri"))
            return True
        if text == "📊 КБЖУ":
            if not check_feature_access(st, u.id, "calories_ai"):
                await update.message.reply_text("Точный расчёт КБЖУ доступен на тарифе Премиум и выше. ⭐")
                return True
            if not profile_complete(st["profile"]):
                await update.message.reply_text("Сначала заполните анкету. 🙂")
                return True
            k = calc_kbju_weight_loss(st["profile"])
            st["diaries"]["metrics"].append({"ts": now_ts(), "type": "kbju", "data": k})
            if award_once(st, "kbju"):
                add_points(st, 2)

            # Формируем подробное сообщение
            lines = [
                f"📊 ПЕРСОНАЛЬНЫЙ РАСЧЕТ КБЖУ",
                f"На основе данных USDA FDC и научных исследований",
                f"",
                f"👤 Ваши показатели:",
                f"• ИМТ: {k['bmi']} ({k['bmi_category']})",
                f"• Цель: {k['goal']}",
                f"",
                f"🔥 Метаболизм:",
                f"• Основной обмен (BMR): {k['bmr']} ккал/день",
                f"• Полная потребность (TDEE): {k['tdee']} ккал/день"
            ]

            if k['goal'] == "Похудеть":
                lines.extend([
                    f"• Рекомендуемый дефицит: {k['deficit_pct']}% ({abs(k['deficit_kcal'])} ккал)",
                    f"• Итоговая норма: {k['target_kcal']} ккал/день"
                ])
            elif k['goal'] == "Набрать массу":
                lines.extend([
                    f"• Рекомендуемый профицит: {abs(k['deficit_pct'])}% (+{abs(k['deficit_kcal'])} ккал)",
                    f"• Итоговая норма: {k['target_kcal']} ккал/день"
                ])
            else:
                lines.append(f"• Норма поддержания: {k['target_kcal']} ккал/день")

            lines.extend([
                f"",
                f"🥩 Распределение макронутриентов:",
                f"• Белки: {k['protein_g']} г ({k['protein_g']*4} ккал)",
                f"• Жиры: {k['fat_g']} г ({k['fat_g']*9} ккал)",
                f"• Углеводы: {k['carbs_g']} г ({k['carbs_g']*4} ккал)",
                f"",
                f"💊 Важные микронутриенты (USDA рекомендации):"
            ])

            micros = k.get('micronutrients', {})
            for nutrient, amount in micros.items():
                if nutrient != 'note':
                    nutrient_name = {
                        'iron': 'Железо',
                        'calcium': 'Кальций',
                        'vitamin_d': 'Витамин D',
                        'vitamin_c': 'Витамин C',
                        'magnesium': 'Магний',
                        'omega3': 'Омега-3'
                    }.get(nutrient, nutrient)
                    lines.append(f"• {nutrient_name}: {amount}")

            if micros.get('note'):
                lines.append(f"💡 {micros['note']}")

            lines.extend([
                f"",
                f"📋 Рекомендации:",
            ])

            for rec in k.get('recommendations', []):
                lines.append(f"• {rec}")

            lines.extend([
                f"",
                f"⚠️ {k['note']}"
            ])

            # Разбиваем длинное сообщение если нужно
            full_text = "\n".join(lines)
            if len(full_text) > 4000:
                # Первая часть
                first_part = "\n".join(lines[:20])
                await update.message.reply_text(first_part, reply_markup=role_keyboard("nutri"))
                # Вторая часть
                second_part = "\n".join(lines[20:])
                await update.message.reply_text(second_part, reply_markup=role_keyboard("nutri"))
            else:
                await update.message.reply_text(full_text, reply_markup=role_keyboard("nutri"))
            return True
        if text == "🍏 Обновить дневник":
            st["awaiting"] = "food_diary"
            await update.message.reply_text(
                "Отправьте фото с ПОДПИСЬЮ (название и ~граммы), либо просто текст блюда/продукта. 🍽️\n\n"
                "🔍 Система поиска:\n"
                "• Штрих-код (8-14 цифр) → Open Food Facts\n"
                "• Брендовые продукты → ИИ-анализ с интернет-поиском\n"
                "• Натуральные продукты → база USDA FDC\n\n"
                "Для точности укажите:\n"
                "• Штрих-код продукта (если есть на упаковке)\n"
                "• Название бренда для готовых продуктов\n"
                "• Вес в граммах\n"
                "• Конкретное название",
                reply_markup=role_keyboard("nutri"),
            )
            return True
        if text in ("🍽️ Сгенерировать меню", "🔄 Изменить меню"):
            if not profile_complete(st["profile"]):
                await update.message.reply_text("Сначала заполните анкету. 🙂")
                return True
            if text == "🔄 Изменить меню":
                st["awaiting"] = "menu_changes"
                await update.message.reply_text("Что поменять в меню? (например: «меньше углеводов вечером», «без лактозы»). Или «без изменений». ✍️")
                return True

            # Генерируем меню из pp_menu_60.json
            current_day = get_current_menu_day(st)
            menu_items = get_menu_for_day(current_day, "a")  # Используем вариант A для основного меню

            if not menu_items:
                # Fallback к старому методу если нет данных
                k = calc_kbju_weight_loss(st["profile"])
                await update.message.reply_text("Думаю… 🤔")
                plan = await generate_menu_via_llm(st["profile"], k["target_kcal"], changes="")
            else:
                k = calc_kbju_weight_loss(st["profile"])
                await update.message.reply_text("Думаю… 🤔")
                plan = await generate_menu_with_nutrition(st["profile"], menu_items, k["target_kcal"], changes="")
                increment_menu_day(st)  # Увеличиваем счетчик дня

            st["tmp"]["last_menu"], st["tmp"]["last_menu_kcal_target"] = plan, k["target_kcal"]
            add_points(st, 5)
            st["awaiting"] = "confirm_save_menu"
            await update.message.reply_text(plan, reply_markup=yes_no_kb("save_menu"))
            await update.message.reply_text("Записать это меню в дневник?", reply_markup=role_keyboard("nutri"))
            return True
        if text == "🔍 Поиск продуктов":
            st["awaiting"] = "search_product"
            await update.message.reply_text(
                "🔍 Умный поиск продуктов\n\n"
                "Введите название продукта для анализа КБЖУ:\n\n"
                "📦 Штрих-код (8-14 цифр):\n"
                "→ Open Food Facts (самый точный поиск)\n\n"
                "🏷️ Брендовые продукты (йогурты, батончики, готовая еда):\n"
                "→ ИИ-анализ с интернет-поиском\n\n"
                "🥬 Натуральные продукты (фрукты, овощи, мясо):\n"
                "→ База данных USDA FDC\n\n"
                "Примеры:\n"
                "• '4601234567890' (штрих-код)\n"
                "• 'Данон йогурт' (брендовый)\n"
                "• 'apple' или 'яблоко' (натуральный)\n"
                "• 'куриная грудка' (натуральный) 🎯"
            )
            return True
        if text == "Задать вопрос ❓":
            st["awaiting"] = "ask_nutri"
            await update.message.reply_text("Задайте вопрос по питанию. 💬")
            return True

    if st.get("current_role") == "trainer":
        if text == "⭐ Получить мотивашку от тренера":
            if has_full_access(u.id):
                msg_list = load_motivations().get("trainer", [])
                if msg_list:
                    await update.message.reply_text(random.choice(msg_list), reply_markup=role_keyboard("trainer"))
                else:
                    await update.message.reply_text("Не удалось найти мотивационное сообщение 🙏", reply_markup=role_keyboard("trainer"))
            else:
                await send_invoice_for_motivation(update, context, "trainer")
            return True
        if text == "📈 Пульсовые зоны":
            if not profile_complete(st["profile"]):
                await update.message.reply_text("Сначала заполните анкету. 🙂")
                return True
            age, hrrest = int(st["profile"]["age"]), get_last_hrrest(st)
            z = pulse_zones(age, hrrest)
            st["diaries"]["metrics"].append({"ts": now_ts(), "type": "zones", "data": {"hrrest": hrrest, "zones": z}})
            if award_once(st, "zones"):
                add_points(st, 2)
            txt = (
                "Ваши диапазоны ЧСС ❤️ (уд/мин):\n"
                f"Восстановление: {z['recovery'][0]}–{z['recovery'][1]}\nАэробная база: {z['aerobic'][0]}–{z['aerobic'][1]}\n"
                f"Темповая: {z['tempo'][0]}–{z['tempo'][1]}\nVO2max: {z['vo2'][0]}–{z['vo2'][1]}\nАнаэробная: {z['anaer'][0]}–{z['anaer'][1]}"
            )
            await update.message.reply_text(txt, reply_markup=role_keyboard("trainer"))
            st["awaiting"] = "zones_hrrest"
            await update.message.reply_text("Для уточнения пришл<ctrl63>ите новое значение пульса в покое (число).")
            return True
        if text == "🫁 МПК (VO2max)":
            if not profile_complete(st["profile"]):
                await update.message.reply_text("Сначала заполните анкету. 🙂")
                return True
            age, hrrest = int(st["profile"]["age"]), get_last_hrrest(st)
            hrmax = 208 - 0.7 * age
            vo2_est = 15.3 * (hrmax / hrrest)
            cat = vo2_category(st["profile"].get("gender", "Мужской"), vo2_est)
            st["diaries"]["metrics"].append({"ts": now_ts(), "type": "vo2", "data": {"vo2": round(vo2_est, 1), "cat": cat, "from": "estimate", "hrrest": hrrest}})
            if award_once(st, "vo2"):
                add_points(st, 2)

            explanation = (
                "🫁 МПК (Максимальное Потребление Кислорода) — это показатель аэробной выносливости. "
                "Он показывает, сколько миллилитров кислорода ваш организм может потребить за минуту на килограмм веса при максимальной нагрузке.\n\n"
                f"📊 Ваша оценка VO2max: {vo2_est:.1f} мл/кг/мин — {cat}\n"
                f"📈 Расчёт по формуле: HRmax/HRrest (HRrest={hrrest})\n\n"
                "💡 Если есть точное значение из тестирования — отправьте число в мл/кг/мин, и я обновлю данные. 🧪"
            )

            await update.message.reply_text(explanation, reply_markup=role_keyboard("trainer"))
            st["awaiting"] = "vo2_value"
            return True
        if text == "📋 Сгенерировать план тренировки":
            if not profile_complete(st["profile"]):
                await update.message.reply_text("Сначала заполните анкету. 🙂")
                return True
            st["awaiting"] = "workout_days"
            await update.message.reply_text("Сколько дней в неделю готовы тренироваться? Выберите 3 или 7.", reply_markup=ReplyKeyboardMarkup([["3", "7"]], resize_keyboard=True))
            return True
        if text == "🔄 Изменить план":
            if not profile_complete(st["profile"]):
                await update.message.reply_text("Сначала заполните анкету. 🙂")
                return True
            st["awaiting"] = "workout_changes"
            await update.message.reply_text("Что менять в плане? (например: «больше кардио», «без прыжков»). Или «без изменений». ✍️")
            return True
        if text == "➕ Внести тренировку":
            st["awaiting"] = "add_workout"
            await update.message.reply_text(
                "Опиши тренировку одной строкой: вид, длительность в мин, средний пульс (если знаешь).\nПример: «Бег 35 мин, пульс 152». 🏃‍♂️"
            )
            return True
        if text == "Задать вопрос ❓":
            st["awaiting"] = "ask_trainer"
            await update.message.reply_text("Спроси про восстановление, технику, прогрессию, профилактику травм — я здесь. 💬", reply_markup=role_keyboard("trainer"))
            return True
    return False

async def handle_text_or_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    st = load_state(u.id)
    msg, text = update.message, (update.message.text or "").strip()
    try:
        if await handle_onboarding(update, context, st):
            return
        if text and await handle_buttons(update, context, st, text):
            save_state(u.id, st)
            return
        awaiting = st.get("awaiting")

        # --- Дневник питания ---
        if awaiting == "food_diary":
            food_count = len(st["diaries"].get("food", []))
            train_count = len(st["diaries"].get("train", []))
            if get_user_access(st, u.id) == "free" and (food_count + train_count) >= FREE_DIARY_LIMIT:
                await update.message.reply_text(
                    f"Вы достигли лимита в {FREE_DIARY_LIMIT} записи в дневнике. "
                    "Для неограниченных записей перейдите на тариф «Базовый» или выше. ⭐",
                    reply_markup=role_keyboard(st.get("current_role")),
                )
                st["awaiting"] = None
                return

            caption, is_photo, src_text, reply = (msg.caption or "").strip(), bool(msg.photo), "", ""
            if is_photo and not caption and not text:
                await update.message.reply_text("Пожалуйста, добавьте ПОДПИСЬ к фото (название и ~граммы). 📸")
                return
            entry, src_text, reply = {"ts": now_ts()}, "", ""
            if is_photo:
                entry["photo"] = msg.photo[-1].file_id
                if caption:
                    entry["text"], src_text = caption, caption
                add_points(st, 3)
                reply = "Фото сохранено. +3 балла. ✅\n"
            elif text:
                entry["text"], src_text = text, text
                add_points(st, 2)
                reply = "Запись сохранена. +2 балла. ✅\n"

            est = None

            # Проверяем на штрих-код сначала, если доступен Open Food Facts
            if src_text and HAS_OPENFOOD:
                barcode_match = re.search(r'\b\d{8,14}\b', src_text)
                if barcode_match:
                    barcode = barcode_match.group()
                    logger.info(f"Detected barcode in diary: {barcode}")
                    try:
                        # Извлекаем граммы из текста
                        grams_match = re.search(r'(\d+(?:[.,]\d+)?)\s*(?:г|гр|g|grams?)\b', src_text, re.I)
                        user_grams = float(grams_match.group(1).replace(',', '.')) if grams_match else 100
                        logger.info(f"User grams for barcode: {user_grams}")
                        
                        barcode_result = await off_by_barcode(barcode, grams=user_grams)
                        logger.info(f"Barcode search result: {barcode_result}")
                        
                        if barcode_result and (barcode_result.get('kcal_100g') or barcode_result.get('kcal_portion')):
                            # Конвертируем в формат ai_meal_json
                            kcal_portion = barcode_result.get('kcal_portion')
                            kcal_100g = barcode_result.get('kcal_100g', 0)
                            
                            # Если нет порционных данных, рассчитываем сами
                            if kcal_portion is None and kcal_100g and user_grams:
                                factor = user_grams / 100.0
                                kcal_portion = kcal_100g * factor
                                protein_portion = (barcode_result.get('protein_100g', 0) or 0) * factor
                                fat_portion = (barcode_result.get('fat_100g', 0) or 0) * factor
                                carbs_portion = (barcode_result.get('carbs_100g', 0) or 0) * factor
                            else:
                                protein_portion = barcode_result.get('protein_portion', 0) or 0
                                fat_portion = barcode_result.get('fat_portion', 0) or 0
                                carbs_portion = barcode_result.get('carbs_portion', 0) or 0
                            
                            # Если все еще нет порционных данных, используем данные на 100г
                            if not kcal_portion and kcal_100g:
                                kcal_portion = kcal_100g
                                protein_portion = barcode_result.get('protein_100g', 0) or 0
                                fat_portion = barcode_result.get('fat_100g', 0) or 0
                                carbs_portion = barcode_result.get('carbs_100g', 0) or 0
                            
                            est = {
                                'kcal': int(kcal_portion or 0),
                                'protein_g': round(protein_portion, 1),
                                'fat_g': round(fat_portion, 1),
                                'carbs_g': round(carbs_portion, 1),
                                'notes': f"📦 Open Food Facts (штрих-код): {barcode_result.get('name', 'Продукт')} ({user_grams}г)",
                                'source_data': {
                                    'grams': user_grams,
                                    'kcal_100g': kcal_100g,
                                    'protein_100g': barcode_result.get('protein_100g', 0),
                                    'fat_100g': barcode_result.get('fat_100g', 0),
                                    'carbs_100g': barcode_result.get('carbs_100g', 0)
                                }
                            }
                            logger.info(f"Successfully found product by barcode: {barcode_result.get('name', 'Unknown')}, final est: {est}")
                        else:
                            logger.warning(f"Barcode result missing nutrition data: {barcode_result}")
                    except Exception as e:
                        logger.warning(f"Barcode search failed: {e}")
                        import traceback
                        logger.warning(f"Barcode search traceback: {traceback.format_exc()}")

            # Если штрих-код не сработал, используем обычный поиск
            if not est:
                logger.info(f"Barcode search failed, trying general search for: {src_text}")
                est = await ai_meal_json(st["profile"], src_text) if src_text else None
                logger.info(f"General search result: {est}")

            if est and est.get("kcal"):
                kcal = int(est["kcal"])
                protein = round(est.get("protein_g", 0), 1)
                fat = round(est.get("fat_g", 0), 1)
                carbs = round(est.get("carbs_g", 0), 1)

                entry.update(
                    {
                        "kcal": kcal,
                        "p": protein,
                        "f": fat,
                        "c": carbs,
                    }
                )
                add_kcal_in(st, kcal)

                # Получаем информацию о граммовке из источника
                source_note = est.get('notes', 'анализ')
                source_data = est.get('source_data', {})
                grams = source_data.get('grams', 100)

                # Упрощенное отображение результата - всегда показываем что было рассчитано
                if grams != 100:
                    reply += f"✅ Рассчитано для {grams}г: {kcal} ккал (Б{protein:.1f}/Ж{fat:.1f}/У{carbs:.1f}). 🍽️"
                else:
                    reply += f"✅ Рассчитано для 100г: {kcal} ккал (Б{protein:.1f}/Ж{fat:.1f}/У{carbs:.1f}). 🍽️"

                # Добавляем информацию об источнике данных
                if "USDA FDC" in source_note:
                    reply += "\n📊 Источник: база USDA FDC"
                elif "Open Food Facts" in source_note:
                    reply += "\n📊 Источник: Open Food Facts"
                elif "Умный поиск" in source_note:
                    reply += "\n📊 Источник: умный поиск"
                elif "База данных" in source_note:
                    reply += "\n📊 Источник: база данных"
            else:
                reply += "❌ Продукт не найден. Попробуйте указать более точное название или добавьте бренд для готовых продуктов. 🙂"

            st["diaries"]["food"].append(entry)
            st["awaiting"] = None
            eat, burn = day_totals(st)
            if profile_complete(st["profile"]):
                k = calc_kbju_weight_loss(st["profile"])
                reply += f"\nСегодня: съедено ~{eat} ккал; сожжено ~{burn} ккал. Рекомендация: ~{k['target_kcal']} ккал. 📊"
            else:
                reply += f"\nСегодня: съедено ~{eat} ккал; сожжено ~{burn} ккал. 📊"
            await update.message.reply_text(reply, reply_markup=role_keyboard(st.get("current_role")))

        # --- Подтверждение сохранения меню ---
        elif awaiting == "confirm_save_menu":
            if text.lower() in ("да", "yes"):
                last_menu = st["tmp"].get("last_menu", "")
                kcal_match = re.search(r"Итого(?:\s+за\s+день)?:\s*~?(\d+)\s*ккал", last_menu)
                kcal = int(kcal_match.group(1)) if kcal_match else int(st["tmp"].get("last_menu_kcal_target", 0))

                # Извлекаем БЖУ из текста меню - ищем в разных форматах
                protein = fat = carbs = 0
                bju_patterns = [
                    r"Б[:\s]*(\d+)[:\s]*г.*?Ж[:\s]*(\d+)[:\s]*г.*?У[:\s]*(\d+)[:\s]*г",
                    r"Б(\d+)/Ж(\d+)/У(\d+)",
                    r"Б(\d+)\s*/\s*Ж(\d+)\s*/\s*У(\d+)",
                    r"белки?\s*[:\-]?\s*(\d+).*?жиры?\s*[:\-]?\s*(\d+).*?углеводы?\s*[:\-]?\s*(\d+)",
                ]

                for pattern in bju_patterns:
                    bju_match = re.search(pattern, last_menu, re.IGNORECASE)
                    if bju_match:
                        protein = int(bju_match.group(1))
                        fat = int(bju_match.group(2))
                        carbs = int(bju_match.group(3))
                        break

                if kcal > 0:
                    add_kcal_in(st, kcal)
                    st["diaries"]["food"].append({
                        "ts": now_ts(),
                        "text": f"Меню на день: {last_menu}",
                        "kcal": kcal,
                        "p": protein,
                        "f": fat,
                        "c": carbs
                    })
                    add_points(st, 2)
                    await update.message.reply_text(f"Добавил полное меню (~{kcal} ккал, Б{protein}/Ж{fat}/У{carbs}) в дневник. +2 балла. ✅", reply_markup=role_keyboard(st.get("current_role")))
                else:
                    await update.message.reply_text("Не нашёл итоговую калорийность. Можно внести трапезы вручную. 🙂", reply_markup=role_keyboard(st.get("current_role")))
            else:
                await update.message.reply_text("Ок, не записываю. 👍", reply_markup=role_keyboard(st.get("current_role")))
            st["awaiting"] = None
            st["tmp"].pop("last_menu", None)
            st["tmp"].pop("last_menu_kcal_target", None)

        # --- Внести тренировку ---
        elif awaiting == "add_workout":
            food_count = len(st["diaries"].get("food", []))
            train_count = len(st["diaries"].get("train", []))
            if get_user_access(st, u.id) == "free" and (food_count + train_count) >= FREE_DIARY_LIMIT:
                await update.message.reply_text(
                    f"Лимит в {FREE_DIARY_LIMIT} записи в дневнике. Для безлимита нужен тариф «Базовый». ⭐",
                    reply_markup=role_keyboard(st.get("current_role")),
                )
                st["awaiting"] = None
                return

            desc = text
            mins = 45
            hrm = None
            try:
                mins_match = re.search(r"(\d{1,3})\s*(?:мин|m)", desc, re.I)
                mins = int(mins_match.group(1)) if mins_match else 45
                hr_match = re.search(r"(\d{2,3})\s*(?:уд|чсс|пульс)", desc, re.I)
                hrm = int(hr_match.group(1)) if hr_match else None
            except:
                pass
            kcal = estimate_kcal_workout(st["profile"], desc, mins, hrm)
            t = desc.lower()
            t_type = next(
                (name for kw, name in [
                    ("бег", "бег"), ("ходь", "ходьба"), ("йога", "йога"),
                    ("силов", "силовая"), ("гантел", "силовая"), ("штанг", "силовая"),
                    ("вел", "вело"), ("bike", "вело"), ("плав", "плавание"),
                    ("интервал", "интервалы"), ("hiit", "HIIT")
                ] if kw in t),
                "тренировка",
            )
            st["diaries"]["train"].append({"ts": now_ts(), "text": desc or "Тренировка", "type": t_type, "avg_hr": hrm, "kcal": kcal})
            add_kcal_out(st, kcal)
            add_points(st, 3)
            eat, burn = day_totals(st)
            k = calc_kbju_weight_loss(st["profile"]) if profile_complete(st["profile"]) else None
            msg = f"Записал: {t_type}{', пульс '+str(hrm) if hrm else ''}, ~{kcal} ккал. +3 балла. ✅\nСегодня: съедено ~{eat} ккал; сожжено ~{burn} ккал."
            if k:
                msg += f" Рекомендация: ~{k['target_kcal']} ккал/сут. 📊"
            await update.message.reply_text(msg, reply_markup=role_keyboard("trainer"))
            st["awaiting"] = None

        # --- Генерация плана ---
        elif awaiting == "workout_days":
            if text not in ("3", "7"):
                await update.message.reply_text("Пожалуйста, выберите 3 или 7.", reply_markup=ReplyKeyboardMarkup([["3", "7"]], resize_keyboard=True))
                return
            st["tmp"]["workout_days"] = int(text)
            st["awaiting"] = "workout_location"
            await update.message.reply_text("Где будете тренироваться?", reply_markup=ReplyKeyboardMarkup(LOCATION_KB, resize_keyboard=True))
        elif awaiting == "workout_location":
            if text not in ("Дом", "Зал", "Улица"):
                await update.message.reply_text("Выберите: Дом | Зал | Улица 🙂", reply_markup=ReplyKeyboardMarkup(LOCATION_KB, resize_keyboard=True))
                return
            st["tmp"]["workout_place"] = text
            if text == "Зал":
                await update.message.reply_text("Думаю… 🤔")
                plan = await generate_workout_via_llm(st["profile"], "Зал", "средняя оснащённость зала", "", days=st["tmp"].get("workout_days"))
                st["tmp"]["last_workout"] = plan
                add_points(st, 5)
                st["awaiting"] = None
                await update.message.reply_text(plan, reply_markup=yes_no_kb("save_workout"))
                await update.message.reply_text("Сохранить план? 🙂", reply_markup=role_keyboard("trainer"))
            else:
                st["awaiting"] = "workout_inventory"
                await update.message.reply_text("Какой инвентарь есть? (эспандер, гантели…) Или «нет». ✍️", reply_markup=ReplyKeyboardRemove())
        elif awaiting == "workout_inventory":
            inv = text or "нет"
            st["tmp"]["last_inventory"] = inv
            place = st["tmp"].get("workout_place", "Дом")
            await update.message.reply_text("Думаю… 🤔")
            plan = await generate_workout_via_llm(st["profile"], place, inv, "", days=st["tmp"].get("workout_days"))
            st["tmp"]["last_workout"] = plan
            add_points(st, 5)
            st["awaiting"] = None
            await update.message.reply_text(plan, reply_markup=yes_no_kb("save_workout"))
            await update.message.reply_text("Сохранить план? 🙂", reply_markup=role_keyboard("trainer"))
        elif awaiting == "menu_changes":
            changes = "" if text.lower() == "без изменений" else text
            st["profile"]["preferences"]["menu_notes"] = changes or st["profile"]["preferences"].get("menu_notes", "")

            # Используем текущий день, но вариант B для изменения меню
            current_day = get_current_menu_day(st)
            menu_items = get_menu_for_day(current_day, "b")  # Используем вариант B для изменения меню

            k = calc_kbju_weight_loss(st["profile"])
            await update.message.reply_text("Думаю… 🤔")

            if not menu_items:
                # Fallback к старому методу
                plan = await generate_menu_via_llm(st["profile"], k["target_kcal"], changes)
            else:
                plan = await generate_menu_with_nutrition(st["profile"], menu_items, k["target_kcal"], changes)

            st["tmp"]["last_menu"], st["tmp"]["last_menu_kcal_target"] = plan, k["target_kcal"]
            add_points(st, 5)
            st["awaiting"] = "confirm_save_menu"
            await update.message.reply_text(plan, reply_markup=yes_no_kb("save_menu"))
            await update.message.reply_text("Записать это меню в дневник?", reply_markup=role_keyboard("nutri"))
        elif awaiting == "workout_changes":
            changes = "" if text.lower() == "без изменений" else text
            st["profile"]["preferences"]["workout_notes"] = changes or st["profile"]["preferences"].get("workout_notes", "")
            place = st["tmp"].get("workout_place", "Дом")
            inventory = st["tmp"].get("last_inventory", "нет")
            await update.message.reply_text("Думаю… 🤔")
            plan = await generate_workout_via_llm(st["profile"], place, inventory, changes, days=st["tmp"].get("workout_days"))
            st["tmp"]["last_workout"] = plan
            add_points(st, 5)
            st["awaiting"] = None
            await update.message.reply_text(plan, reply_markup=yes_no_kb("save_workout"))
            await update.message.reply_text("Сохранить план? 🙂", reply_markup=role_keyboard("trainer"))

        # --- Зоны / VO2 ---
        elif awaiting == "zones_hrrest":
            try:
                hrrest = int(text)
                assert 35 <= hrrest <= 110
                z = pulse_zones(int(st["profile"]["age"]), hrrest)
                st["diaries"]["metrics"].append({"ts": now_ts(), "type": "zones", "data": {"hrrest": hrrest, "zones": z}})
                txt = (
                    "Обновлённые диапазоны ЧСС ❤️ (уд/мин):\n"
                    f"Восстановление: {z['recovery'][0]}–{z['recovery'][1]}\nАэробная база: {z['aerobic'][0]}–{z['aerobic'][1]}\n"
                    f"Темповая: {z['tempo'][0]}–{z['tempo'][1]}\nVO2max: {z['vo2'][0]}–{z['vo2'][1]}\nАнаэробная: {z['anaer'][0]}–{z['anaer'][1]}"
                )
                await update.message.reply_text(txt, reply_markup=role_keyboard("trainer"))
                st["awaiting"] = None
            except Exception:
                await update.message.reply_text("Введите корректный пульс в покое (число от 35 до 110). 🙂")
        elif awaiting == "vo2_value":
            try:
                vo2 = float(text.replace(",", "."))
                cat = vo2_category(st["profile"].get("gender", "Мужской"), vo2)
                st["diaries"]["metrics"].append({"ts": now_ts(), "type": "vo2", "data": {"vo2": vo2, "cat": cat}})
                if award_once(st, "vo2_manual"):
                    add_points(st, 2)
                await update.message.reply_text(f"VO2max: {vo2:.1f} — {cat}. Записано. ✅", reply_markup=role_keyboard("trainer"))
                st["awaiting"] = None
            except Exception:
                await update.message.reply_text("Введите число, например 42. 🙂")

        # --- Выбор категории рецептов ---
        elif awaiting == "recipe_category":
            if text == "🌅 Завтраки":
                await show_recipe_category(update, context, "завтрак", st)
                return
            elif text == "🍽️ Обеды":
                await show_recipe_category(update, context, "обед", st)
                return
            elif text == "🌙 Ужины":
                await show_recipe_category(update, context, "ужин", st)
                return
            elif text == "✨ Случайный рецепт":
                await show_random_recipe(update, context, st)
                return
            elif text == "⬅️ Назад":
                st["current_role"] = None
                st["awaiting"] = None
                await update.message.reply_text("Главное меню:", reply_markup=role_keyboard(None))
                return
            else:
                await update.message.reply_text("Выберите категорию из предложенных вариантов.")
                return

        # --- Выбор номера рецепта ---
        elif awaiting == "recipe_number":
            if text in ("🌅 Завтраки", "🍽️ Обеды", "🌙 Ужины", "✨ Случайный рецепт"):
                # Пользователь выбрал другую категорию
                st["awaiting"] = "recipe_category"
                await handle_text_or_photo(update, context)  # Рекурсивно обработать
                return
            elif text == "⬅️ Назад":
                st["current_role"] = None
                st["awaiting"] = None
                await update.message.reply_text("Главное меню:", reply_markup=role_keyboard(None))
                return

            try:
                recipe_num = int(text)
                category_recipes = st["tmp"].get("category_recipes", [])
                if 1 <= recipe_num <= min(5, len(category_recipes)):
                    recipe_dict = category_recipes[recipe_num - 1]
                    # Создаем Recipe объект из словаря
                    recipe = Recipe(
                        id=recipe_dict["id"],
                        title=recipe_dict["title"],
                        category=recipe_dict["category"],
                        kcal=recipe_dict["kcal"],
                        protein_g=recipe_dict["protein_g"],
                        fat_g=recipe_dict["fat_g"],
                        carbs_g=recipe_dict["carbs_g"],
                        steps=recipe_dict["steps"],
                        ingredients=recipe_dict.get("ingredients")
                    )
                    await show_recipe_detail(update, context, recipe, st)
                else:
                    await update.message.reply_text("Введите номер от 1 до 5 или выберите категорию.")
            except ValueError:
                await update.message.reply_text("Введите номер рецепта или выберите категорию.")
            return

        # --- Добавление рецепта в дневник ---
        elif awaiting == "add_recipe_to_diary":
            if text.lower() == "да":
                current_recipe = st["tmp"].get("current_recipe")
                if current_recipe:
                    st["diaries"]["food"].append({
                        "ts": now_ts(),
                        "text": f"Рецепт: {current_recipe['title']}",
                        "kcal": current_recipe["kcal"],
                        "p": current_recipe["protein_g"],
                        "f": current_recipe["fat_g"],
                        "c": current_recipe["carbs_g"]
                    })
                    add_kcal_in(st, current_recipe["kcal"])
                    add_points(st, 2)
                    await update.message.reply_text(
                        "Рецепт добавлен в дневник! +2 балла ✅",
                        reply_markup=ReplyKeyboardMarkup([
                            [KeyboardButton("🌅 Завтраки"), KeyboardButton("🍽️ Обеды")],
                            [KeyboardButton("🌙 Ужины"), KeyboardButton("✨ Случайный рецепт")],
                            [KeyboardButton("⬅️ Назад")]
                        ], resize_keyboard=True)
                    )
                st["awaiting"] = "recipe_category"
            elif text.lower() == "нет":
                await update.message.reply_text(
                    "Выберите другую категорию:",
                    reply_markup=ReplyKeyboardMarkup([
                        [KeyboardButton("🌅 Завтраки"), KeyboardButton("🍽️ Обеды")],
                        [KeyboardButton("🌙 Ужины"), KeyboardButton("✨ Случайный рецепт")],
                        [KeyboardButton("⬅️ Назад")]
                    ], resize_keyboard=True)
                )
                st["awaiting"] = "recipe_category"
            elif text == "⬅️ Назад":
                st["current_role"] = None
                st["awaiting"] = None
                await update.message.reply_text("Главное меню:", reply_markup=role_keyboard(None))
            else:
                await update.message.reply_text("Ответьте 'Да' или 'Нет'.")
            return

        # --- Редактирование дневника питания ---
        elif awaiting == "edit_food_diary":
            if text.lower() == "отмена":
                st["awaiting"] = None
                await update.message.reply_text("Отменено.", reply_markup=role_keyboard(st.get("current_role")))
                return

            if text.lower() == "все":
                foods = st["diaries"].get("food", [])
                if foods:
                    # Подсчитываем общую калорийность удаляемых записей
                    total_kcal = sum(entry.get("kcal", 0) for entry in foods)
                    # Очищаем список
                    st["diaries"]["food"] = []
                    # Корректируем дневную калорийность
                    day = ensure_day(st)
                    day["in"] = max(0, day["in"] - total_kcal)

                    await update.message.reply_text(
                        f"Удалены все записи питания ({len(foods)} записей, -{total_kcal} ккал)",
                        reply_markup=role_keyboard(st.get("current_role"))
                    )
                else:
                    await update.message.reply_text("Дневник питания уже пуст.", reply_markup=role_keyboard(st.get("current_role")))
                st["awaiting"] = None
                return

            try:
                entry_num = int(text)
                foods = st["diaries"].get("food", [])
                display_count = min(10, len(foods))

                if 1 <= entry_num <= display_count:
                    # Правильно вычисляем индекс для удаления из последних 10 записей
                    actual_index = len(foods) - display_count + entry_num - 1
                    removed_entry = foods.pop(actual_index)

                    # Корректируем дневную калорийность
                    removed_kcal = removed_entry.get("kcal", 0)
                    day = ensure_day(st)
                    day["in"] = max(0, day["in"] - removed_kcal)

                    entry_text = removed_entry.get('text', 'Без названия')
                    if len(entry_text) > 50:
                        entry_text = entry_text[:47] + "..."

                    await update.message.reply_text(
                        f"Запись удалена: {entry_text} (-{removed_kcal} ккал)",
                        reply_markup=role_keyboard(st.get("current_role"))
                    )
                    st["awaiting"] = None
                else:
                    await update.message.reply_text(f"Введите номер от 1 до {display_count}, 'все' или 'отмена'.")
            except ValueError:
                await update.message.reply_text("Введите номер записи, 'все' или 'отмена'.")
            return

        # --- Редактирование дневника тренировок ---
        elif awaiting == "edit_train_diary":
            if text.lower() == "отмена":
                st["awaiting"] = None
                await update.message.reply_text("Отменено.", reply_markup=role_keyboard(st.get("current_role")))
                return

            if text.lower() == "все":
                trains = st["diaries"].get("train", [])
                if trains:
                    # Подсчитываем общую калорийность удаляемых записей
                    total_kcal = sum(entry.get("kcal", 0) for entry in trains)
                    # Очищаем список
                    st["diaries"]["train"] = []
                    # Корректируем дневную калорийность
                    day = ensure_day(st)
                    day["out"] = max(0, day["out"] - total_kcal)

                    await update.message.reply_text(
                        f"Удалены все записи тренировок ({len(trains)} записей, -{total_kcal} ккал)",
                        reply_markup=role_keyboard(st.get("current_role"))
                    )
                else:
                    await update.message.reply_text("Дневник тренировок уже пуст.", reply_markup=role_keyboard(st.get("current_role")))
                st["awaiting"] = None
                return

            try:
                entry_num = int(text)
                trains = st["diaries"].get("train", [])
                display_count = min(10, len(trains))

                if 1 <= entry_num <= display_count:
                    # Правильно вычисляем индекс для удаления из последних 10 записей
                    actual_index = len(trains) - display_count + entry_num - 1
                    removed_entry = trains.pop(actual_index)

                    # Корректируем дневную калорийность
                    removed_kcal = removed_entry.get("kcal", 0)
                    day = ensure_day(st)
                    day["out"] = max(0, day["out"] - removed_kcal)

                    entry_text = removed_entry.get('text', 'Тренировка')
                    if len(entry_text) > 50:
                        entry_text = entry_text[:47] + "..."

                    await update.message.reply_text(
                        f"Запись удалена: {entry_text} (-{removed_kcal} ккал)",
                        reply_markup=role_keyboard(st.get("current_role"))
                    )
                    st["awaiting"] = None
                else:
                    await update.message.reply_text(f"Введите номер от 1 до {display_count}, 'все' или 'отмена'.")
            except ValueError:
                await update.message.reply_text("Введите номер записи, 'все' или 'отмена'.")
            return

        # --- Поиск продуктов ---
        elif awaiting == "search_product":
            if not text:
                await update.message.reply_text("Введите название продукта для поиска.")
                return

            await update.message.reply_text("🔍 Ищу продукт в базах данных...")

            # Новый агрегатор: USDA (натуралка) → Google CSE/JSON-LD → Vision (бренд)
            search_result = await search_product_on_internet(text)
            if search_result:
                # ---------- helpers ----------
                def _parse_amounts(s: str):
                    grams = ml = None
                    def _f(x): return float(x.replace(',', '.'))
                    for m in re.finditer(r'(\d+(?:[.,]\d+)?)\s*(кг|kg)\b', s, flags=re.I):
                        grams = (grams or 0) + _f(m.group(1)) * 1000
                    for m in re.finditer(r'(\d+(?:[.,]\d+)?)\s*(?:г|гр|g|grams?)\b', s, flags=re.I):
                        grams = (grams or 0) + _f(m.group(1))
                    for m in re.finditer(r'(\d+(?:[.,]\d+)?)\s*(?:л|l|литр(?:а|ов)?)\b', s, flags=re.I):
                        ml = (ml or 0) + _f(m.group(1)) * 1000
                    for m in re.finditer(r'(\d+(?:[.,]\d+)?)\s*(?:мл|ml|milliliter[s]?)\b', s, flags=re.I):
                        ml = (ml or 0) + _f(m.group(1))
                    return grams, ml
                def _fmt(x, digits=1, unit=" г"):
                    return (f"{x:.{digits}f}{unit}" if x is not None else "— г")
                def _fmt_kcal(x):
                    return (f"{x:.0f} ккал" if x is not None else "— ккал")
                def _scale_portion(res: dict, q_text: str):
                    # если аггрегатор не вернул порцию — выдёрнем из текста
                    g = res.get("portion_g"); m = res.get("portion_ml")
                    if g is None and m is None:
                        gg, mm = _parse_amounts(q_text)
                        if gg: res["portion_g"] = g = gg
                        if mm: res["portion_ml"] = m = mm
                    # посчитать на порцию
                    if g and res.get("kcal_100g") is not None:
                        k = g / 100.0
                        res["kcal_portion"]    = res["kcal_100g"]    * k
                        res["protein_portion"] = (res.get("protein_100g") or 0) * k if res.get("protein_100g") is not None else None
                        res["fat_portion"]     = (res.get("fat_100g")     or 0) * k if res.get("fat_100g")     is not None else None
                        res["carbs_portion"]   = (res.get("carbs_100g")   or 0) * k if res.get("carbs_100g")   is not None else None
                    elif m and res.get("kcal_100ml") is not None:
                        k = m / 100.0
                        res["kcal_portion"]    = res["kcal_100ml"]    * k
                        res["protein_portion"] = (res.get("protein_100ml") or 0) * k if res.get("protein_100ml") is not None else None
                        res["fat_portion"]     = (res.get("fat_100ml")     or 0) * k if res.get("fat_100ml")     is not None else None
                        res["carbs_portion"]   = (res.get("carbs_100ml")   or 0) * k if res.get("carbs_100ml")   is not None else None
                    return res
                # привести к единому виду и досчитать «на порцию»
                search_result = _scale_portion(dict(search_result), text)

                source_map = {
                    'google_cse_jsonld': '🔎 Google (JSON-LD)',
                    'google_cse_regex':  '🔎 Google (страница)',
                    'vision_ocr':        '🖼️ Google Vision OCR',
                    'usda':              '🌿 USDA FDC'
                }
                source = source_map.get(search_result.get('source'), '🔎 Источник не указан')

                lines = [
                    f"✅ Найден продукт:",
                    f"📦 {search_result.get('name') or '—'}",
                ]

                if search_result.get('brand'):
                    lines.append(f"🏷️ Бренд: {search_result['brand']}")

                # Блок «Порция», если указаны граммы/мл
                if search_result.get("portion_g") or search_result.get("portion_ml"):
                    portion_line = f"⚖️ Порция: {int(search_result.get('portion_g') or 0)} г" if search_result.get("portion_g") \
                                   else f"⚖️ Порция: {int(search_result.get('portion_ml') or 0)} мл"
                    lines.extend([
                        "",
                        portion_line,
                        "📊 На порцию:",
                        f"🔥 Калории: {_fmt_kcal(search_result.get('kcal_portion'))}",
                        f"🥩 Белки: {_fmt(search_result.get('protein_portion'))}",
                        f"🥑 Жиры: {_fmt(search_result.get('fat_portion'))}",
                        f"🍞 Углеводы: {_fmt(search_result.get('carbs_portion'))}",
                    ])

                # Блок «на 100 г / 100 мл» (что есть)
                if any(search_result.get(k) is not None for k in ("kcal_100g","protein_100g","fat_100g","carbs_100g")):
                    lines.extend([
                        "",
                        "📊 Питательная ценность на 100 г:",
                        f"🔥 Калории: {_fmt_kcal(search_result.get('kcal_100g'))}",
                        f"🥩 Белки: {_fmt(search_result.get('protein_100g'))}",
                        f"🥑 Жиры: {_fmt(search_result.get('fat_100g'))}",
                        f"🍞 Углеводы: {_fmt(search_result.get('carbs_100g'))}",
                    ])
                elif any(search_result.get(k) is not None for k in ("kcal_100ml","protein_100ml","fat_100ml","carbs_100ml")):
                    lines.extend([
                        "",
                        "📊 Питательная ценность на 100 мл:",
                        f"🔥 Калории: {_fmt_kcal(search_result.get('kcal_100ml'))}",
                        f"🥩 Белки: {_fmt(search_result.get('protein_100ml'))}",
                        f"🥑 Жиры: {_fmt(search_result.get('fat_100ml'))}",
                        f"🍞 Углеводы: {_fmt(search_result.get('carbs_100ml'))}",
                    ])

                lines.extend(["", f"💡 Источник: {source}"])

                await update.message.reply_text("\n".join(lines), reply_markup=role_keyboard("nutri"))
            else:
                await update.message.reply_text(
                    f"❌ Продукт '{text}' не найден ни в одной базе данных.\n\n"
                    "💡 Попробуйте:\n"
                    "• Указать более точное название\n"
                    "• Добавить название бренда для готовых продуктов\n"
                    "• Использовать английское название для натуральных продуктов\n"
                    "• Проверить правильность написания",
                    reply_markup=role_keyboard("nutri")
                )
            st["awaiting"] = None

        # --- Диалог с персонами ---
        elif awaiting in ("ask_nutri", "ask_trainer") or st.get("current_role") in ("nutri", "trainer") and text:
            if not text:
                return
            role = "nutri" if awaiting == "ask_nutri" else "trainer" if awaiting == "ask_trainer" else st.get("current_role")
            await update.message.reply_text("Думаю… 🤔")
            ans = await chat_llm([{"role": "system", "content": persona_system(role, st["profile"])}, {"role": "user", "content": text}])
            await update.message.reply_text(ans, reply_markup=role_keyboard(role))
            st["awaiting"] = None
        elif text:
            await update.message.reply_text("Доступные темы: питание и тренировки. Выберите раздел ниже. 🙂", reply_markup=role_keyboard(st.get("current_role")))
    except Exception as e:
        logger.exception(f"An error occurred in handler: {e}")
        await update.message.reply_text("Ой, что-то пошло не так. Попробуйте ещё раз или /start. 🙏")
    finally:
        save_state(u.id, st)

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query, u = update.callback_query, update.effective_user
    data = query.data or ""
    st = load_state(u.id)
    try:
        await query.answer()
        if data.startswith("buy:"):
            tier = data.split(":", 1)[1]
            await send_invoice_for_subscription(update, context, tier)
            await query.edit_message_reply_markup(None)
            return

        if data.startswith("save_menu:"):
            answer = data.split(":", 1)[1]
            if answer == "yes":
                last_menu = st["tmp"].get("last_menu", "")
                kcal_match = re.search(r"Итого(?:\s+за\s+день)?:\s*~?(\d+)\s*ккал", last_menu)
                kcal = int(kcal_match.group(1)) if kcal_match else int(st["tmp"].get("last_menu_kcal_target", 0))

                # Извлекаем БЖУ из текста меню - ищем в разных форматах
                protein = fat = carbs = 0
                bju_patterns = [
                    r"Б[:\s]*(\d+)[:\s]*г.*?Ж[:\s]*(\d+)[:\s]*г.*?У[:\s]*(\d+)[:\s]*г",
                    r"Б(\d+)/Ж(\d+)/У(\d+)",
                    r"Б(\d+)\s*/\s*Ж(\d+)\s*/\s*У(\d+)",
                    r"белки?\s*[:\-]?\s*(\d+).*?жиры?\s*[:\-]?\s*(\d+).*?углеводы?\s*[:\-]?\s*(\d+)",
                ]

                for pattern in bju_patterns:
                    bju_match = re.search(pattern, last_menu, re.IGNORECASE)
                    if bju_match:
                        protein = int(bju_match.group(1))
                        fat = int(bju_match.group(2))
                        carbs = int(bju_match.group(3))
                        break

                if kcal > 0:
                    add_kcal_in(st, kcal)
                    st["diaries"]["food"].append({
                        "ts": now_ts(),
                        "text": f"Меню на день: {last_menu}",
                        "kcal": kcal,
                        "p": protein,
                        "f": fat,
                        "c": carbs
                    })
                    add_points(st, 2)
                    await query.edit_message_text(query.message.text + f"\n\nЗаписано полное меню в дневник: +{kcal} ккал (Б{protein}/Ж{fat}/У{carbs})")
                else:
                    await query.edit_message_text(query.message.text + "\n\nНе нашёл итоговую калорийность. Можно внести трапезы вручную.")
            else:
                await query.edit_message_text(query.message.text + "\n\n👍 Ок, не сохраняю план.")
            st["tmp"].pop("last_menu", None)
            st["tmp"].pop("last_menu_kcal_target", None)
        elif data.startswith("save_workout:"):
            answer = data.split(":", 1)[1]
            if answer == "yes":
                last_workout = st["tmp"].get("last_workout", "")
                # Извлекаем примерную калорийность из плана тренировок
                kcal_match = re.search(r"Итого за неделю:\s*~?(\d+)\s*ккал", last_workout)
                weekly_kcal = int(kcal_match.group(1)) if kcal_match else 1500
                daily_kcal = weekly_kcal // 7  # Примерно делим на дни недели

                st["diaries"]["train"].append({
                    "ts": now_ts(),
                    "text": f"Новый тренировочный план (неделя)",
                    "type": "план тренировок",
                    "kcal": daily_kcal
                })
                add_points(st, 2)
                await query.edit_message_text(query.message.text + "\n\n✅ План сохранён в дневнике (+2 балла).")
            else:
                await query.edit_message_text(query.message.text + "\n\n👍 Ок, не сохраняю план.")
            st["tmp"].pop("last_workout", None)
    except Exception as e:
        logger.exception(f"Callback error: {e}")
        await query.answer("Ошибка.")
    finally:
        save_state(u.id, st)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception(f"Update {update} caused error {context.error}")

# ========= ПОИСК ПРОДУКТОВ И АНАЛИЗ ПИТАТЕЛЬНОСТИ =========
# Инициализация OpenAI клиента
client = None
if OPENAI_API_KEY:
    try:
        # Создаем клиент только с API ключом, без дополнительных параметров
        client = OpenAI(api_key=OPENAI_API_KEY)
        logger.info("✅ OpenAI client initialized successfully")
    except Exception as e:
        logger.error(f"❌ Failed to initialize OpenAI client: {e}")
        client = None
else:
    logger.warning("⚠️ OpenAI API key not provided")

def is_branded_product(query: str) -> bool:
    """Определяет, является ли продукт брендовым"""
    branded_keywords = {
        'bombbar', 'данон', 'danone', 'activia', 'nestle', 'milka', 'snickers', 
        'mars', 'protein', 'pancake', 'bar', 'батончик', 'йогурт', 'творожок',
        'напиток', 'коктейль', 'shake'
    }
    
    query_lower = query.lower()
    
    # Проверяем наличие брендовых ключевых слов
    if any(keyword in query_lower for keyword in branded_keywords):
        return True
    
    # Проверяем наличие штрих-кода
    if re.search(r'\b\d{8,14}\b', query):
        return True
    
    # Проверяем латинские символы (часто в названиях брендов)
    if re.search(r'[a-zA-Z]', query) and len(query.split()) <= 4:
        return True
    
    return False

async def chat_llm(messages: List[Dict[str, str]], model: str = MODEL_NAME, temperature: float = 0.7) -> str:
    """Отправляет запрос к OpenAI API"""
    if not client:
        return "ИИ недоступен. Проверьте настройки API ключа."
    
    try:
        def _call_openai():
            return client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature
            )
        
        response = await asyncio.to_thread(_call_openai)
        content = response.choices[0].message.content
        return content if content else "Получен пустой ответ от ИИ."
    except Exception as e:
        logger.error(f"OpenAI API error: {e}")
        return f"Ошибка ИИ: {e}"

def normalize_result(search_result: Dict[str, Any]) -> Dict[str, Any]:
    """Нормализует результат поиска: исправляет kJ->kcal, парсит '733 ккал/100г' и т.д."""
    result = search_result.copy()
    
    # Нормализация калорий - исправляем kJ и парсим строки
    kcal_keys = ['kcal_100g', 'energy-kcal_100g', 'energy_kcal_100g', 'kcal_serv']
    for key in kcal_keys:
        if key in result:
            kcal_val = result[key]
            if isinstance(kcal_val, str):
                # Парсим строки типа "733 ккал/100 г"
                kcal_match = re.search(r'(\d+(?:\.\d+)?)', str(kcal_val))
                if kcal_match:
                    kcal_val = float(kcal_match.group(1))
                else:
                    continue
            
            if isinstance(kcal_val, (int, float)):
                # Если значение > 500 и нет явного указания kcal, возможно это kJ
                if kcal_val > 500 and 'kj' in str(result.get(key, '')).lower():
                    result[key] = kcal_val / 4.184  # kJ -> kcal
                elif kcal_val > 1000:  # Эвристика для определения kJ
                    result[key] = kcal_val / 4.184
                else:
                    result[key] = kcal_val
    
    return result

# если JSON-LD дал числа «на порцию», пересчитаем в «на 100 г»
def _fix_portion_leak(res: dict) -> dict:
    r = dict(res)
    s = r.get("serving_g") or r.get("portion_g")
    if not s or s <= 0: 
        return r
    def maybe_fix(kind: str):
        serv = r.get(f"{kind}_serv") or r.get(f"{kind}_portion")
        m100 = r.get(f"{kind}_100g")
        if serv is None: 
            return
        exp = serv * 100.0 / s
        # если «на 100 г» отсутствует ИЛИ явно не совпадает с пересчетом — перепишем
        if (m100 is None) or (abs(exp - m100) / max(1.0, exp) > 0.5):
            r[f"{kind}_100g"] = exp
    for k in ("kcal","protein","fat","carbs"):
        maybe_fix(k)
    return r
    s = r.get("serving_g") or r.get("portion_g")
    if not s or s <= 0: 
        return r
    def maybe_fix(kind: str):
        serv = r.get(f"{kind}_serv") or r.get(f"{kind}_portion")
        m100 = r.get(f"{kind}_100g")
        if serv is None: 
            return
        exp = serv * 100.0 / s
        # если «на 100 г» отсутствует ИЛИ явно не совпадает с пересчетом — перепишем
        if (m100 is None) or (abs(exp - m100) / max(1.0, exp) > 0.5):
            r[f"{kind}_100g"] = exp
    for k in ("kcal","protein","fat","carbs"):
        maybe_fix(k)
    return r

# жесткий фильтр по правдоподобию для шоколада/батончиков и пр.
def _hard_plausible(res: dict, cat: str | None) -> bool:
    if not cat: 
        return True
    # шоколад с жиром < 10 г/100 г — почти точно мусор
    if cat == "chocolate" and (res.get("fat_100g") is not None) and (res["fat_100g"] < 10):
        return False
    return _plausible(res, cat)

def _plausible_branded(d):
    """Мягкий фильтр для брендовых продуктов"""
    return (d.get("kcal_100g") or d.get("kcal_100ml")) and (
        d.get("protein_100g") or d.get("fat_100g") or d.get("carbs_100g") or
        d.get("protein_100ml") or d.get("fat_100ml") or d.get("carbs_100ml")
    )

async def _gpt_extract_nutrition(text: str) -> Optional[dict]:
    """Fallback: извлекаем КБЖУ через GPT-4o mini"""
    if not client:
        return None
    try:
        prompt = f"""Извлеки из текста данные о питательности продукта. Верни ТОЛЬКО валидный JSON без дополнительного текста.

Текст продукта:
{text[:5000]}

Формат ответа:
{{
  "kcal_serv": число или null,
  "protein_serv": число или null, 
  "fat_serv": число или null,
  "carb_serv": число или null,
  "serving_g": 100
}}

Если данных нет - верни: {{"serving_g": 100}}"""
        
        def _call_openai():
            return client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "Ты извлекаешь питательные данные из текста. Отвечай ТОЛЬКО валидным JSON без объяснений."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0,
                timeout=20
            )
        
        response = await asyncio.to_thread(_call_openai)
        content = response.choices[0].message.content.strip()
        
        # Очищаем от markdown блоков если есть
        if content.startswith("```"):
            content = re.sub(r'^```(?:json)?\n?|```$', '', content, flags=re.MULTILINE)
            content = content.strip()
        
        # Пытаемся парсить JSON
        if content:
            parsed = json.loads(content)
            # Проверяем что это не пустой объект
            if parsed and any(parsed.get(k) for k in ["kcal_serv", "protein_serv", "fat_serv", "carb_serv"]):
                logger.info(f"GPT extracted nutrition: {parsed}")
                return parsed
        
        logger.info("GPT returned empty or invalid nutrition data")
        return None
        
    except json.JSONDecodeError as e:
        logger.warning(f"GPT JSON parse error: {e}, content: {content[:200] if 'content' in locals() else 'no content'}")
        return None
    except Exception as e:
        logger.warning(f"GPT extractor failed: {e}")
        return None

async def search_av_ru_branded(query: str, grams: Optional[float], ml: Optional[float]) -> Optional[Dict[str, Any]]:
    """Поиск брендовых продуктов на av.ru с автоматическим выбором Москвы"""
    try:
        import re
        from urllib.parse import quote
        
        # Очищаем запрос от граммов для поиска
        clean_query = re.sub(r'\d+\s*(?:г|гр|g|grams?|мл|ml)', '', query, flags=re.IGNORECASE).strip()
        if not clean_query:
            return None
            
        # Формируем URL для поиска в каталоге av.ru (Москва)
        search_url = f"https://av.ru/search?q={quote(clean_query)}&cityId=213"  # 213 - ID Москвы
        
        headers = {
            "User-Agent": "healco-lite/1.0 (+mailto:Rafael.sayadi@gmail.com)",
            "Accept-Language": "ru,ru-RU;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        }
        
        def _fetch_av():
            response = requests.get(search_url, headers=headers, timeout=20)
            if response.status_code != 200:
                logger.warning(f"av.ru search returned status {response.status_code}")
                return None
            return response.text
            
        html = await asyncio.to_thread(_fetch_av)
        if not html:
            return None
            
        # Парсим результаты поиска
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, 'html.parser')
        
        # Ищем первый продукт в результатах
        product_links = soup.select('a[href*="/product/"]')
        if not product_links:
            logger.info(f"No products found on av.ru for: {clean_query}")
            return None
            
        # Берем первый результат
        first_product_url = "https://av.ru" + product_links[0].get('href')
        logger.info(f"Found av.ru product: {first_product_url}")
        
        # Загружаем страницу продукта
        def _fetch_product():
            return requests.get(first_product_url, headers=headers, timeout=20)
            
        product_response = await asyncio.to_thread(_fetch_product)
        if product_response.status_code != 200:
            return None
            
        # Парсим питательные данные
        nutrition_data = _parse_av_ru_html(product_response.text)
        if not nutrition_data:
            return None
            
        # Адаптируем к нашему формату
        result = {
            'name': nutrition_data.get('name', clean_query),
            'brand': nutrition_data.get('brand', ''),
            'kcal_100g': nutrition_data.get('kcal_100g'),
            'protein_100g': nutrition_data.get('protein_100g'),
            'fat_100g': nutrition_data.get('fat_100g'),
            'carbs_100g': nutrition_data.get('carbs_100g'),
            'url': first_product_url,
            'source': 'av_ru'
        }
        
        # Рассчитываем на пользовательскую порцию если указана
        if grams and result.get('kcal_100g'):
            factor = grams / 100.0
            result.update({
                'portion_g': grams,
                'kcal_portion': result['kcal_100g'] * factor,
                'protein_portion': (result.get('protein_100g', 0) or 0) * factor,
                'fat_portion': (result.get('fat_100g', 0) or 0) * factor,
                'carbs_portion': (result.get('carbs_100g', 0) or 0) * factor,
            })
        elif ml and result.get('kcal_100g'):  # Для жидкостей используем те же данные на 100мл
            factor = ml / 100.0
            result.update({
                'portion_ml': ml,
                'kcal_portion': result['kcal_100g'] * factor,
                'protein_portion': (result.get('protein_100g', 0) or 0) * factor,
                'fat_portion': (result.get('fat_100g', 0) or 0) * factor,
                'carbs_portion': (result.get('carbs_100g', 0) or 0) * factor,
            })
            
        logger.info(f"av.ru result: {result.get('name')} - {result.get('kcal_100g')} kcal")
        return result
        
    except Exception as e:
        logger.warning(f"av.ru branded search failed: {e}")
        return None

def _parse_av_ru_html(html: str) -> Optional[Dict[str, Any]]:
    """Парсит HTML страницы продукта av.ru для извлечения питательных данных"""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, 'html.parser')
        
        # 1) Пробуем JSON-LD
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                data = json.loads(script.string or "")
                if isinstance(data, list):
                    for item in data:
                        result = _extract_from_json_ld(item)
                        if result:
                            return result
                else:
                    result = _extract_from_json_ld(data)
                    if result:
                        return result
            except:
                continue
                
        # 2) Пробуем window.__INITIAL_STATE__
        state_match = re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;<", html, flags=re.S)
        if state_match:
            try:
                state = json.loads(state_match.group(1))
                product = (state.get("product") or state.get("goods") or {}).get("item") or {}
                result = _extract_from_state(product)
                if result:
                    return result
            except:
                pass
                
        # 3) Парсим текст напрямую
        text = soup.get_text(" ", strip=True).lower()
        
        def extract_number(pattern):
            match = re.search(pattern, text)
            if match:
                try:
                    return float(match.group(1).replace(',', '.'))
                except:
                    return None
            return None
            
        kcal = extract_number(r"ккал[^\d]{0,5}(\d+[.,]?\d*)")
        protein = extract_number(r"бел(?:ок|ки)[^\d]{0,5}(\d+[.,]?\d*)")
        fat = extract_number(r"жир(?:ы)?[^\d]{0,5}(\d+[.,]?\d*)")
        carbs = extract_number(r"углевод(?:ы)?[^\d]{0,5}(\d+[.,]?\d*)")
        
        if any(v is not None for v in (kcal, protein, fat, carbs)):
            # Пробуем найти название
            title_tag = soup.select_one("h1")
            title = title_tag.get_text(strip=True) if title_tag else None
            
            if not title:
                og_title = soup.find("meta", attrs={"property": "og:title"})
                title = og_title.get("content") if og_title else "Продукт"
                
            return {
                "name": title,
                "brand": None,  # Можно попробовать извлечь из названия
                "kcal_100g": kcal,
                "protein_100g": protein,
                "fat_100g": fat,
                "carbs_100g": carbs,
            }
            
    except Exception as e:
        logger.warning(f"Error parsing av.ru HTML: {e}")
        
    return None

def _extract_from_json_ld(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Извлекает данные из JSON-LD структуры"""
    if not isinstance(data, dict):
        return None
        
    name = (data.get("name") or data.get("headline") or "").strip()
    brand = data.get("brand")
    if isinstance(brand, dict):
        brand = brand.get("name")
        
    nutrition = data.get("nutrition") or {}
    
    def get_nutrition_value(key):
        value = nutrition.get(key)
        if isinstance(value, dict):
            value = value.get("@value") or value.get("value")
        try:
            return float(str(value).replace(',', '.')) if value is not None else None
        except:
            return None
            
    kcal = get_nutrition_value("calories") or get_nutrition_value("energy") or get_nutrition_value("caloriesContent")
    protein = get_nutrition_value("proteinContent")
    fat = get_nutrition_value("fatContent") 
    carbs = get_nutrition_value("carbohydrateContent")
    
    if any(v is not None for v in (kcal, protein, fat, carbs)):
        return {
            "name": name or "Продукт",
            "brand": brand,
            "kcal_100g": kcal,
            "protein_100g": protein,
            "fat_100g": fat,
            "carbs_100g": carbs,
        }
    return None

def _extract_from_state(product: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Извлекает данные из window.__INITIAL_STATE__"""
    if not isinstance(product, dict):
        return None
        
    nutrition = product.get("nutrition") or {}
    if not nutrition:
        return None
        
    def to_float(value):
        try:
            return float(str(value).replace(',', '.')) if value is not None else None
        except:
            return None
            
    return {
        "name": product.get("title") or product.get("name") or "Продукт",
        "brand": product.get("brand"),
        "kcal_100g": to_float(nutrition.get("calories")),
        "protein_100g": to_float(nutrition.get("protein")),
        "fat_100g": to_float(nutrition.get("fat")),
        "carbs_100g": to_float(nutrition.get("carbohydrates")),
    }

async def search_product_on_internet(user_text: str) -> Optional[Dict[str, Any]]:
    """Поиск продукта в интернете с нормализацией через LLM"""
    try:
        # Нормализуем запрос через LLM
        info = await call_llm_normalizer(user_text)
        if not info:
            info = _heuristic_normalize(user_text)
        
        grams = info.get("portion_grams")
        mills = info.get("portion_ml")
        
        # Брендовый поиск
        if info.get("query_type") == "brand":
            # Приоритетный поиск на av.ru
            r = await search_av_ru_branded(user_text, grams, mills)
            if r:
                r['source'] = 'av_ru'
                return r
            
            # Fallback к Google CSE если av.ru не дал результата
            r = await search_branded_product_via_google(user_text)
            if r: 
                r['source'] = 'google_cse_jsonld'
                return r
        
        # Натуральный поиск через USDA
        if info.get("query_type") == "natural" and info.get("usda_queries"):
            for query in info["usda_queries"]:
                r = await search_usda_fdc_product(query, info.get("base_en"))
                if r:
                    r['source'] = 'usda'
                    return r
        
        # Последний шанс — USDA по сырому тексту (натуралка)
        r = await search_usda_fdc_product(user_text)
        if r:
            r['source'] = 'usda'
        return r
        
    except Exception as e:
        logger.error(f"search_product_on_internet error: {e}")
        return None

async def ai_meal_json(profile: Dict[str, Any], user_text: str) -> Optional[Dict[str, Any]]:
    """
    Главная функция поиска продуктов с использованием множественных источников
    Возвращает унифицированный результат с КБЖУ на 100г и на порцию пользователя
    """
    try:
        logger.info(f"=== AI MEAL SEARCH START ===")
        logger.info(f"Query: '{user_text}'")
        
        # Сначала пробуем нормализовать запрос через ИИ
        normalized = await call_llm_normalizer(user_text)
        logger.info(f"Normalized query: {normalized}")
        
        route_info = route_query_with_ai(normalized, user_text)
        logger.info(f"Route info: {route_info}")
        
        # Извлекаем граммы из запроса пользователя
        def extract_portion_grams(text: str) -> Optional[float]:
            m = re.search(r'(\d+(?:[.,]\d+)?)\s*(?:г|гр|g|grams?)\b', text, re.I)
            return float(m.group(1).replace(',', '.')) if m else None
        
        user_grams = extract_portion_grams(user_text)
        logger.info(f"User grams: {user_grams}")
        
        # Выбираем стратегию поиска на основе маршрута
        result = None
        
        if route_info["path"] == "brand":
            # Брендовый поиск через av.ru (приоритет) и Google CSE
            logger.info("=== BRANDED SEARCH ===")
            for query in route_info["queries"]:
                logger.info(f"Trying av.ru branded query: '{query}'")
                result = await search_av_ru_branded(query, user_grams, None)
                if result:
                    logger.info(f"Found av.ru branded result: {result.get('name', 'Unknown')}")
                    break
                    
                logger.info(f"Trying Google CSE branded query: '{query}'")
                result = await search_branded_product_via_google(query)
                if result:
                    logger.info(f"Found Google CSE branded result: {result.get('name', 'Unknown')}")
                    break
            
            # Fallback: попробуем обычный Google поиск для брендовых продуктов
            if not result:
                logger.info("No branded result found, trying Google search fallback")
                result = await search_google_for_product(user_text)
                if result:
                    logger.info(f"Found via Google search fallback: {result.get('name', 'Unknown')}")
                    result['source'] = 'smart_search'
        
        elif route_info["path"] == "usda":
            # Натуральный поиск через USDA FDC
            logger.info("=== USDA SEARCH ===")
            for query in route_info["queries"]:
                logger.info(f"Trying USDA query: '{query}'")
                result = await search_usda_fdc_product(query, route_info.get("base_en"))
                if result:
                    logger.info(f"Found USDA result: {result.get('name', 'Unknown')}")
                    break
        
        # Fallback поиски если основной не сработал
        if not result:
            logger.info("=== FALLBACK SEARCHES ===")
            
            # 1. FatSecret API (если не сработал в брендовом поиске)
            if FATSECRET_KEY and FATSECRET_SECRET:
                logger.info("Trying FatSecret API as fallback...")
                try:
                    # Проверяем штрих-код
                    barcode_match = re.search(r'\b\d{8,14}\b', user_text)
                    if barcode_match:
                        barcode = barcode_match.group()
                        logger.info(f"Searching FatSecret by barcode: {barcode}")
                        fid = await _fs_find_by_barcode(barcode)
                        if fid:
                            food = await _fs_get_food(fid)
                            if food:
                                result = _fs_norm(food, user_grams, None)
                                if result and result.get('kcal_100g'):
                                    result['source'] = '🧩 FatSecret'
                                    logger.info(f"Found FatSecret result by barcode: {result.get('name', 'Unknown')}")
                    
                    # Поиск по названию если штрих-код не сработал
                    if not result:
                        clean_query = re.sub(r'\d+\s*(?:г|гр|g|grams?)', '', user_text, flags=re.IGNORECASE).strip()
                        if clean_query:
                            logger.info(f"Searching FatSecret by name: {clean_query}")
                            food = await _fs_search_best(clean_query)
                            if food:
                                result = _fs_norm(food, user_grams, None)
                                if result and result.get('kcal_100g'):
                                    result['source'] = '🧩 FatSecret'
                                    logger.info(f"Found FatSecret result by name: {result.get('name', 'Unknown')}")
                                    
                except Exception as e:
                    logger.warning(f"FatSecret fallback search failed: {e}")
            
            # 2. Типичные данные для популярных продуктов
            if not result:
                logger.info("Trying typical nutrition data...")
                result = get_typical_nutrition(user_text)
                if result:
                    logger.info(f"Found typical data: {result.get('name', 'Unknown')}")
            
            # 2. Внешняя JSONL база
            if not result:
                logger.info("Trying external JSONL database...")
                try:
                    products = await load_external_jsonl_database()
                    logger.info(f"Loaded {len(products)} products from external database")
                    if products:
                        result = await search_external_jsonl_product(user_text, products)
                        if result:
                            logger.info(f"Found in external JSONL: {result.get('name', 'Unknown')}")
                    else:
                        logger.info("No products in external database")
                except Exception as e:
                    logger.warning(f"External JSONL search failed: {e}")
            
            # 3. Open Food Facts (новый модуль)
            if not result and HAS_OPENFOOD:
                logger.info("Trying Open Food Facts (new module)...")
                try:
                    # Определяем грамы из запроса
                    grams_match = re.search(r'(\d+(?:[.,]\d+)?)\s*(?:г|гр|g|grams?)\b', user_text, re.I)
                    user_grams_off = float(grams_match.group(1).replace(',', '.')) if grams_match else None
                    
                    # Сначала пробуем поиск по штрих-коду если есть цифры
                    barcode_match = re.search(r'\b\d{8,14}\b', user_text)
                    if barcode_match:
                        barcode = barcode_match.group()
                        logger.info(f"Detected barcode: {barcode}")
                        result = await off_by_barcode(barcode, grams=user_grams_off)
                        if result:
                            logger.info(f"Found by barcode in Open Food Facts: {result.get('name', 'Unknown')}")
                    
                    # Если штрих-код не сработал, пробуем поиск по названию
                    if not result:
                        clean_query_off = re.sub(r'\d+\s*(?:г|гр|g|grams?|мл|ml)', '', user_text, flags=re.IGNORECASE).strip()
                        if clean_query_off:
                            result = await off_search_by_name(clean_query_off, grams=user_grams_off)
                            if result:
                                logger.info(f"Found by name in Open Food Facts: {result.get('name', 'Unknown')}")
                    
                    # Если не нашли через новый модуль, пробуем старый метод
                    if not result:
                        logger.info("Trying legacy Open Food Facts...")
                        result = await search_openfoodfacts_product(user_text)
                        if result:
                            logger.info(f"Found in legacy Open Food Facts: {result.get('name', 'Unknown')}")
                        else:
                            logger.info("No results from Open Food Facts")
                except Exception as e:
                    logger.warning(f"Open Food Facts search failed: {e}")
            elif not result:
                logger.info("Trying legacy Open Food Facts...")
                try:
                    result = await search_openfoodfacts_product(user_text)
                    if result:
                        logger.info(f"Found in legacy Open Food Facts: {result.get('name', 'Unknown')}")
                    else:
                        logger.info("No results from legacy Open Food Facts")
                except Exception as e:
                    logger.warning(f"Legacy Open Food Facts search failed: {e}")
            
            # 4. Google поиск как последний резерв
            if not result:
                logger.info("Trying Google search fallback...")
                try:
                    result = await search_google_for_product(user_text)
                    if result:
                        logger.info(f"Found via Google search: {result.get('name', 'Unknown')}")
                    else:
                        logger.info("No results from Google search")
                except Exception as e:
                    logger.warning(f"Google search failed: {e}")
        
        if not result:
            logger.info("=== NO RESULTS FOUND ===")
            return None
        
        # Нормализация энергий (фиксируем kJ и «733 ккал/100 г»)
        result = normalize_result(result)
        
        # Обновляем маппинг источников
        source_map = {
            'av_ru':             '🛒 AV.RU',
            'google_cse_jsonld': '🔎 Google (JSON-LD)',
            'google_cse_regex':  '🔎 Google (страница)',
            'vision_ocr':        '🖼️ Google Vision OCR',
            'usda':              '🌿 USDA FDC',
            'external_database': '📊 База данных',
            'openfoodfacts':     '📦 Open Food Facts',
            'smart_search':      '🔍 Умный поиск',
            'fatsecret':         '🧩 FatSecret',
            '🧩 FatSecret':      '🧩 FatSecret'
        }
        
        # Рассчитываем КБЖУ на пользовательскую порцию
        if user_grams and user_grams != 100:
            logger.info(f"Calculating nutrition for {user_grams}g portion")
            factor = user_grams / 100.0
            
            # Определяем источник для формирования notes
            source_key = result.get('source', '')
            source_url = result.get('url', '')
            
            # Используем обновленный маппинг источников
            source_display = source_map.get(source_key, '📊 База данных')
            
            # Fallback для определения источника по URL если source не задан
            if not source_key:
                if 'usda' in source_url or 'fdc.nal.usda.gov' in source_url:
                    source_display = '🌿 USDA FDC'
                elif 'openfoodfacts' in source_url:
                    source_display = '📦 Open Food Facts'
                elif source_url == 'external_database':
                    source_display = '📊 База данных'
                else:
                    source_display = '🔍 Умный поиск'
            
            notes = f"{source_display}: {result.get('name', 'Продукт')} ({user_grams}г)"
            
            return {
                'kcal': int(result.get('kcal_100g', 0) * factor),
                'protein_g': round(result.get('protein_100g', 0) * factor, 1),
                'fat_g': round(result.get('fat_100g', 0) * factor, 1),
                'carbs_g': round(result.get('carbs_100g', 0) * factor, 1),
                'notes': notes,
                'source_data': {
                    'grams': user_grams,
                    'kcal_100g': result.get('kcal_100g', 0),
                    'protein_100g': result.get('protein_100g', 0),
                    'fat_100g': result.get('fat_100g', 0),
                    'carbs_100g': result.get('carbs_100g', 0)
                }
            }
        else:
            # Возвращаем данные на 100г
            source_key = result.get('source', '')
            source_url = result.get('url', '')
            
            # Используем обновленный маппинг источников
            source_display = source_map.get(source_key, '📊 База данных')
            
            # Fallback для определения источника по URL если source не задан
            if not source_key:
                if 'usda' in source_url or 'fdc.nal.usda.gov' in source_url:
                    source_display = '🌿 USDA FDC'
                elif 'openfoodfacts' in source_url:
                    source_display = '📦 Open Food Facts'
                elif source_url == 'external_database':
                    source_display = '📊 База данных'
                else:
                    source_display = '🔍 Умный поиск'
            
            notes = f"{source_display}: {result.get('name', 'Продукт')} (100г)"
            
            return {
                'kcal': int(result.get('kcal_100g', 0)),
                'protein_g': round(result.get('protein_100g', 0), 1),
                'fat_g': round(result.get('fat_100g', 0), 1),
                'carbs_g': round(result.get('carbs_100g', 0), 1),
                'notes': notes,
                'source_data': {
                    'grams': 100,
                    'kcal_100g': result.get('kcal_100g', 0),
                    'protein_100g': result.get('protein_100g', 0),
                    'fat_100g': result.get('fat_100g', 0),
                    'carbs_100g': result.get('carbs_100g', 0)
                }
            }
        
    except Exception as e:
        logger.error(f"ai_meal_json error: {e}")
        return None

def get_last_hrrest(st: Dict[str, Any], default: int = 60) -> int:
    """Получает последний записанный пульс покоя из метрик"""
    metrics = st.get("diaries", {}).get("metrics", [])
    for m in reversed(metrics):
        if isinstance(m, dict) and m.get("type") == "zones":
            data = m.get("data", {})
            hrrest = data.get("hrrest")
            if hrrest and isinstance(hrrest, int) and 35 <= hrrest <= 110:
                return hrrest
    return default

def estimate_kcal_workout(profile: Dict[str, Any], desc: str, mins: int, hrm: Optional[int] = None) -> int:
    """Оценка калорий за тренировку"""
    if not profile_complete(profile):
        return mins * 8  # базовая оценка
    
    weight_kg = float(profile["weight_kg"])
    desc_lower = desc.lower()
    
    # Базовые MET значения для разных видов активности
    met_values = {
        "бег": 10.0, "running": 10.0, "run": 10.0,
        "ходьба": 3.5, "walking": 3.5, "walk": 3.5,
        "велосипед": 8.0, "cycling": 8.0, "bike": 8.0, "вело": 8.0,
        "плавание": 8.0, "swimming": 8.0, "swim": 8.0,
        "силовая": 6.0, "strength": 6.0, "weight": 6.0, "гантели": 6.0, "штанга": 6.0,
        "йога": 3.0, "yoga": 3.0,
        "hiit": 12.0, "интервал": 12.0, "табата": 12.0,
        "кроссфит": 10.0, "crossfit": 10.0,
        "теннис": 8.0, "tennis": 8.0,
        "футбол": 9.0, "football": 9.0, "soccer": 9.0
    }
    
    # Определяем тип активности и MET
    met = 6.0  # значение по умолчанию
    for keyword, met_value in met_values.items():
        if keyword in desc_lower:
            met = met_value
            break
    
    # Корректировка на основе пульса (если указан)
    if hrm:
        if hrm > 160:
            met *= 1.3  # высокая интенсивность
        elif hrm > 140:
            met *= 1.1  # умеренно-высокая
        elif hrm < 120:
            met *= 0.8  # низкая интенсивность
    
    # Формула: Калории = MET × вес_кг × время_часы
    hours = mins / 60.0
    kcal = met * weight_kg * hours
    
    return max(10, int(kcal))  # минимум 10 ккал

async def generate_menu_via_llm(profile: Dict[str, Any], target_kcal: int, changes: str = "") -> str:
    """Генерирует персональное меню через LLM"""
    if not client:
        return "ИИ недоступен для генерации меню."
    
    allergies = profile.get("allergies", "нет")
    conditions = profile.get("conditions", "нет")
    goal = profile.get("goal", "Поддерживать вес")
    preferences = changes or profile.get("preferences", {}).get("menu_notes", "")
    
    system_prompt = (
        "Вы профессиональный нутрициолог. Составляйте персональные меню с точными граммовками и КБЖУ. "
        "Используйте официальный деловой стиль. НЕ используйте смайлики в тексте меню. "
        "НЕ используйте символы # (решетки) для заголовков - используйте простой текст с заглавными буквами. "
        "Для каждого приема пищи указывайте конкретные продукты с граммовками и КБЖУ. "
        "Фрукты и орехи указывайте в штуках с граммовкой (например: яблоко 150г (1 шт), грецкие орехи 30г (6 шт)). "
        "В конце каждого приема пищи обязательно указывайте итог: 'Итого: ~X ккал, Б: Y г, Ж: Z г, У: W г'"
        "В самом конце дня - общий итог: 'Итого за день: ~X ккал, Б: Y г, Ж: Z г, У: W г'"
    )
    
    user_prompt = (
        f"Составьте персональное меню на {target_kcal} ккал для:\n"
        f"Пол: {profile.get('gender')}, Возраст: {profile.get('age')}, "
        f"Рост: {profile.get('height_cm')} см, Вес: {profile.get('weight_kg')} кг\n"
        f"Цель: {goal}\n"
        f"Аллергии: {allergies}\n"
        f"Заболевания: {conditions}\n"
        f"Пожелания: {preferences or 'стандартное здоровое меню'}\n\n"
        f"Распределите на 5 приемов пищи: завтрак, перекус 1, обед, перекус 2, ужин.\n"
        f"ВАЖНО: НЕ используйте символы # (решетки). Используйте обычный текст.\n"
        f"ОБЯЗАТЕЛЬНО завершите ответ: 'Итого за день: ~{target_kcal} ккал, Б: X г, Ж: Y г, У: Z г'"
    )
    
    try:
        result = await chat_llm([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ])
        
        # Убираем решетки из результата если они есть
        result = result.replace("###", "").replace("##", "").replace("#", "")
        
        # Проверяем есть ли итог за день
        if not re.search(r"Итого за день:.*?ккал", result):
            # Рассчитываем примерные БЖУ
            protein_g = int(target_kcal * 0.25 / 4)  # 25% от калорий на белки
            fat_g = int(target_kcal * 0.25 / 9)      # 25% от калорий на жиры  
            carbs_g = int(target_kcal * 0.50 / 4)    # 50% от калорий на углеводы
            
            result += f"\n\nИтого за день: ~{target_kcal} ккал, Б: {protein_g} г, Ж: {fat_g} г, У: {carbs_g} г"
        
        return sanitize_ai(result)
        
    except Exception as e:
        logger.error(f"Menu generation error: {e}")
        return f"Ошибка генерации меню: {e}"

async def generate_workout_via_llm(profile: Dict[str, Any], location: str, inventory: str, changes: str = "", days: int = 3) -> str:
    """Генерирует план тренировок через LLM"""
    if not client:
        return "ИИ недоступен для генерации планов."
    
    goal = profile.get("goal", "Поддерживать вес")
    injuries = profile.get("injuries", "нет")
    conditions = profile.get("conditions", "нет")
    preferences = changes or profile.get("preferences", {}).get("workout_notes", "")
    
    system_prompt = (
        "Вы сертифицированный персональный тренер по стандартам NASM. "
        "Составляйте безопасные и эффективные планы тренировок с учетом индивидуальных особенностей. "
        "Используйте научно обоснованные методики. НЕ используйте смайлики в тексте плана. "
        "НЕ используйте символы # (решетки) для заголовков. "
        "Указывайте конкретные упражнения, подходы, повторения и время отдыха. "
        "В конце обязательно указывайте примерный расход калорий за неделю."
    )
    
    user_prompt = (
        f"Составьте план тренировок на {days} дня в неделю для:\n"
        f"Пол: {profile.get('gender')}, Возраст: {profile.get('age')}\n"
        f"Цель: {goal}\n"
        f"Место: {location}\n"
        f"Инвентарь: {inventory}\n"
        f"Травмы/ограничения: {injuries}\n"
        f"Заболевания: {conditions}\n"
        f"Пожелания: {preferences or 'стандартная программа'}\n\n"
        f"Укажите конкретные упражнения, подходы×повторения, время отдыха.\n"
        f"ВАЖНО: НЕ используйте символы # (решетки).\n"
        f"ОБЯЗАТЕЛЬНО завершите: 'Итого за неделю: ~X ккал'"
    )
    
    try:
        result = await chat_llm([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ])
        
        # Убираем решетки
        result = result.replace("###", "").replace("##", "").replace("#", "")
        
        # Проверяем есть ли итог за неделю
        if not re.search(r"Итого за неделю:.*?ккал", result):
            # Примерная оценка калорий за неделю
            estimated_weekly_kcal = days * 400  # примерно 400 ккал за тренировку
            result += f"\n\nИтого за неделю: ~{estimated_weekly_kcal} ккал"
        
        return sanitize_ai(result)
        
    except Exception as e:
        logger.error(f"Workout generation error: {e}")
        return f"Ошибка генерации плана: {e}"

def persona_system(role: str, profile: Dict[str, Any]) -> str:
    """Системный промпт для персонализированного общения"""
    if role == "nutri":
        return (
            f"Вы профессиональный нутрициолог с опытом работы 15+ лет. "
            f"Отвечайте на русском языке, профессионально, но дружелюбно. "
            f"Учитывайте индивидуальные особенности клиента. "
            f"Клиент: {profile.get('gender', '')}, {profile.get('age', '')} лет, "
            f"цель: {profile.get('goal', '')}, аллергии: {profile.get('allergies', 'нет')}, "
            f"заболевания: {profile.get('conditions', 'нет')}. "
            f"Используйте научно обоснованные рекомендации и актуальные данные о питании."
        )
    elif role == "trainer":
        return (
            f"Вы сертифицированный персональный тренер NASM с опытом 15+ лет. "
            f"Отвечайте на русском языке, мотивирующе и профессионально. "
            f"Специализируетесь на безопасных тренировках и профилактике травм. "
            f"Клиент: {profile.get('gender', '')}, {profile.get('age', '')} лет, "
            f"цель: {profile.get('goal', '')}, травмы: {profile.get('injuries', 'нет')}, "
            f"заболевания: {profile.get('conditions', 'нет')}. "
            f"Используйте научные принципы тренировок и физиологии."
        )
    return "Вы помощник по здоровью и фитнесу."

# ========= ДОПОЛНИТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ ПАРСИНГА =========
def _jsonld(html: str) -> Optional[Dict[str, Any]]:
    """Извлекает JSON-LD данные о питательности из HTML"""
    try:
        import json
        import re
        
        # Ищем JSON-LD скрипты
        scripts = re.findall(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.DOTALL | re.IGNORECASE)
        
        for script in scripts:
            try:
                data = json.loads(script.strip())
                # Ищем данные о питательности
                if isinstance(data, dict):
                    nutrition = data.get('nutrition') or data.get('nutritionValue')
                    if nutrition:
                        return _extract_nutrition_from_jsonld(nutrition)
            except json.JSONDecodeError:
                continue
        
        return None
    except Exception:
        return None

def _extract_nutrition_from_jsonld(nutrition_data) -> Optional[Dict[str, Any]]:
    """Извлекает питательные данные из JSON-LD структуры"""
    try:
        result = {}
        
        if isinstance(nutrition_data, dict):
            # Прямые значения
            if 'calories' in nutrition_data:
                result['kcal_serv'] = float(nutrition_data['calories'])
            if 'protein' in nutrition_data:
                result['protein_serv'] = float(nutrition_data['protein'])
            if 'fat' in nutrition_data:
                result['fat_serv'] = float(nutrition_data['fat'])
            if 'carbohydrate' in nutrition_data or 'carbs' in nutrition_data:
                result['carb_serv'] = float(nutrition_data.get('carbohydrate') or nutrition_data.get('carbs'))
        
        # Предполагаем порцию 100г если не указано иначе
        result['serving_g'] = 100
        
        return result if result else None
    except Exception:
        return None

def _regex_nutrition(html: str) -> Optional[Dict[str, Any]]:
    """Извлекает питательные данные через регулярные выражения"""
    try:
        text = html.lower()
        
        # Расширенные паттерны для поиска питательных данных
        kcal_patterns = [
            r'(\d{1,4})\s*(?:ккал|kcal|calories?)\s*(?:/100\s*г|на\s*100\s*г|per\s*100\s*g)?',
            r'калорийность[:\s]*(\d{1,4})\s*(?:ккал)?',
            r'энергетическая\s+ценность[:\s]*(\d{1,4})\s*(?:ккал)?',
            r'energy[:\s]*(\d{1,4})\s*(?:kcal)?',
            r'пищевая\s+ценность[:\s\-]*(\d{1,4})\s*(?:ккал)?',
            r'(\d{1,4})\s*кал(?:орий)?',
            r'энергия[:\s]*(\d{1,4})'
        ]
        
        protein_patterns = [
            r'(?:белк[иао]|protein|протеин)[:\s\-]*(\d{1,3}(?:[.,]\d{1,2})?)\s*г?',
            r'(\d{1,3}(?:[.,]\d{1,2})?)\s*г?\s*(?:белк|protein)',
            r'б[:\s]*(\d{1,3}(?:[.,]\d{1,2})?)\s*г',
            r'белок[:\s]*(\d{1,3}(?:[.,]\d{1,2})?)'
        ]
        
        fat_patterns = [
            r'(?:жир[ыао]|fat|липид)[:\s\-]*(\d{1,3}(?:[.,]\d{1,2})?)\s*г?',
            r'(\d{1,3}(?:[.,]\d{1,2})?)\s*г?\s*(?:жир|fat)',
            r'ж[:\s]*(\d{1,3}(?:[.,]\d{1,2})?)\s*г',
            r'липиды[:\s]*(\d{1,3}(?:[.,]\d{1,2})?)'
        ]
        
        carb_patterns = [
            r'(?:углевод[ыао]|carb(?:ohydrate)?s?|сахар)[:\s\-]*(\d{1,3}(?:[.,]\d{1,2})?)\s*г?',
            r'(\d{1,3}(?:[.,]\d{1,2})?)\s*г?\s*(?:углевод|carb)',
            r'у[:\s]*(\d{1,3}(?:[.,]\d{1,2})?)\s*г',
            r'углеводы[:\s]*(\d{1,3}(?:[.,]\d{1,2})?)'
        ]
        
        # Ищем калории
        kcal = None
        for pattern in kcal_patterns:
            matches = re.findall(pattern, text)
            for match in matches:
                try:
                    potential_kcal = float(match)
                    if 10 <= potential_kcal <= 900:  # Разумные пределы
                        kcal = potential_kcal
                        break
                except (ValueError, TypeError):
                    continue
            if kcal:
                break
        
        if not kcal:
            logger.info("No calories found in regex extraction")
            return None
        
        # Функция для безопасного извлечения числа
        def extract_macro(patterns):
            for pattern in patterns:
                matches = re.findall(pattern, text)
                for match in matches:
                    try:
                        val = float(str(match).replace(',', '.'))
                        if 0 <= val <= 100:
                            return val
                    except (ValueError, TypeError):
                        continue
            return None
        
        protein = extract_macro(protein_patterns)
        fat = extract_macro(fat_patterns)  
        carbs = extract_macro(carb_patterns)
        
        result = {
            'kcal_serv': kcal,
            'serving_g': 100
        }
        
        if protein is not None:
            result['protein_serv'] = protein
        if fat is not None:
            result['fat_serv'] = fat
        if carbs is not None:
            result['carb_serv'] = carbs
        
        logger.info(f"Regex extraction result: kcal={kcal}, protein={protein}, fat={fat}, carbs={carbs}")
        return result
        
    except Exception as e:
        logger.warning(f"Regex nutrition parsing error: {e}")
        return None

async def _vision_ocr_text(image_url: str) -> Optional[str]:
    """Извлекает текст из изображения через Google Vision API"""
    if not VISION_KEY:
        return None
    
    try:
        # Загружаем изображение
        def _fetch_image():
            response = requests.get(image_url, timeout=15)
            return response if response.status_code == 200 else None
        
        response = await asyncio.to_thread(_fetch_image)
        if not response:
            return None
        
        import base64
        image_content = base64.b64encode(response.content).decode('utf-8')
        
        # Запрос к Vision API
        vision_url = f"https://vision.googleapis.com/v1/images:annotate?key={VISION_KEY}"
        
        payload = {
            "requests": [{
                "image": {"content": image_content},
                "features": [{"type": "TEXT_DETECTION"}]
            }]
        }
        
        def _make_vision_request():
            return requests.post(vision_url, json=payload, timeout=20)
        
        ocr_response = await asyncio.to_thread(_make_vision_request)
        if ocr_response.status_code == 200:
            result = ocr_response.json()
            annotations = result.get('responses', [{}])[0].get('textAnnotations', [])
            if annotations:
                return annotations[0].get('description', '')
        
        return None
    except Exception as e:
        logger.warning(f"Vision OCR failed: {e}")
        return None

def _parse_ocr(text: str) -> Optional[Dict[str, Any]]:
    """Парсит текст OCR для извлечения питательных данных"""
    try:
        text_lower = text.lower()
        
        # Паттерны для OCR текста (могут быть менее точными)
        kcal_match = re.search(r'(\d{1,4})\s*(?:ккал|kcal|cal|calories?)', text_lower)
        protein_match = re.search(r'(?:белк|protein|протеин)[:\s]*(\d{1,3}(?:[.,]\d{1,2})?)', text_lower)
        fat_match = re.search(r'(?:жир|fat|липид)[:\s]*(\d{1,3}(?:[.,]\d{1,2})?)', text_lower)
        carb_match = re.search(r'(?:углевод|carb|сахар)[:\s]*(\d{1,3}(?:[.,]\d{1,2})?)', text_lower)
        
        if kcal_match:
            result = {
                'kcal_serv': float(kcal_match.group(1)),
                'serving_g': 100
            }
            
            if protein_match:
                result['protein_serv'] = float(protein_match.group(1).replace(',', '.'))
            if fat_match:
                result['fat_serv'] = float(fat_match.group(1).replace(',', '.'))
            if carb_match:
                result['carb_serv'] = float(carb_match.group(1).replace(',', '.'))
            
            return result
        
        return None
    except Exception:
        return None

def _extract_from_ocr_text(text: str) -> Optional[Dict[str, Any]]:
    """Alias для _parse_ocr для совместимости"""
    return _parse_ocr(text)

# ========= АНКЕТА =========
async def handle_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE, st: Dict[str, Any]) -> bool:
    u, text, step = update.effective_user, (update.message.text or "").strip(), st.get("awaiting")
    if not step or not step.startswith("onb_"):
        return False
    if step == "onb_gender":
        if text not in ("Женский", "Мужской"):
            await update.message.reply_text("Выберите «Женский» или «Мужской».", reply_markup=ReplyKeyboardMarkup(GENDER_KB, resize_keyboard=True))
            return True
        st["profile"]["gender"] = text
        st["awaiting"] = "onb_age"
        await update.message.reply_text("Возраст (лет): 10–100", reply_markup=ReplyKeyboardRemove())
    elif step == "onb_age":
        try:
            age = int(text)
            assert 10 <= age <= 100
            st["profile"]["age"] = age
            st["awaiting"] = "onb_height"
            await update.message.reply_text("Рост (см): 100–250")
        except Exception:
            await update.message.reply_text("Введите число 10–100.")
    elif step == "onb_height":
        try:
            h = int(text)
            assert 100 <= h <= 250
            st["profile"]["height_cm"] = h
            st["awaiting"] = "onb_weight"
            await update.message.reply_text("Вес (кг): 30–300")
        except Exception:
            await update.message.reply_text("Введите число 100–250.")
    elif step == "onb_weight":
        try:
            w = float(text.replace(",", "."))
            assert 30 <= w <= 300
            st["profile"]["weight_kg"] = round(w, 1)
            st["awaiting"] = "onb_activity"
            await update.message.reply_text("Активность:", reply_markup=ReplyKeyboardMarkup(ACTIVITY_KB, resize_keyboard=True))
        except Exception:
            await update.message.reply_text("Введите число 30–300 (например 72.5).")
    elif step == "onb_activity":
        if text not in ("Низкая", "Умеренная", "Высокая"):
            await update.message.reply_text("Выберите: Низкая | Умеренная | Высокая", reply_markup=ReplyKeyboardMarkup(ACTIVITY_KB, resize_keyboard=True))
            return True
        st["profile"]["activity"] = text
        st["awaiting"] = "onb_goal"
        await update.message.reply_text("Цель:", reply_markup=ReplyKeyboardMarkup(GOAL_KB, resize_keyboard=True))
    elif step == "onb_goal":
        if text not in ("Набрать массу", "Похудеть", "Поддерживать вес"):
            await update.message.reply_text("Выберите: Набрать массу | Похудеть | Поддерживать вес", reply_markup=ReplyKeyboardMarkup(GOAL_KB, resize_keyboard=True))
            return True
        st["profile"]["goal"] = text
        st["awaiting"] = "onb_allergies"
        await update.message.reply_text("Аллергии на продукты? Перечислите или «нет».", reply_markup=ReplyKeyboardRemove())
    elif step == "onb_allergies":
        st["profile"]["allergies"] = text
        st["awaiting"] = "onb_conditions"
        await update.message.reply_text("Хронические заболевания? (или «нет»).")
    elif step == "onb_conditions":
        st["profile"]["conditions"] = text
        st["awaiting"] = "onb_injuries"
        await update.message.reply_text("Травмы/ограничения? (или «нет»).")
    elif step == "onb_injuries":
        st["profile"]["injuries"] = text
        st["awaiting"] = None
        add_points(st, 25)

        # Формируем персональное резюме на основе заполненного профиля
        profile = st["profile"]
        k = calc_kbju_weight_loss(profile)

        summary_lines = [
            "✅ АНКЕТА ЗАВЕРШЕНА! +25 баллов!",
            "",
            "📋 ВАШЕ ПЕРСОНАЛЬНОЕ РЕЗЮМЕ:",
            f"👤 {profile['gender']}, {profile['age']} лет",
            f"📏 Рост: {profile['height_cm']} см, Вес: {profile['weight_kg']} кг",
            f"📊 ИМТ: {k['bmi']} ({k['bmi_category']})",
            f"🎯 Цель: {profile['goal']}",
            f"🏃 Активность: {profile['activity']}",
            "",
            "🔥 ВАШИ НОРМЫ ПИТАНИЯ:",
            f"• Калории: {k['target_kcal']} ккал/день",
            f"• Белки: {k['protein_g']} г/день",
            f"• Жиры: {k['fat_g']} г/день",
            f"• Углеводы: {k['carbs_g']} г/день",
            "",
            "💡 ЧТО ДЕЛАТЬ ДАЛЬШЕ:"
        ]

        # Добавляем рекомендации в зависимости от цели
        if profile['goal'] == "Похудеть":
            summary_lines.extend([
                "1. 🍽️ Питайтесь в рамках рассчитанной нормы",
                "2. 🥩 Увеличьте долю белка для сохранения мышц",
                "3. 💪 Добавьте силовые тренировки 2-3 раза в неделю",
                "4. 📱 Ведите дневник питания для контроля",
                "5. ⚖️ Взвешивайтесь 1 раз в неделю в одно время"
            ])
        elif profile['goal'] == "Набрать массу":
            summary_lines.extend([
                "1. 🍽️ Питайтесь с профицитом калорий",
                "2. 🥩 Употребляйте много белка (каждые 3-4 часа)",
                "3. 💪 Силовые тренировки 3-4 раза в неделю обязательны",
                "4. 💤 Спите не менее 7-8 часов для восстановления",
                "5. 📊 Отслеживайте прогресс и корректируйте питание"
            ])
        else:  # Поддерживать вес
            summary_lines.extend([
                "1. 🍽️ Поддерживайте сбалансированное питание",
                "2. 🏃 Сочетайте кардио и силовые тренировки",
                "3. 📊 Контролируйте вес и состав тела",
                "4. 🥗 Включайте разнообразные продукты в рацион",
                "5. 💧 Пейте достаточно воды (30-35 мл/кг веса)"
            ])

        if profile['allergies'] and profile['allergies'].lower() != "нет":
            summary_lines.append(f"⚠️ Учитывайте аллергии: {profile['allergies']}")

        if profile['conditions'] and profile['conditions'].lower() != "нет":
            summary_lines.append(f"⚠️ Учитывайте заболевания: {profile['conditions']}")

        if profile['injuries'] and profile['injuries'].lower() != "нет":
            summary_lines.append(f"⚠️ Учитывайте ограничения: {profile['injuries']}")

        summary_lines.extend([
            "",
            "🚀 Используйте разделы бота:",
            "• 🥗 Нутрициолог - меню и КБЖУ",
            "• 🏋️ Тренер - планы тренировок",
            "• 📒 Дневники - отслеживание прогресса"
        ])

        # Разбиваем на части если сообщение длинное
        full_summary = "\n".join(summary_lines)
        if len(full_summary) > 4000:
            # Первая часть - основные данные
            first_part = "\n".join(summary_lines[:15])
            await update.message.reply_text(first_part)

            # Вторая часть - рекомендации
            second_part = "\n".join(summary_lines[15:])
            await update.message.reply_text(second_part, reply_markup=role_keyboard(None))
        else:
            await update.message.reply_text(full_summary, reply_markup=role_keyboard(None))

    save_state(u.id, st)
    return True

# ========= KEEP‑ALIVE HTTP СЕРВЕР =========
def start_keepalive_server():
    # Используем уже импортированный keep_alive модуль
    try:
        import keep_alive
        keep_alive.start()
        logger.info("Keep‑alive server started")
    except Exception as e:
        logger.warning(f"Keep‑alive server не запущен: {e}")

# ========= ЗАПУСК =========
def main():
    with single_instance_lock():
        if not BOT_TOKEN:
            print("Ошибка: не задан TELEGRAM_BOT_TOKEN")
            return
        if not TELEGRAM_PAYMENT_PROVIDER_TOKEN:
            print("Внимание: не задан TELEGRAM_PAYMENT_PROVIDER_TOKEN. Платёжные функции будут недоступны.")

        log_egress_ip_once()

        # запустим keep‑alive сервер в фоне (отдельный поток)
        try:
            import threading
            threading.Thread(target=start_keepalive_server, daemon=True).start()
        except Exception as e:
            logger.warning(f"Не удалось запустить keep-alive: {e}")

        app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()

        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("help", help_cmd))
        app.add_handler(CommandHandler("whoami", whoami_cmd))
        app.add_handler(CommandHandler("health", health_cmd))
        app.add_handler(CommandHandler("version", version_cmd))
        app.add_handler(CommandHandler("shop", shop_command))
        app.add_handler(CommandHandler("add_admin", add_admin_cmd))
        app.add_handler(CommandHandler("remove_admin", remove_admin_cmd))
        app.add_handler(CommandHandler("list_admins", list_admins_cmd))


        app.add_handler(
            CallbackQueryHandler(
                recipes_callbacks, pattern=r"^(rroot|rcat:|rpage:|rshow:|radd:|rback|shop_open)"
            )
        )
        app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^(save_menu|save_workout|buy):"))

        # платежи
        app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
        app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))

        # основной обработчик
        app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle_text_or_photo))
        app.add_error_handler(error_handler)

        print(f"{PROJECT_NAME} запущен. {VERSION}")
        app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()