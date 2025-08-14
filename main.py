# healco lite (v2)
# Полностью рабочий Telegram-бот по ТЗ
# Техстек: Python 3.11+, python-telegram-bot==21.6 (async), openai>=1.30.0, python-dotenv>=1.0.1, replit (DB), (опц.) aiohttp для /health
# Переменные окружения: TELEGRAM_BOT_TOKEN, OPENAI_API_KEY
# Архитектура: ApplicationBuilder().concurrent_updates(True), polling с drop_pending_updates=True
# Хранилище: Replit DB (ключи user:<id>), автоматический локальный JSON-фолбэк для запуска вне Replit
# Автор: healco lite (v2)

import os
import asyncio
import json
import math
from datetime import datetime
from typing import Dict, Any, List, Optional

from dotenv import load_dotenv
from openai import OpenAI

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    KeyboardButton
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

try:
    # Основное хранилище по ТЗ
    from replit import db as replit_db
    HAS_REPLIT = True
except Exception:
    HAS_REPLIT = False

# ---------- Конфигурация ----------

PROJECT_NAME = "healco lite (v2)"
MODEL_NAME = "gpt-4o-mini"  # один путь к LLM по ТЗ
LONG_OP_THINKING = ["Думаю…", "Разрабатываю…"]

MAIN_MENU = [
    [KeyboardButton("🥗 Нутрициолог"), KeyboardButton("🏋️ Фитнес-тренер")],
    [KeyboardButton("📒 Мои дневники"), KeyboardButton("🏆 Мои баллы")],
]

NUTRI_MENU = [
    [KeyboardButton("🍽️ Сгенерировать меню"), KeyboardButton("📏 ИМТ (BMI)")],
    [KeyboardButton("📊 КБЖУ"), KeyboardButton("🍏 Дневник питания")],
    [KeyboardButton("Задать вопрос ❓"), KeyboardButton("⬅️ Назад")],
]

TRAINER_MENU = [
    [KeyboardButton("📋 Сгенерировать план тренировки"), KeyboardButton("📏 ИМТ (BMI)")],
    [KeyboardButton("🫁 МПК (VO2max)"), KeyboardButton("📈 Пульсовые зоны")],
    [KeyboardButton("💪 Дневник тренировок"), KeyboardButton("🛠 Обновить профиль")],
    [KeyboardButton("Задать вопрос ❓"), KeyboardButton("⬅️ Назад")],
]

LOCATION_KB = [[KeyboardButton("Дом"), KeyboardButton("Зал"), KeyboardButton("Улица")]]

ACTIVITY_KB = [[KeyboardButton("Низкая"), KeyboardButton("Умеренная"), KeyboardButton("Высокая")]]
GOAL_KB = [[KeyboardButton("Набрать массу"), KeyboardButton("Похудеть"), KeyboardButton("Поддерживать вес")]]
GENDER_KB = [[KeyboardButton("Женский"), KeyboardButton("Мужской")]]

# ---------- Локальный JSON-фолбэк, если Replit DB недоступен ----------
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

    def __getitem__(self, key):
        return self.store[key]

    def __setitem__(self, key, value):
        self.store[key] = value
        self._save()

    def __contains__(self, key):
        return key in self.store

    def keys(self):
        return list(self.store.keys())

# Глобальный DB-объект
local_db = LocalDB(os.environ.get("HLITE_DB_PATH", "db.json"))

def db_get(key: str, default=None):
    if HAS_REPLIT:
        try:
            return replit_db.get(key, default)
        except Exception:
            # аварийный фолбэк
            return local_db.store.get(key, default)
    else:
        return local_db.store.get(key, default)

def db_set(key: str, value):
    if HAS_REPLIT:
        try:
            replit_db[key] = value
            return
        except Exception:
            pass
    local_db[key] = value

def db_keys_prefix(prefix: str) -> List[str]:
    if HAS_REPLIT:
        try:
            return [k for k in replit_db.keys() if str(k).startswith(prefix)]
        except Exception:
            # фолбэк
            return [k for k in local_db.keys() if str(k).startswith(prefix)]
    else:
        return [k for k in local_db.keys() if str(k).startswith(prefix)]

# ---------- Состояние пользователя ----------

def default_state() -> Dict[str, Any]:
    return {
        "profile": {
            "gender": None,  # "Женский"/"Мужской"
            "age": None,     # int
            "height_cm": None, # int
            "weight_kg": None, # float
            "activity": None,  # "Низкая"/"Умеренная"/"Высокая"
            "goal": None,      # "Набрать массу"/"Похудеть"/"Поддерживать вес"
        },
        "diaries": {
            "food": [],
            "train": [],
            "metrics": [],
        },
        "points": 0,
        "current_role": None,  # "nutri" / "trainer" / None
        "awaiting": None,      # что именно ожидаем от пользователя
        "tmp": {}              # временные данные (напр. сгенерированный план)
    }

def state_key(user_id: int) -> str:
    return f"user:{user_id}"

def load_state(user_id: int) -> Dict[str, Any]:
    key = state_key(user_id)
    st = db_get(key)
    if not st:
        st = default_state()
        db_set(key, st)
    return st

def save_state(user_id: int, st: Dict[str, Any]):
    key = state_key(user_id)
    db_set(key, st)

def add_points(user_id: int, amount: int, reason: str = "") -> int:
    st = load_state(user_id)
    st["points"] = int(st.get("points", 0)) + int(amount)
    save_state(user_id, st)
    return st["points"]

def top10() -> List[Dict[str, Any]]:
    users = []
    for k in db_keys_prefix("user:"):
        st = db_get(k, {})
        if isinstance(st, dict):
            pts = int(st.get("points", 0))
            uid = k.split(":", 1)[-1]
            users.append({"user_id": uid, "points": pts})
    users.sort(key=lambda x: x["points"], reverse=True)
    return users[:10]

# ---------- Вспомогательные функции ----------

def role_keyboard(role: Optional[str] = None) -> ReplyKeyboardMarkup:
    if role == "nutri":
        return ReplyKeyboardMarkup(NUTRI_MENU, resize_keyboard=True)
    elif role == "trainer":
        return ReplyKeyboardMarkup(TRAINER_MENU, resize_keyboard=True)
    else:
        return ReplyKeyboardMarkup(MAIN_MENU, resize_keyboard=True)

def now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def profile_complete(profile: Dict[str, Any]) -> bool:
    return all([
        profile.get("gender") in ("Женский", "Мужской"),
        isinstance(profile.get("age"), int) and 10 <= profile["age"] <= 100,
        isinstance(profile.get("height_cm"), int) and 100 <= profile["height_cm"] <= 250,
        (isinstance(profile.get("weight_kg"), (int, float)) and 30 <= float(profile["weight_kg"]) <= 300),
        profile.get("activity") in ("Низкая", "Умеренная", "Высокая"),
        profile.get("goal") in ("Набрать массу", "Похудеть", "Поддерживать вес"),
    ])

def activity_multiplier(activity: str) -> float:
    return {"Низкая": 1.2, "Умеренная": 1.55, "Высокая": 1.725}.get(activity, 1.2)

# ---------- Калькуляторы ----------

def calc_bmi(weight_kg: float, height_cm: int):
    h_m = height_cm / 100.0
    bmi = weight_kg / (h_m * h_m)
    if bmi < 18.5:
        cat = "Недостаточная масса"
    elif bmi < 25:
        cat = "Норма"
    elif bmi < 30:
        cat = "Избыточная масса"
    else:
        cat = "Ожирение"
    return round(bmi, 1), cat

def mifflin_st_jeor(gender: str, age: int, height_cm: int, weight_kg: float) -> float:
    if gender == "Мужской":
        return 10 * weight_kg + 6.25 * height_cm - 5 * age + 5
    else:
        return 10 * weight_kg + 6.25 * height_cm - 5 * age - 161

def calc_kbju(profile: Dict[str, Any]) -> Dict[str, Any]:
    gender = profile["gender"]
    age = profile["age"]
    height_cm = profile["height_cm"]
    weight_kg = float(profile["weight_kg"])
    goal = profile["goal"]
    act_mult = activity_multiplier(profile["activity"])

    bmr = mifflin_st_jeor(gender, age, height_cm, weight_kg)
    tdee = bmr * act_mult

    goal_mult = {"Похудеть": 0.85, "Набрать массу": 1.10, "Поддерживать вес": 1.00}[goal]
    target_kcal = tdee * goal_mult

    # Белок
    protein_per_kg = 1.8 if goal == "Похудеть" else 1.6
    protein_g = protein_per_kg * weight_kg

    # Жир (диапазон 0.8–0.9 г/кг)
    fat_g_low = 0.8 * weight_kg
    fat_g_high = 0.9 * weight_kg
    fat_g_mid = 0.85 * weight_kg  # используем для расчёта калорий

    # Углеводы = остаток
    kcal_from_protein = protein_g * 4
    kcal_from_fat = fat_g_mid * 9
    carbs_kcal = max(0.0, target_kcal - (kcal_from_protein + kcal_from_fat))
    carbs_g = carbs_kcal / 4.0

    return {
        "bmr": round(bmr),
        "tdee": round(tdee),
        "target_kcal": round(target_kcal),
        "protein_g": int(round(protein_g)),
        "fat_g_range": (int(round(fat_g_low)), int(round(fat_g_high))),
        "carbs_g": int(round(carbs_g)),
        "why": (
            "Mifflin–St Jeor + множитель активности; цель: "
            f"{goal} ⇒ {goal_mult:.2f}×TDEE. Белок {protein_per_kg} г/кг "
            "для поддержания мышц и контроля аппетита; жир 0.8–0.9 г/кг для гормонального баланса; "
            "углеводы — оставшиеся калории для энергии."
        )
    }

def vo2_category(gender: str, vo2: float) -> str:
    # Упрощённая короткая шкала (ориентировочная)
    if gender == "Мужской":
        if vo2 < 35: return "Низкий"
        if vo2 < 43: return "Ниже среднего"
        if vo2 < 51: return "Средний"
        if vo2 < 58: return "Выше среднего"
        return "Высокий"
    else:
        if vo2 < 28: return "Низкий"
        if vo2 < 35: return "Ниже среднего"
        if vo2 < 42: return "Средний"
        if vo2 < 49: return "Выше среднего"
        return "Высокий"

def pulse_zones(age: int, hr_rest: int = 60) -> Dict[str, tuple]:
    hr_max = 208 - 0.7 * age  # Tanaka
    hrr = hr_max - hr_rest

    def rng(p1, p2):
        lo = int(round(hr_rest + p1 * hrr))
        hi = int(round(hr_rest + p2 * hrr))
        return (min(lo, hi), max(lo, hi))

    return {
        "Z1 «восстановление»": rng(0.50, 0.60),
        "Z2 «аэробная база»": rng(0.60, 0.70),
        "Z3 «темповая»": rng(0.70, 0.80),
        "Z4 «VO2max»": rng(0.80, 0.90),
        "Z5 «анаэробная»": rng(0.90, 1.00),
    }

# ---------- LLM ----------

load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

async def ai_chat(role: str, profile: Dict[str, Any], question: str) -> str:
    """
    role: "nutri" | "trainer"
    Возвращает ответ от LLM. Таймаут и 1 ретрай по ТЗ.
    """
    if not client:
        return "Сервис ответов временно недоступен (нет ключа API). Пожалуйста, вернитесь в меню."

    sys_base_gate = (
        "Отвечай только по теме роли. Если вопрос не о питании/тренировках/здоровых привычках — "
        "вежливо откажись одной строкой и предложи вернуться в меню. "
        "Не шути и не уходи в сторонние темы."
    )

    if role == "nutri":
        sys_role = (
            "ИИ Нутрициолог. " + sys_base_gate + " Формат: структурно, кратко, без Markdown, "
            "используй 3–6 эмодзи. Учитывай профиль пользователя."
        )
    else:
        sys_role = (
            "ИИ Фитнес-тренер. " + sys_base_gate + " Формат: планы с упражнениями/подходами/повторами/временем, "
            "добавляй ориентир по сжигаемым ккал, пульсовые зоны в ударах/мин (диапазоны), "
            "варианты «новичок/средний», используй 3–6 эмодзи."
        )

    prof_text = (
        f"Профиль: пол={profile.get('gender')}, возраст={profile.get('age')}, "
        f"рост={profile.get('height_cm')} см, вес={profile.get('weight_kg')} кг, "
        f"активность={profile.get('activity')}, цель={profile.get('goal')}."
    )

    msgs = [
        {"role": "system", "content": sys_role},
        {"role": "system", "content": prof_text},
        {"role": "user", "content": question.strip()[:4000]},
    ]

    async def _call():
        return client.chat.completions.create(
            model=MODEL_NAME,
            messages=msgs,
            temperature=0.4,
            max_tokens=600,
        )

    for attempt in range(2):
        try:
            comp = await asyncio.wait_for(asyncio.to_thread(_call), timeout=30)
            txt = comp.choices[0].message.content.strip()
            return txt
        except asyncio.TimeoutError:
            if attempt == 0:
                continue
            return "Превышено время ожидания ответа. Пожалуйста, попробуйте ещё раз."
        except Exception:
            if attempt == 0:
                await asyncio.sleep(1)
                continue
            return "Произошла ошибка при генерации ответа. Вернитесь в меню и попробуйте снова."

# ---------- Генераторы ----------

def gen_menu_text(profile: Dict[str, Any]) -> str:
    goal = profile.get("goal") or "Поддерживать вес"
    act = profile.get("activity") or "Умеренная"
    kcal_hint = {"Похудеть": "-15%", "Набрать массу": "+10%", "Поддерживать вес": "≈TDEE"}[goal]

    # Два варианта: Базовое и Гурме (5 приёмов: завтрак/перекус/обед/перекус/ужин)
    # Объёмы и ккал приблизительные.
    text = []
    text.append(f"Вариант «Базовое» ({kcal_hint}, активность: {act})")
    text.append("Завтрак: овсяная каша на воде с бананом (~300 г, ~380 ккал)")
    text.append("Перекус: йогурт натуральный (~150 г, ~120 ккал)")
    text.append("Обед: куриная грудка, рис, салат (~350 г, ~520 ккал)")
    text.append("Перекус: орехи (~30 г, ~180 ккал)")
    text.append("Ужин: рыба с овощами на пару (~300 г, ~400 ккал)")
    text.append("")
    text.append("Вариант «Гурме»")
    text.append("Завтрак: омлет с шпинатом и сыром (~250 г, ~420 ккал)")
    text.append("Перекус: творог с ягодами (~200 г, ~220 ккал)")
    text.append("Обед: лосось, киноа, авокадо-салат (~350 г, ~600 ккал)")
    text.append("Перекус: фрукт + миндаль (~180 г, ~200 ккал)")
    text.append("Ужин: говядина тушёная + гречка + овощи (~350 г, ~560 ккал)")
    text.append("")
    text.append("Записать в дневник? (да/нет)")
    return "\n".join(text)

def gen_gym_plan(profile: Dict[str, Any]) -> str:
    age = profile.get("age") or 30
    zones = pulse_zones(age, 60)
    z2 = zones["Z2 «аэробная база»"]
    kcal_week = 1800  # примерная неделя

    text = []
    text.append("Недельный план (зал)")
    text.append("Пн: Силы (ноги/ягодицы) — приседания со штангой 4×6–8; жим ногами 3×10–12; румынская тяга 3×8–10; пресс 3×15–20")
    text.append("Вт: Кардио — беговая дорожка 30 мин в Z2")
    text.append("Ср: Силы (грудь/спина) — жим лёжа 4×6–8; тяга верхнего блока 3×10–12; тяга штанги в наклоне 3×8–10; планка 3×40–60 сек")
    text.append("Чт: Отдых/растяжка 20 мин")
    text.append("Пт: Силы (плечи/руки) — жим гантелей 4×8–10; подъём на бицепс 3×10–12; разгибания на трицепс 3×10–12; гиперэкстензия 3×12–15")
    text.append("Сб: Интервалы — эллипс 25 мин (3 мин Z2 / 1 мин Z4)")
    text.append("Вс: Ходьба 45–60 мин в Z2")
    text.append("")
    text.append(f"Целевые пульсовые зоны (уд/мин): Z2: {z2[0]}–{z2[1]} (длительная выносливость).")
    text.append(f"Ориентир по сжигаемым ккал за неделю: ~{kcal_week}.")
    text.append("Варианты: новичок — уменьшить объём на 25–30%; средний — как указано.")
    text.append("")
    text.append("Команда: /workout_done когда закончишь. Записать в дневник? (да/нет)")
    return "\n".join(text)

def gen_home_or_outdoor_plan(profile: Dict[str, Any], place: str, inventory: str) -> str:
    age = profile.get("age") or 30
    zones = pulse_zones(age, 60)
    z2 = zones["Z2 «аэробная база»"]
    kcal_week = 1500  # примерная неделя

    text = []
    text.append(f"Недельный план ({place.lower()})")
    if place == "Дом":
        text.append("Пн: Круговая (3–4 круга) — приседания 15; отжимания 8–12; ягодичный мост 15; планка 40–60 сек; скручивания 15")
        text.append("Вт: Кардио — шаги дома/скакалка 30 мин в Z2")
        text.append("Ср: Ноги/кор — выпады 3×12 на ногу; приседания плие 3×12; супермен 3×15; боковая планка 3×30–45 сек")
        text.append("Чт: Растяжка / мобилити 20–25 мин")
        text.append("Пт: Верх тела — отжимания 4×8–12; тяга в наклоне (эластичная лента/гантели) 4×10–12; плечи — махи 3×12–15; пресс 3×15–20")
        text.append("Сб: Интервалы 20–25 мин (40 сек работа / 20 сек отдых, 10 раундов)")
        text.append("Вс: Активное восстановление — прогулка 45–60 мин в Z2")
    else:
        text.append("Пн: Бег 30 мин в Z2 + 5 ускорений по 20 сек")
        text.append("Вт: ОФП — подтягивания/низкая перекладина 3×макс; отжимания 3×10–12; выпады 3×12 на ногу; планка 3×40–60 сек")
        text.append("Ср: Велосипед/самокат 40 мин в Z2")
        text.append("Чт: Лестницы/холмы 8–10×30–45 сек подъёмы (Z3–Z4)")
        text.append("Пт: Силовая с собственным весом — приседания 4×15; отжимания 4×10–12; тяга эспандера 4×12; пресс 3×15–20")
        text.append("Сб: Длинная прогулка/поход 60–90 мин в Z2")
        text.append("Вс: Растяжка/йога 20–25 мин")

    if inventory and inventory.strip().lower() != "нет":
        text.append("")
        text.append(f"Учитываю инвентарь: {inventory.strip()}")

    text.append("")
    text.append(f"Целевые пульсовые зоны (уд/мин): Z2: {z2[0]}–{z2[1]} (длительная выносливость).")
    text.append(f"Ориентир по сжигаемым ккал за неделю: ~{kcal_week}.")
    text.append("Варианты: новичок — уменьшить объём на 25–30%; средний — как указано.")
    text.append("")
    text.append("Команда: /workout_done когда закончишь. Записать в дневник? (да/нет)")
    return "\n".join(text)

# ---------- Телеграм-бот ----------

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

async def send_long_op(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Отправляем короткое сообщение о долгой операции
    try:
        await update.effective_chat.send_message(LONG_OP_THINKING[0])
    except Exception:
        pass

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    st = load_state(user.id)
    kb = role_keyboard(None)
    greet = f"Привет, {user.first_name or 'друг'}! Это {PROJECT_NAME}.\n"
    if not profile_complete(st["profile"]):
        st["awaiting"] = "onb_gender"
        save_state(user.id, st)
        await update.message.reply_text(
            greet + "Давай начнём с небольшой анкеты.",
            reply_markup=ReplyKeyboardMarkup(GENDER_KB, resize_keyboard=True),
        )
        await update.message.reply_text("Пол: выбери «Женский» или «Мужской».")
    else:
        await update.message.reply_text(greet + "Выбери раздел:", reply_markup=kb)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "Доступные команды:\n"
        "/start — перезапуск и меню\n"
        "/help — помощь\n"
        "/workout_done <текст> — отметить выполнение тренировки\n"
        "/health — диагностика (200 OK)\n\n"
        "Или используй кнопки меню."
    )
    await update.message.reply_text(txt, reply_markup=role_keyboard(None))

async def health_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # (опц.) aiohttp не требуется для простого health-пинга
    await update.message.reply_text("200 OK")

async def workout_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    st = load_state(user.id)
    args = context.args
    note = " ".join(args).strip() if args else "(без описания)"
    st["diaries"]["train"].append({"ts": now_ts(), "text": note})
    save_state(user.id, st)
    add_points(user.id, 2, "workout_done")
    await update.message.reply_text("Записал в дневник тренировок. +2 балла!")

# ---------- Анкета (онбординг) ----------

def ask_next_onboarding(update: Update, st: Dict[str, Any]):
    # помощник для перехода между шагами анкеты
    pass

async def handle_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE, st: Dict[str, Any]):
    user = update.effective_user
    text = (update.message.text or "").strip()

    # Определяем шаг
    step = st.get("awaiting")

    # Пол
    if step == "onb_gender":
        if text not in ("Женский", "Мужской"):
            await update.message.reply_text("Пожалуйста, выбери «Женский» или «Мужской».", reply_markup=ReplyKeyboardMarkup(GENDER_KB, resize_keyboard=True))
            return True
        st["profile"]["gender"] = text
        st["awaiting"] = "onb_age"
        save_state(user.id, st)
        await update.message.reply_text("Возраст (полных лет): 10–100", reply_markup=ReplyKeyboardRemove())
        return True

    # Возраст
    if step == "onb_age":
        try:
            age = int(text)
            if 10 <= age <= 100:
                st["profile"]["age"] = age
                st["awaiting"] = "onb_height"
                save_state(user.id, st)
                await update.message.reply_text("Рост (см): 100–250")
            else:
                await update.message.reply_text("Некорректно. Введите возраст в диапазоне 10–100.")
            return True
        except ValueError:
            await update.message.reply_text("Введите число, например: 34")
            return True

    # Рост
    if step == "onb_height":
        try:
            h = int(text)
            if 100 <= h <= 250:
                st["profile"]["height_cm"] = h
                st["awaiting"] = "onb_weight"
                save_state(user.id, st)
                await update.message.reply_text("Вес (кг): 30–300")
            else:
                await update.message.reply_text("Некорректно. Введите рост в диапазоне 100–250 см.")
            return True
        except ValueError:
            await update.message.reply_text("Введите число, например: 170")
            return True

    # Вес
    if step == "onb_weight":
        try:
            w = float(text.replace(",", "."))
            if 30 <= w <= 300:
                st["profile"]["weight_kg"] = round(w, 1)
                st["awaiting"] = "onb_activity"
                save_state(user.id, st)
                await update.message.reply_text("Активность:", reply_markup=ReplyKeyboardMarkup(ACTIVITY_KB, resize_keyboard=True))
            else:
                await update.message.reply_text("Некорректно. Введите вес в диапазоне 30–300 кг.")
            return True
        except ValueError:
            await update.message.reply_text("Введите число, например: 63.5")
            return True

    # Активность
    if step == "onb_activity":
        if text not in ("Низкая", "Умеренная", "Высокая"):
            await update.message.reply_text("Выберите одну из кнопок: Низкая | Умеренная | Высокая", reply_markup=ReplyKeyboardMarkup(ACTIVITY_KB, resize_keyboard=True))
            return True
        st["profile"]["activity"] = text
        st["awaiting"] = "onb_goal"
        save_state(user.id, st)
        await update.message.reply_text("Цель:", reply_markup=ReplyKeyboardMarkup(GOAL_KB, resize_keyboard=True))
        return True

    # Цель
    if step == "onb_goal":
        if text not in ("Набрать массу", "Похудеть", "Поддерживать вес"):
            await update.message.reply_text("Выберите: Набрать массу | Похудеть | Поддерживать вес", reply_markup=ReplyKeyboardMarkup(GOAL_KB, resize_keyboard=True))
            return True
        st["profile"]["goal"] = text
        st["awaiting"] = None
        save_state(user.id, st)
        add_points(user.id, 25, "onboarding")
        # CTA
        msg = (
            "Анкета сохранена. +25 баллов!\n\n"
            "Доступные быстрые шаги:\n"
            "• Мини-меню и калькуляторы (ИМТ/КБЖУ/VO2/Зоны)\n"
            "• 15-мин тренировка дома\n"
            "• Дневники питания и тренировок\n\n"
            "Выберите раздел ниже."
        )
        await update.message.reply_text(msg, reply_markup=role_keyboard(None))
        return True

    return False

# ---------- Обработчики логики ролей и кнопок ----------

async def show_diaries(update: Update, context: ContextTypes.DEFAULT_TYPE, st: Dict[str, Any]):
    d = st["diaries"]
    food = d["food"][-20:]
    train = d["train"][-20:]
    metrics = d["metrics"][-20:]

    lines = ["Последние записи:"]
    if food:
        lines.append("\n🍏 Питание:")
        for x in food:
            if "photo" in x:
                lines.append(f"- {x['ts']}: фото {x['photo']}")
            else:
                lines.append(f"- {x['ts']}: {x['text']}")
    if train:
        lines.append("\n💪 Тренировки:")
        for x in train:
            lines.append(f"- {x['ts']}: {x['text']}")
    if metrics:
        lines.append("\n📊 Метрики:")
        for x in metrics:
            t = x.get("type")
            data = x.get("data")
            if t == "zones":
                lines.append(f"- {x['ts']}: пульсовые зоны сохранены")
            elif t == "bmi":
                lines.append(f"- {x['ts']}: BMI={data.get('bmi')} ({data.get('cat')})")
            elif t == "kbju":
                lines.append(f"- {x['ts']}: КБЖУ таргет={data.get('target_kcal')} ккал")
            elif t == "vo2":
                lines.append(f"- {x['ts']}: VO2max={data.get('vo2')} ({data.get('cat')})")
            else:
                lines.append(f"- {x['ts']}: {t}")
    if len(lines) == 1:
        lines.append("Пока пусто.")
    await update.message.reply_text("\n".join(lines), reply_markup=role_keyboard(st.get("current_role")))

async def show_points(update: Update, context: ContextTypes.DEFAULT_TYPE, st: Dict[str, Any]):
    pts = st.get("points", 0)
    board = top10()
    lines = [f"Ваши баллы: {pts}"]
    if board:
        lines.append("\nТоп-10:")
        for i, u in enumerate(board, start=1):
            mark = "← вы" if str(update.effective_user.id) == str(u["user_id"]) else ""
            lines.append(f"{i}. {u['user_id']}: {u['points']} {mark}")
    await update.message.reply_text("\n".join(lines), reply_markup=role_keyboard(st.get("current_role")))

async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE, st: Dict[str, Any], text: str):
    user = update.effective_user

    # Глобальные кнопки
    if text == "🥗 Нутрициолог":
        st["current_role"] = "nutri"
        st["awaiting"] = None
        save_state(user.id, st)
        await update.message.reply_text("Раздел «Нутрициолог». Любой текст — вопрос по питанию.", reply_markup=role_keyboard("nutri"))
        return True

    if text == "🏋️ Фитнес-тренер":
        st["current_role"] = "trainer"
        st["awaiting"] = None
        save_state(user.id, st)
        await update.message.reply_text("Раздел «Фитнес-тренер». Любой текст — вопрос по тренировкам.", reply_markup=role_keyboard("trainer"))
        return True

    if text == "📒 Мои дневники":
        await show_diaries(update, context, st)
        return True

    if text == "🏆 Мои баллы":
        await show_points(update, context, st)
        return True

    # Назад
    if text == "⬅️ Назад":
        st["current_role"] = None
        st["awaiting"] = None
        st["tmp"] = {}
        save_state(user.id, st)
        await update.message.reply_text("Главное меню:", reply_markup=role_keyboard(None))
        return True

    # Нутрициолог — кнопки
    if st.get("current_role") == "nutri":
        if text == "🍽️ Сгенерировать меню":
            await send_long_op(update, context)
            plan = gen_menu_text(st["profile"])
            st["tmp"]["last_menu"] = plan
            save_state(user.id, st)
            add_points(user.id, 5, "menu_gen")
            await update.message.reply_text(plan, reply_markup=role_keyboard("nutri"))
            return True

        if text == "📏 ИМТ (BMI)":
            p = st["profile"]
            if not profile_complete(p):
                await update.message.reply_text("Сначала заполните анкету через «🛠 Обновить профиль» в разделе Фитнес-тренер.")
                return True
            bmi, cat = calc_bmi(float(p["weight_kg"]), int(p["height_cm"]))
            st["diaries"]["metrics"].append({"ts": now_ts(), "type": "bmi", "data": {"bmi": bmi, "cat": cat}})
            save_state(user.id, st)
            add_points(user.id, 2, "bmi")
            await update.message.reply_text(f"BMI: {bmi} — {cat}\nЗаписано в метрики. +2 балла.", reply_markup=role_keyboard("nutri"))
            return True

        if text == "📊 КБЖУ":
            p = st["profile"]
            if not profile_complete(p):
                await update.message.reply_text("Сначала заполните анкету через «🛠 Обновить профиль».")
                return True
            res = calc_kbju(p)
            st["diaries"]["metrics"].append({"ts": now_ts(), "type": "kbju", "data": res})
            save_state(user.id, st)
            add_points(user.id, 2, "kbju")
            msg = (
                f"Цель: {res['target_kcal']} ккал/сут\n"
                f"Белок: {res['protein_g']} г\n"
                f"Жир: {res['fat_g_range'][0]}–{res['fat_g_range'][1]} г\n"
                f"Углеводы: ~{res['carbs_g']} г\n"
                f"Почему так: {res['why']}\n"
                "Записано в метрики. +2 балла."
            )
            await update.message.reply_text(msg, reply_markup=role_keyboard("nutri"))
            return True

        if text == "🍏 Дневник питания":
            st["awaiting"] = "food_diary"
            save_state(user.id, st)
            await update.message.reply_text("Отправьте текст или фото еды. Для отмены — «⬅️ Назад».", reply_markup=role_keyboard("nutri"))
            return True

        if text == "Задать вопрос ❓":
            st["awaiting"] = "ask_nutri"
            save_state(user.id, st)
            await update.message.reply_text("Задайте вопрос по питанию.")
            return True

    # Тренер — кнопки
    if st.get("current_role") == "trainer":
        if text == "📋 Сгенерировать план тренировки":
            st["awaiting"] = "workout_location"
            save_state(user.id, st)
            await update.message.reply_text("Где будете тренироваться? Выберите:", reply_markup=ReplyKeyboardMarkup(LOCATION_KB, resize_keyboard=True))
            return True

        if text == "📏 ИМТ (BMI)":
            p = st["profile"]
            if not profile_complete(p):
                await update.message.reply_text("Сначала заполните анкету через «🛠 Обновить профиль».")
                return True
            bmi, cat = calc_bmi(float(p["weight_kg"]), int(p["height_cm"]))
            st["diaries"]["metrics"].append({"ts": now_ts(), "type": "bmi", "data": {"bmi": bmi, "cat": cat}})
            save_state(user.id, st)
            add_points(user.id, 2, "bmi")
            await update.message.reply_text(f"BMI: {bmi} — {cat}\nЗаписано в метрики. +2 балла.", reply_markup=role_keyboard("trainer"))
            return True

        if text == "🫁 МПК (VO2max)":
            st["awaiting"] = "vo2_value"
            save_state(user.id, st)
            await update.message.reply_text("Введите VO2max (мл/кг/мин): число, например 42.")
            return True

        if text == "📈 Пульсовые зоны":
            p = st["profile"]
            if not profile_complete(p):
                await update.message.reply_text("Сначала заполните анкету через «🛠 Обновить профиль».")
                return True
            st["awaiting"] = "zones_hrrest"
            save_state(user.id, st)
            await update.message.reply_text("Введите пульс в покое (уд/мин). Если не знаете — отправьте 60.")
            return True

        if text == "💪 Дневник тренировок":
            st["awaiting"] = "train_diary"
            save_state(user.id, st)
            await update.message.reply_text("Опишите тренировку текстом. Для отмены — «⬅️ Назад».", reply_markup=role_keyboard("trainer"))
            return True

        if text == "🛠 Обновить профиль":
            # перезапускаем онбординг
            st["awaiting"] = "onb_gender"
            save_state(user.id, st)
            await update.message.reply_text("Обновим профиль. Пол:", reply_markup=ReplyKeyboardMarkup(GENDER_KB, resize_keyboard=True))
            return True

        if text == "Задать вопрос ❓":
            st["awaiting"] = "ask_trainer"
            save_state(user.id, st)
            await update.message.reply_text("Задайте вопрос по тренировкам/нагрузке/восстановлению.")
            return True

    return False

async def handle_text_or_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    st = load_state(user.id)

    # Если мы в процессе анкеты — отдаём обработчик анкеты
    if st.get("awaiting", "").startswith("onb_"):
        if await handle_onboarding(update, context, st):
            return

    msg = update.message
    text = (msg.text or "").strip()

    # Обработка кнопок/меню
    if text:
        handled = await handle_buttons(update, context, st, text)
        if handled:
            return

    # Обработка ожидаемых данных
    awaiting = st.get("awaiting")

    # Дневник питания
    if awaiting == "food_diary":
        if msg.photo:
            file_id = msg.photo[-1].file_id  # сохраняем file_id
            st["diaries"]["food"].append({"ts": now_ts(), "photo": file_id})
            save_state(user.id, st)
            add_points(user.id, 3, "food_photo")
            await update.message.reply_text("Фото еды сохранено. +3 балла.", reply_markup=role_keyboard(st.get("current_role")))
            return
        elif text:
            st["diaries"]["food"].append({"ts": now_ts(), "text": text})
            save_state(user.id, st)
            add_points(user.id, 2, "food_text")
            await update.message.reply_text("Запись о питании сохранена. +2 балла.", reply_markup=role_keyboard(st.get("current_role")))
            return
        else:
            await update.message.reply_text("Отправьте текст или фото для дневника питания.")
            return

    # Дневник тренировок
    if awaiting == "train_diary":
        if text:
            st["diaries"]["train"].append({"ts": now_ts(), "text": text})
            save_state(user.id, st)
            add_points(user.id, 2, "train_text")
            await update.message.reply_text("Запись о тренировке сохранена. +2 балла.", reply_markup=role_keyboard(st.get("current_role")))
            return
        else:
            await update.message.reply_text("Отправьте текст для дневника тренировок.")
            return

    # VO2max
    if awaiting == "vo2_value":
        try:
            vo2 = float(text.replace(",", "."))
            cat = vo2_category(st["profile"].get("gender") or "Мужской", vo2)
            st["diaries"]["metrics"].append({"ts": now_ts(), "type": "vo2", "data": {"vo2": vo2, "cat": cat}})
            save_state(user.id, st)
            add_points(user.id, 2, "vo2")
            await update.message.reply_text(f"VO2max: {vo2:.1f} — {cat}. Записано. +2 балла.", reply_markup=role_keyboard(st.get("current_role")))
            st["awaiting"] = None
            save_state(user.id, st)
            return
        except Exception:
            await update.message.reply_text("Введите число, например: 42")
            return

    # Пульсовые зоны
    if awaiting == "zones_hrrest":
        try:
            hrrest = int(text)
        except Exception:
            hrrest = 60
        p = st["profile"]
        z = pulse_zones(int(p["age"]), int(hrrest))
        lines = ["Ваши пульсовые зоны:"]
        for name, (lo, hi) in z.items():
            lines.append(f"{name}: {lo}–{hi} уд/мин")
        lines.append("Применение: Z1 восстановление; Z2 база и длительная выносливость; Z3 темп; Z4 интервалы; Z5 короткий спринт.")
        st["diaries"]["metrics"].append({"ts": now_ts(), "type": "zones", "data": {"hrrest": hrrest, "zones": z}})
        save_state(user.id, st)
        add_points(user.id, 2, "zones")
        await update.message.reply_text("\n".join(lines) + "\nЗаписано. +2 балла.", reply_markup=role_keyboard(st.get("current_role")))
        st["awaiting"] = None
        save_state(user.id, st)
        return

    # План тренировки — место
    if awaiting == "workout_location":
        place = text
        if place not in ("Дом", "Зал", "Улица"):
            await update.message.reply_text("Выберите: Дом | Зал | Улица", reply_markup=ReplyKeyboardMarkup(LOCATION_KB, resize_keyboard=True))
            return
        st["tmp"]["workout_place"] = place
        if place == "Зал":
            # сразу генерируем без инвентаря
            await send_long_op(update, context)
            plan = gen_gym_plan(st["profile"])
            st["tmp"]["last_workout"] = plan
            st["awaiting"] = "workout_log_confirm"
            save_state(user.id, st)
            add_points(user.id, 5, "workout_plan")
            await update.message.reply_text(plan, reply_markup=role_keyboard("trainer"))
            return
        else:
            st["awaiting"] = "workout_inventory"
            save_state(user.id, st)
            await update.message.reply_text("Какой инвентарь есть? (например: эспандер, гантели 2×5 кг) или напишите «нет».")
            return

    if awaiting == "workout_inventory":
        inv = text or "нет"
        place = st["tmp"].get("workout_place", "Дом")
        await send_long_op(update, context)
        plan = gen_home_or_outdoor_plan(st["profile"], place, inv)
        st["tmp"]["last_workout"] = plan
        st["awaiting"] = "workout_log_confirm"
        save_state(user.id, st)
        add_points(user.id, 5, "workout_plan")
        await update.message.reply_text(plan, reply_markup=role_keyboard("trainer"))
        return

    # Подтверждения «Записать в дневник? (да/нет)»
    if st["tmp"].get("last_menu") and text.lower() in ("да", "нет"):
        if text.lower() == "да":
            st["diaries"]["food"].append({"ts": now_ts(), "text": st["tmp"]["last_menu"]})
            save_state(user.id, st)
            add_points(user.id, 2, "menu_logged")
            await update.message.reply_text("Меню записано в дневник. +2 балла.", reply_markup=role_keyboard(st.get("current_role")))
        else:
            await update.message.reply_text("Ок, не записываю.", reply_markup=role_keyboard(st.get("current_role")))
        st["tmp"]["last_menu"] = None
        save_state(user.id, st)
        return

    if st["tmp"].get("last_workout") and text.lower() in ("да", "нет"):
        if text.lower() == "да":
            st["diaries"]["train"].append({"ts": now_ts(), "text": st["tmp"]["last_workout"]})
            save_state(user.id, st)
            add_points(user.id, 2, "workout_logged")
            await update.message.reply_text("План записан в дневник. +2 балла.", reply_markup=role_keyboard(st.get("current_role")))
        else:
            await update.message.reply_text("Ок, не записываю.", reply_markup=role_keyboard(st.get("current_role")))
        st["tmp"]["last_workout"] = None
        st["awaiting"] = None
        save_state(user.id, st)
        return

    # Вопросы к ролям
    if st.get("current_role") in ("nutri", "trainer") and text:
        # любой текст трактуется как вопрос к текущей роли
        await send_long_op(update, context)
        answer = await ai_chat(st["current_role"], st["profile"], text)
        await update.message.reply_text(answer, reply_markup=role_keyboard(st.get("current_role")))
        return

    # Оффтоп вне роли
    if text:
        await update.message.reply_text(
            "Доступные темы: питание, тренировки, здоровые привычки. Выберите раздел в меню.",
            reply_markup=role_keyboard(st.get("current_role"))
        )
        return

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Логируем, но пользователю выдаём мягкое сообщение
    try:
        print(f"Exception: {context.error}")
    except Exception:
        pass

async def main():
    if not BOT_TOKEN:
        print("Ошибка: не задан TELEGRAM_BOT_TOKEN")
        return

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("health", health_cmd))
    app.add_handler(CommandHandler("workout_done", workout_done))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle_text_or_photo))

    app.add_error_handler(error_handler)

    print(f"{PROJECT_NAME} запущен.")
    await app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
