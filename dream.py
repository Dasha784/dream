import os
import asyncio
import json
import sqlite3
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional, Tuple
import random

# Non-repeat cache for short advice lines to avoid repetition across recent answers
_recent_cache: Dict[str, List[str]] = {}

def choose_nonrepeat(options: List[str], key: str, k: int = 5) -> str:
    used = _recent_cache.get(key, [])
    candidates = [o for o in options if o not in used]
    if not candidates:
        candidates = options[:]
        used = []
    choice = random.choice(candidates)
    used.append(choice)
    if len(used) > k:
        used = used[-k:]
    _recent_cache[key] = used
    return choice

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

try:
    from google import genai as genai_new
except Exception:
    genai_new = None 


# Импорт конфигурации (поддерживает как переменные окружения, так и config.json)
try:
    from config import TELEGRAM_BOT_TOKEN, GOOGLE_API_KEY, GEMINI_MODEL, DB_PATH
except ImportError:
    # Fallback на прямое чтение из переменных окружения, если config.py нет
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
    GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
    DB_PATH = os.getenv("DREAMMAP_DB", os.path.join(os.path.dirname(__file__), "dreammap.sqlite3"))

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Please set TELEGRAM_BOT_TOKEN in environment variables.")

if GOOGLE_API_KEY and genai_new is not None:
    pass
# Ensure the directory for the SQLite DB exists (helps when using mounted volumes like /data)
_db_dir = os.path.dirname(DB_PATH) or "."
try:
    os.makedirs(_db_dir, exist_ok=True)
except Exception:
    pass


def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def db_migrate() -> None:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_user_id INTEGER UNIQUE NOT NULL,
            username TEXT,
            language TEXT,
            premium INTEGER DEFAULT 0,
            default_mode TEXT DEFAULT 'Mixed',
            notifications_enabled INTEGER DEFAULT 0,
            daily_hour INTEGER DEFAULT 9,
            last_daily_sent TEXT,
            created_at TEXT
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS dreams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            raw_text TEXT NOT NULL,
            created_at TEXT,
            model_version TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dream_id INTEGER NOT NULL,
            language TEXT,
            mode TEXT,
            json_struct TEXT,
            mixed_interpretation TEXT,
            psych_interpretation TEXT,
            esoteric_interpretation TEXT,
            advice TEXT,
            created_at TEXT,
            FOREIGN KEY(dream_id) REFERENCES dreams(id)
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS qa (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            question TEXT,
            answer TEXT,
            created_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        """
    )
    conn.commit()
    try:
        cur.execute("ALTER TABLE users ADD COLUMN default_mode TEXT DEFAULT 'Mixed'")
    except Exception:
        pass
    try:
        cur.execute("ALTER TABLE users ADD COLUMN notifications_enabled INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        cur.execute("ALTER TABLE users ADD COLUMN daily_hour INTEGER DEFAULT 9")
    except Exception:
        pass
    try:
        cur.execute("ALTER TABLE users ADD COLUMN last_daily_sent TEXT")
    except Exception:
        pass
    # Timezone-aware notification columns
    try:
        cur.execute("ALTER TABLE users ADD COLUMN timezone TEXT DEFAULT 'Europe/Kyiv'")
    except Exception:
        pass
    try:
        cur.execute("ALTER TABLE users ADD COLUMN morning_hour INTEGER DEFAULT 8")
    except Exception:
        pass
    try:
        cur.execute("ALTER TABLE users ADD COLUMN evening_hour INTEGER DEFAULT 20")
    except Exception:
        pass
    try:
        cur.execute("ALTER TABLE users ADD COLUMN last_morning_sent TEXT")
    except Exception:
        pass
    try:
        cur.execute("ALTER TABLE users ADD COLUMN last_evening_sent TEXT")
    except Exception:
        pass
    conn.commit()
    conn.close()


def row_get(row: Optional[sqlite3.Row], key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        return row[key]
    except Exception:
        return default


def get_lang_for_user(tg_user_id: int, fallback: str = "ru") -> str:
    u = get_user(tg_user_id)
    val = row_get(u, "language", fallback)
    return val if val else fallback


def set_language_for_user(tg_user_id: int, language: str) -> None:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET language=? WHERE tg_user_id=?", (language, tg_user_id))
    conn.commit()
    conn.close()


def set_timezone_for_user(tg_user_id: int, tz: str) -> None:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET timezone=? WHERE tg_user_id=?", (tz, tg_user_id))
    conn.commit()
    conn.close()


def get_or_create_user(tg_user_id: int, username: Optional[str], language: str) -> int:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE tg_user_id = ?", (tg_user_id,))
    r = cur.fetchone()
    if r:
        user_id = int(r[0])
        cur.execute("UPDATE users SET username = COALESCE(?, username), language=? WHERE id=?", (username, language, user_id))
        conn.commit()
        conn.close()
        return user_id
    cur.execute(
        "INSERT INTO users (tg_user_id, username, language, premium, created_at) VALUES (?,?,?,?,?)",
        (tg_user_id, username, language, 0, datetime.utcnow().isoformat()),
    )
    user_id = cur.lastrowid
    conn.commit()
    conn.close()
    return int(user_id)


def get_user(tg_user_id: int) -> Optional[sqlite3.Row]:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE tg_user_id = ?", (tg_user_id,))
    r = cur.fetchone()
    conn.close()
    return r


def set_user_mode(tg_user_id: int, mode: str) -> None:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET default_mode=? WHERE tg_user_id=?", (mode, tg_user_id))
    conn.commit()
    conn.close()


def set_notifications(tg_user_id: int, enabled: int, hour: Optional[int] = None) -> None:
    conn = db_conn()
    cur = conn.cursor()
    if hour is not None:
        cur.execute("UPDATE users SET notifications_enabled=?, daily_hour=? WHERE tg_user_id=?", (enabled, hour, tg_user_id))
    else:
        cur.execute("UPDATE users SET notifications_enabled=? WHERE tg_user_id=?", (enabled, tg_user_id))
    conn.commit()
    conn.close()


def mark_daily_sent(tg_user_id: int, date_str: str) -> None:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET last_daily_sent=? WHERE tg_user_id=?", (date_str, tg_user_id))
    conn.commit()
    conn.close()


def insert_dream(user_id: int, text: str, model_version: str) -> int:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO dreams (user_id, raw_text, created_at, model_version) VALUES (?,?,?,?)",
        (user_id, text.strip(), datetime.utcnow().isoformat(), model_version),
    )
    dream_id = cur.lastrowid
    conn.commit()
    conn.close()
    return int(dream_id)


def insert_analysis(dream_id: int, language: str, mode: str, json_struct: str, mixed: str, psych: str, esoteric: str, advice: str) -> None:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO analyses (dream_id, language, mode, json_struct, mixed_interpretation, psych_interpretation, esoteric_interpretation, advice, created_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (dream_id, language, mode, json_struct, mixed, psych, esoteric, advice, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def get_user_stats(user_id: int) -> Dict[str, Any]:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM dreams WHERE user_id=?", (user_id,))
    total_dreams = cur.fetchone()[0]
    cur.execute(
        "SELECT COUNT(*) FROM analyses a JOIN dreams d ON a.dream_id=d.id WHERE d.user_id=?",
        (user_id,),
    )
    total_analyses = cur.fetchone()[0]
    cur.execute(
        "SELECT a.json_struct FROM analyses a JOIN dreams d ON a.dream_id=d.id WHERE d.user_id=? ORDER BY a.id DESC LIMIT 50",
        (user_id,),
    )
    rows = cur.fetchall()
    themes: Dict[str, int] = {}
    archetypes: Dict[str, int] = {}
    emotions: Dict[str, float] = {}
    n_emotions = 0
    for row in rows:
        try:
            js = json.loads(row[0]) if row and row[0] else {}
        except Exception:
            js = {}
        for t in js.get("themes", []) or []:
            themes[t] = themes.get(t, 0) + 1
        for a in js.get("archetypes", []) or []:
            archetypes[a] = archetypes.get(a, 0) + 1
        for e in js.get("emotions", []) or []:
            lbl = e.get("label")
            sc = float(e.get("score", 0))
            if lbl:
                emotions[lbl] = emotions.get(lbl, 0.0) + sc
                n_emotions += 1
    conn.close()
    return {
        "total_dreams": total_dreams,
        "total_analyses": total_analyses,
        "top_themes": sorted(themes.items(), key=lambda x: x[1], reverse=True)[:5],
        "top_archetypes": sorted(archetypes.items(), key=lambda x: x[1], reverse=True)[:5],
        "avg_emotions": {k: round(v / max(n_emotions, 1), 3) for k, v in emotions.items()},
    }


def user_is_premium(tg_user_id: int) -> bool:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT premium FROM users WHERE tg_user_id=?", (tg_user_id,))
    r = cur.fetchone()
    conn.close()
    if not r:
        return False
    return bool(r[0])


UA_CHARS = set("іїєґІЇЄҐ")


def detect_lang(text: str) -> str:
    t = text or ""
    if any(c in UA_CHARS for c in t):
        return "uk"
    if re.search(r"[А-Яа-яЁёЇїІіЄєҐґ]", t):
        return "ru"
    return "en"


def choose_ui_text(lang: str) -> Dict[str, str]:
    if lang == "uk":
        return {
            "hello": "Вітаю! Надішли текст сну, і я надам структурований аналіз (Mixed). Команда /dream — також приймає сон.",
            "prompt_dream": "Будь ласка, надішли текст сну одним повідомленням.",
            "processing": "Опрацьовую сон…",
            "no_api": "Аналіз доступний після налаштування GOOGLE_API_KEY.",
            "done": "Готово.",
            "image_paid": "Генерація зображень — платна функція. У вас наразі безкоштовний тариф.",
            "image_ok": "магія читає ваші сни🔮🔮🔮:",
            "ask_need_text": "Використай: /ask ваше запитання",
            "stats_title": "Статистика ваших снів",
        }
    if lang == "ru":
        return {
            "hello": "Привет! Пришли текст сна — верну структурированный анализ (Mixed). Команда /dream — тоже принимает сон.",
            "prompt_dream": "Пожалуйста, отправь текст сна одним сообщением.",
            "processing": "магия читает ваши сны🔮🔮🔮",
            "no_api": "Анализ доступен после настройки GOOGLE_API_KEY.",
            "done": "Готово.",
            "image_paid": "Генерация изображений — платная функция. У вас сейчас бесплатный тариф.",
            "image_ok": "Готовлю визуализацию (демо-описание):",
            "ask_need_text": "Используй: /ask ваш вопрос",
            "stats_title": "Статистика ваших снов",
        }
    return {
        "hello": "Hi! Send your dream text to get a structured Mixed interpretation. You can also use /dream.",
        "prompt_dream": "Please send your dream text in a single message.",
        "processing": "Magic reads your dreams🔮🔮🔮",
        "no_api": "Analysis requires GOOGLE_API_KEY to be set.",
        "done": "Done.",
        "image_paid": "Image generation is a paid feature. You are currently on the free tier.",
        "image_ok": "Preparing visualization (demo description):",
        "ask_need_text": "Use: /ask your question",
        "stats_title": "Your dream stats",
    }


def menu_labels(lang: str) -> Dict[str, str]:
    if lang == "uk":
        return {
            "compat": "Сумісність",
            "interpret": "Тлумачення снів",
            "spreads": "Розклади",
            "diary": "Щоденник снів",
            "settings": "Налаштування / Підписка",
        }
    if lang == "ru":
        return {
            "compat": "Совместимость",
            "interpret": "Интерпретация снов",
            "spreads": "Расклады",
            "diary": "Дневник снов",
            "settings": "Настройки / Подписка",
        }
    return {
        "compat": "Compatibility",
        "interpret": "Dream Interpretation",
        "spreads": "Spreads",
        "diary": "Dream Diary",
        "settings": "Settings / Subscription",
    }


def main_menu_kb(lang: str) -> ReplyKeyboardMarkup:
    m = menu_labels(lang)
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        keyboard=[
            [KeyboardButton(text=m["compat"]), KeyboardButton(text=m["interpret"])],
            [KeyboardButton(text=m["spreads"]), KeyboardButton(text=m["diary"])],
            [KeyboardButton(text=m["settings"])],
        ],
    )


def compat_menu_kb(lang: str) -> InlineKeyboardMarkup:
    if lang == "uk":
        items = [("За снами", "compat:by_dreams"), ("За датами народження", "compat:by_birthdates"), ("За архетипами", "compat:by_archetypes")]
    elif lang == "ru":
        items = [("По снам", "compat:by_dreams"), ("По датам рождения", "compat:by_birthdates"), ("По архетипам", "compat:by_archetypes")]
    else:
        items = [("By dreams", "compat:by_dreams"), ("By birthdates", "compat:by_birthdates"), ("By archetypes", "compat:by_archetypes")]
    kb = InlineKeyboardBuilder()
    for text, data in items:
        kb.button(text=text, callback_data=data)
    kb.adjust(1)
    return kb.as_markup()


def settings_timezone_kb(lang: str) -> InlineKeyboardMarkup:
    if lang == "uk":
        items = [("Київ (Europe/Kyiv)", "settings:tz:Europe/Kyiv"), ("Париж (Europe/Paris)", "settings:tz:Europe/Paris"), ("Лондон (Europe/London)", "settings:tz:Europe/London")]
    elif lang == "ru":
        items = [("Киев (Europe/Kyiv)", "settings:tz:Europe/Kyiv"), ("Париж (Europe/Paris)", "settings:tz:Europe/Paris"), ("Лондон (Europe/London)", "settings:tz:Europe/London")]
    else:
        items = [("Kyiv (Europe/Kyiv)", "settings:tz:Europe/Kyiv"), ("Paris (Europe/Paris)", "settings:tz:Europe/Paris"), ("London (Europe/London)", "settings:tz:Europe/London")]
    kb = InlineKeyboardBuilder()
    for text, data in items:
        kb.button(text=text, callback_data=data)
    kb.adjust(1)
    return kb.as_markup()


CITY_TO_TZ = {
    # Europe
    "kyiv": "Europe/Kyiv",
    "kiev": "Europe/Kyiv",
    "paris": "Europe/Paris",
    "london": "Europe/London",
    "berlin": "Europe/Berlin",
    "warsaw": "Europe/Warsaw",
    "madrid": "Europe/Madrid",
    "rome": "Europe/Rome",
    "prague": "Europe/Prague",
    "vienna": "Europe/Vienna",
    # Americas
    "newyork": "America/New_York",
    "new york": "America/New_York",
    "losangeles": "America/Los_Angeles",
    "los angeles": "America/Los_Angeles",
    "toronto": "America/Toronto",
    # Asia
    "tokyo": "Asia/Tokyo",
    "seoul": "Asia/Seoul",
    "singapore": "Asia/Singapore",
}


MORNING_VARIANTS = {
    "ru": [
        "Доброе утро ☀️ Что приснилось сегодня? Хотите нежный прогноз на день?",
        "Просыпаемся мягко ☀️ Поделитесь сном — и заглянем в энергии дня ✨",
        "С новыми силами! ☀️ О чём шептал сон этой ночью? Готовы к лёгкому раскладу дня?",
    ],
    "uk": [
        "Добрий ранок ☀️ Що наснилося сьогодні? Хочеш м’який прогноз на день?",
        "Прокидаймось ніжно ☀️ Поділися сном — і зазирнемо в енергії дня ✨",
        "З новими силами! ☀️ Про що шептав сон цієї ночі? Готовий(а) до легкого розкладу дня?",
    ],
    "en": [
        "Good morning ☀️ What did you dream about? Want a gentle forecast for your day?",
        "Wake softly ☀️ Share your dream — let’s peek into today’s energies ✨",
        "Fresh start! ☀️ What whispered in your dreams? Ready for a light day preview?",
    ],
}


EVENING_VARIANTS = {
    "ru": [
        "Как прошёл твой день? 🌙 Пара строк — и добавлю в дневник снов.",
        "Вечерняя пауза 🌙 Поделись ощущениями: что было главным сегодня?",
        "Тихий вечер 🌙 О чём было твоё состояние днём? Запишем аккуратно.",
    ],
    "uk": [
        "Як минув твій день? 🌙 Кілька рядків — і додам у щоденник снів.",
        "Вечірня пауза 🌙 Поділися відчуттями: що було головним сьогодні?",
        "Тихий вечір 🌙 Про що був твій стан вдень? Запишемо дбайливо.",
    ],
    "en": [
        "How was your day? 🌙 A few lines — I’ll add it to your dream diary.",
        "Evening pause 🌙 Share your feelings: what stood out today?",
        "Soft night 🌙 What did your day feel like? Let’s note it gently.",
    ],
}


def morning_text(lang: str) -> str:
    arr = MORNING_VARIANTS.get(lang) or MORNING_VARIANTS["en"]
    return random.choice(arr)


def evening_text(lang: str) -> str:
    arr = EVENING_VARIANTS.get(lang) or EVENING_VARIANTS["en"]
    return random.choice(arr)


def interpret_menu_kb(lang: str) -> InlineKeyboardMarkup:
    if lang == "uk":
        items = [("Mixed", "interpret:mixed"), ("Psychological", "interpret:psych"), ("Custom", "interpret:custom"), ("Зробити режимом за замовч.", "interpret:set_mode")]
    elif lang == "ru":
        items = [("Mixed", "interpret:mixed"), ("Psychological", "interpret:psych"), ("Custom", "interpret:custom"), ("Сделать режимом по умолч.", "interpret:set_mode")]
    else:
        items = [("Mixed", "interpret:mixed"), ("Psychological", "interpret:psych"), ("Custom", "interpret:custom"), ("Set as default", "interpret:set_mode")]
    kb = InlineKeyboardBuilder()
    for text, data in items:
        kb.button(text=text, callback_data=data)
    kb.adjust(2)
    return kb.as_markup()


def spreads_menu_kb(lang: str) -> InlineKeyboardMarkup:
    if lang == "uk":
        items = [("1 карта (порада)", "spreads:one"), ("3 карти (П/Н/М)", "spreads:three"), ("5 карт (глибоко)", "spreads:five")]
    elif lang == "ru":
        items = [("1 карта (совет)", "spreads:one"), ("3 карты (П/Н/Б)", "spreads:three"), ("5 карт (глубоко)", "spreads:five")]
    else:
        items = [("1 card (advice)", "spreads:one"), ("3 cards (P/N/F)", "spreads:three"), ("5 cards (deep)", "spreads:five")]
    kb = InlineKeyboardBuilder()
    for text, data in items:
        kb.button(text=text, callback_data=data)
    kb.adjust(1)
    return kb.as_markup()


def diary_menu_kb(lang: str) -> InlineKeyboardMarkup:
    if lang == "uk":
        items = [("Історія", "diary:history"), ("Статистика", "diary:stats"), ("Карта символів", "diary:symbol_map"), ("Попередження", "diary:warnings")]
    elif lang == "ru":
        items = [("История", "diary:history"), ("Статистика", "diary:stats"), ("Карта символов", "diary:symbol_map"), ("Предупреждения", "diary:warnings")]
    else:
        items = [("History", "diary:history"), ("Stats", "diary:stats"), ("Symbol map", "diary:symbol_map"), ("Warnings", "diary:warnings")]
    kb = InlineKeyboardBuilder()
    for text, data in items:
        kb.button(text=text, callback_data=data)
    kb.adjust(2)
    return kb.as_markup()


def settings_menu_kb(lang: str) -> InlineKeyboardMarkup:
    if lang == "uk":
        items = [("Режим за замовч.", "settings:mode"), ("Увімкнути нотиф.", "settings:notifications_on"), ("Вимкнути нотиф.", "settings:notifications_off"), ("Мови", "settings:languages"), ("Часовий пояс", "settings:timezone")]
    elif lang == "ru":
        items = [("Режим по умолч.", "settings:mode"), ("Включить уведомл.", "settings:notifications_on"), ("Выключить уведомл.", "settings:notifications_off"), ("Языки", "settings:languages"), ("Часовой пояс", "settings:timezone")]
    else:
        items = [("Default mode", "settings:mode"), ("Enable notif.", "settings:notifications_on"), ("Disable notif.", "settings:notifications_off"), ("Languages", "settings:languages"), ("Timezone", "settings:timezone")]
    kb = InlineKeyboardBuilder()
    for text, data in items:
        kb.button(text=text, callback_data=data)
    kb.adjust(2)
    return kb.as_markup()


def settings_languages_kb(lang: str) -> InlineKeyboardMarkup:
    if lang == "uk":
        items = [("Українська", "settings:language:uk"), ("Русский", "settings:language:ru"), ("English", "settings:language:en")]
    elif lang == "ru":
        items = [("Русский", "settings:language:ru"), ("Українська", "settings:language:uk"), ("English", "settings:language:en")]
    else:
        items = [("English", "settings:language:en"), ("Русский", "settings:language:ru"), ("Українська", "settings:language:uk")]
    kb = InlineKeyboardBuilder()
    for text, data in items:
        kb.button(text=text, callback_data=data)
    kb.adjust(1)
    return kb.as_markup()

def gemini_client():
    if not GOOGLE_API_KEY or genai_new is None:
        return None
    try:
        return genai_new.Client(api_key=GOOGLE_API_KEY)
    except Exception:
        return None


def build_struct_prompt(dream_text: str, lang: str) -> str:
    if lang == "uk":
        return (
            "Завдання: розбери сон на структуру й поверни строгий JSON без коментарів.\n"
            "Поля: location, characters[{name,role}], actions[], symbols[], emotions[{label,score:0..1}], themes[], archetypes[], summary.\n"
            f"Текст сну: \"{dream_text}\"\n"
            "ПОВЕРТАЙ лише JSON."
        )
    if lang == "ru":
        return (
            "Задача: разберите сон на структуру и верните строгий JSON без комментариев.\n"
            "Поля: location, characters[{name,role}], actions[], symbols[], emotions[{label,score:0..1}], themes[], archetypes[], summary.\n"
            f"Текст сна: \"{dream_text}\"\n"
            "ВЕРНИТЕ только JSON."
        )
    return (
        "Task: parse the dream into a structure and return strict JSON only.\n"
        "Fields: location, characters[{name,role}], actions[], symbols[], emotions[{label,score:0..1}], themes[], archetypes[], summary.\n"
        f"Dream text: \"{dream_text}\"\n"
        "RETURN JSON only."
    )


def build_style_header(lang: str) -> str:
    dream_symbolism_guide = (
        "\n\n📚 СИМВОЛИКА СНОВИДЕНИЙ — используй для точной интерпретации:\n\n"
        "🔴 СТИХИИ:\n"
        "• Огонь (солнце, свечи, пожар) — жизненная энергия, сексуальность, творчество, страсть, духовность, гнев\n"
        "• Вода (море, река, дождь) — эмоции, чувства, бессознательное, очищение. Река = динамика психических процессов. "
        "Плотина = блокировка эмоций. Бурный поток = беспокойство. Тонуть = переполненность эмоциями. "
        "Лед/снег = эмоциональный холод. Море = чувства, архетип матери. Шторм = сильные чувства\n"
        "• Земля (природа, растения) — инстинкты, физическая энергия, материальные проблемы, безопасность. "
        "Под землей = прошлое тянет, скрываемое\n"
        "• Воздух (летать, ветер) — ментальная энергия, свобода, интеллект. Задыхаться = ограничения. "
        "Летать = освобождение. Удушение = невозможность самовыражаться\n\n"
        "🌍 ПРИРОДА:\n"
        "• Вулкан — эмоциональный взрыв, конфликт\n"
        "• Дорога/путь — жизненный путь, цели (смотри качество дороги)\n"
        "• Горы — восхождение, успех, рост. Восхождение = подъем в жизни. Спуск = заниженная самооценка\n"
        "• Лес — социальные связи, социум, положение в обществе\n"
        "• Пустыня — одиночество, пустота в жизни\n"
        "• Животные — инстинкты, сексуальность. Насекомые/грызуны = докучающие проблемы. "
        "Рыба = чувственная сфера. Птица = удовольствие, освобождение, информация\n\n"
        "👥 ЛЮДИ:\n"
        "• Военные/полицейские — власть, подчинение\n"
        "• Воры — нарушение границ, похищение энергии\n"
        "• Незнакомцы — непознанные аспекты себя, субличности\n"
        "• Чудовища/преследователи — подавленные эмоции, страхи, заблокированный потенциал\n\n"
        "🏠 ПРЕДМЕТЫ:\n"
        "• Дом — физическое тело, символ личности, самооценка. Подвалы = бессознательное. Крыша = сознательное. "
        "Кухня = оральные потребности. Спальня = интимно-сексуальная тематика\n"
        "• Дверь — новые возможности, выход из ситуации\n"
        "• Лестницы — восхождение, развитие, достижение\n"
        "• Автомобиль — движение к цели. Сломался = кризис, невозможность двигаться\n"
        "• Деньги/драгоценности — власть, значимость, внутренние ресурсы\n\n"
        "💪 ТЕЛО:\n"
        "• Голова — контроль. Обезглавливание = потеря контроля\n"
        "• Глаза — осознанность. Слепота = недостаток осознанности\n"
        "• Горло — самовыражение. Сдавливать = невозможность самовыражаться\n"
        "• Зубы — агрессия. Потеря зубов = невозможность защититься\n"
        "• Ноги — движение вперед. Лишиться ног = невозможность двигаться\n"
        "• Руки — контакты. Проблемы с руками = потеря работоспособности\n\n"
        "⚡ ДЕЙСТВИЯ:\n"
        "• Падение — страх неудачи, чувство неполноценности, страх потерять привязанность\n"
        "• Полет — сексуальное возбуждение, чувство превосходства, освобождение\n"
        "• Тонуть — переполненность эмоциями, подавленные чувства\n"
        "• Опаздывать — усталость, нехватка времени, психологические проблемы\n"
        "• Экзамены — страх неудачи, осуждения. Не знать ответа = непонимание себя\n"
        "• Убегать — отказываться от чего-то, избегать, нежелание общаться\n"
        "• Убивать — хочется забыть, вытеснение\n"
        "• Умирание — трансформация, рождение нового\n\n"
        "🔢 ЧИСЛА (если встречаются в снах):\n"
        "• 1 — Бог, единство, лидерство, ответственность, встреча с Богом\n"
        "• 2 — умножение, партнерство, единство, помощь. Негативно: разделение\n"
        "• 3 — Троица, совершенство, нужда в Боге, опереться на Него\n"
        "• 4 — творческая работа, порядок, стабильность, укоренение, утверждение\n"
        "• 5 — благодать, искупление, помазание, милость\n"
        "• 6 — человек, человечество, плоть, слабость, несовершенство\n"
        "• 7 — совершенство, полнота, завершение, духовное совершенство, новый сезон\n"
        "• 8 — новое начало, учитель, перемены к лучшему, переход в новый сезон\n"
        "• 9 — плоды Духа, рождение нового, новый план от Бога\n"
        "• 10 — завершение, ответственность, принадлежность Богу, посвящение\n"
        "• 11 — переход, новый путь, пророк\n"
        "• 12 — дизайн, божественное управление, порядок, позиция лидера\n"
        "• 13 — бунт\n"
        "• 14 — двойное помазание\n"
        "• 15 — помилование, милость\n"
        "• 16 — установленное новое начало\n"
        "• 17 — выбор Бога\n"
        "• 18 — новая жизнь, жизнь с избытком\n"
        "• 22 — число Невесты, управление\n"
        "• 25 — начало служения подготовки\n"
        "• 30 — начало служения\n"
        "• 37 — перворождённый, первенец\n"
        "• 40 — испытание, проверка, духовная битва, завершённое управление\n"
        "• 50 — юбилей, полное обновление, свобода, сила Бога\n"
        "• 70 — совершенный духовный порядок, восстановление, управление\n"
        "• 111 — Возлюбленный Сын\n"
        "• 120 — слава, священная власть, авторитет, конец плоти\n"
        "• 153 — умножение Царства\n"
        "• 222 — Невеста под управлением Бога\n"
        "• 666 — полное беззаконие\n"
        "• 888 — воскресение, число Иисуса Христа\n"
        "• 1500 — свет, сила, власть\n\n"
        "🎨 ЦВЕТА (важно учитывать контекст — положительное/отрицательное значение):\n"
        "• Черный — смерть, голод, наказание, грех, невежество (отрицательно)\n"
        "• Коричневый — сочувствие, смиренность (положительно); плоть, похоть, бесплодность (отрицательно)\n"
        "• Синий — откровение, общение (положительно); депрессия, тревожность (отрицательно)\n"
        "• Голубой — Святой Дух, откровение, власть Неба, небесные посещения, очищение\n"
        "• Золотой — святость, Святой Дух, Бог, царствование, слава, очищение; идолопоклонство (отрицательно)\n"
        "• Серый — достоинство, честь (положительно); смешение, компромисс, обман (отрицательно)\n"
        "• Зеленый — жизнь, рост, плодотворность, процветание, изобилие; ревность, зависть (отрицательно)\n"
        "• Оранжевый — настойчивость, слава; нью эйдж, непокорность, опасность (отрицательно)\n"
        "• Розовый — новая жизнь, чувственность; плоть (отрицательно)\n"
        "• Багряный/Пурпурно-красный — величие, королевский, власть, изобилие; ненависть (отрицательно)\n"
        "• Красный — кровь Иисуса, искупление, прощение, любовь; страдание, страсть, ревность, война (отрицательно)\n"
        "• Серебряный — цена искупления, оплата долга; законничество, доминирование (отрицательно)\n"
        "• Белый — чистота, праведность, святость, победа; религиозный дух (отрицательно)\n"
        "• Желтый — разум, надежда, дары от Господа; грех, немощь, страх (отрицательно)\n"
        "• Фиолетовый — цвет Царства, царский цвет, духовная зрелость, власть; ложная власть (отрицательно)\n\n"
        "🔮 ДОПОЛНИТЕЛЬНЫЕ СИМВОЛЫ:\n"
        "• Алтарь, жертвенник — место убоя, смерть собственного Я, посвящение Богу, ходатайство\n"
        "• Якорь — безопасность, уверенность, надежда\n"
        "• Ковчег — Христос, безопасность, спасение, Бог среди людей\n"
        "• Рука — власть, сила, спаситель, решение\n"
        "• Броня — божественное оснащение, защита, облаченный в Истину\n"
        "• Стрелы, копья — слово о Божьем избавлении; ложь, проклятия (отрицательно); быстрый суд Божий\n"
        "• Пепел, зола — покаяние, уничтожение, конец собственной жизни, траурный\n"
        "• Атомная бомба — признак последних дней, великая война, внезапное разрушение\n"
        "• Осень — изменения, переходный период, покаяние, завершение\n"
        "• Топор — Евангелие, проповедь, обличать, добраться до корня проблемы\n"
        "• Масло — исцеление, восстановление, помазание исцеления, Святой Дух\n"
        "• Знамя, флаг — Слово Божье, возвышение истины, война\n"
        "• Крещение — смерть жизни по плоти, полное погружение в работу\n"
        "• Колокола (звон) — изменение, предупреждение, Божье присутствие\n"
        "• Живот (чрево) — место эмоций, дух изольется, любовь\n"
        "• Весы — правосудие, честность, баланс, разделение\n"
        "• Бинокль, телескоп — пророческое видение, в фокусе/вне фокуса, открытие, будущие события\n"
        "• Кровь — завет, благодать, искупление, освобождение; убийство, вина (отрицательно)\n"
        "• Кости — духовное состояние Церкви, духовное состояние сердца, смерть\n"
        "• Книга — Слово Божье, обучение, божественное откровение, знание\n"
        "• Поклон — спасение, решение; обвинения, ложь (отрицательно); молитва\n"
        "• Ветвь (отрасль) — Божий народ, церкви, победы\n"
        "• Хлеб — Слово Божье, доктрина, обучение, причастие, обеспечение\n"
        "• Мост — Иисус Христос, мост между человеком и Богом, крест, вера, поддержка, единство\n"
        "• Кирпич — человеческая работа, рабство, ложный камень\n"
        "• Свеча — свет, истина, Дух Божий, Слово Божье, откровение\n"
        "• Трос, веревка — плен, неволя; единство, согласие\n"
        "• Стул — отдых, позиция, получать, ожидание\n"
        "• Мякина — грех, плоть, похоть, бесполезность, ложность\n"
        "• Круг — вечный, соглашение, бесконечный, полнота\n"
        "• Глина — человеческая плоть, слабость\n"
        "• Часы — сроки, задержка, переходный период, Божье время\n"
        "• Облачность — накануне грозы, присутствие Божье, Слава Божья, ангелы\n"
        "• Распутье (перекресток) — изменения, переходной период, решение, делать выбор, новое направление\n"
        "• Дамба, плотина — препятствие для Силы Божьей, преграда, резерв (блокировка эмоций)\n"
        "• Темнота — невежество, обман, смерть, печаль, бедствие, проклятие, слепота\n"
        "• Дневной свет — истина, свет, благословения, доброта, чистота\n"
        "• Смерть — окончательное отделение от Бога, духовная смерть, конец, покаяние, освящение\n"
        "• Алмаз — ценность, драгоценность, дар Духа, жесткое лидерство, неизменный, многогранный\n"
        "• Сон во сне — сообщение в сообщении, глубокие духовные истины, видение\n"
        "• Утопление — преодолеть депрессию, жаль, печаль, отступить от веры, чрезмерный долг, стресс, кризис по плоти\n"
        "• Ухо — слух, слушать, различение, вера от слышанья\n"
        "• Землетрясение — решение, Божья встряска, вибрация, переворот, изменения в кризисе, покаяние\n"
        "• Египет — мир, суетность, рабство, идолопоклонство, неволя, плен\n"
        "• Электричество — сила Святого Духа, власть, молитва\n"
        "• Взрыв — внезапное расширение, увеличение, рост, быстрое изменение, откровение, пробуждение\n"
        "• Глаза — зрение, видение, понимание, желание, знание, окна души, все видеть; скупость, похоть (отрицательно)\n"
        "• Лицо — характер человека, его сердце, образ, настроение\n"
        "• Падение — отпасть от Бога, падение в грех, нет поддержки, испытание, понижение в должности\n"
        "• Перья — покрытие, духовное покрытие, свобода, ангелы\n"
        "• Ноги — прогулка, твой призыв, сердце, мысли, поведение; босые ноги = без духовной подготовки\n"
        "• Огонь (пожар) — испытание, преследования, жертвоприношение, посвящение, очищение, присутствие Бога, пробуждение\n"
        "• Наводнение (потоп) — решение, потоп, сокрушение, искушение, депрессия, обвинения, наводнение воспоминаний\n\n"
        "🏢 МЕСТА И ЗДАНИЯ:\n"
        "• Аэропорт — место подготовки, ожидание, изменения, готов парить в Духе, обучение пророков\n"
        "• Банк — сокровища на небесах, церковь, защищенный, безопасный, хранение\n"
        "• Амбар — место провизии, церковь, хранение\n"
        "• Салон красоты — подготовка, святость; суета, гордость (отрицательно)\n"
        "• Кафетерий/кафе — церковная служба, снабжение Словом, выбор; компромисс (отрицательно)\n"
        "• Церковь — церковная служба, строительство, корпоративная структура, собрание\n"
        "• Город — корпоративная церковь, знание об основателе\n"
        "• Класс/школа — место обучения, инструкция, обучение служения, наставничество, ученичество\n"
        "• Суд — время судебного разбирательства, сложности, преследование, клевета, решение\n"
        "• Больница — церковь со служением исцеления; раненая церковь, нуждается в исцелении\n"
        "• Гостиница/мотель — церкви вместе, место отдыха, временное положение, переход\n"
        "• Дом — церковь, твой дом, твое тело, твоя семья. Подвал = хранение данных, прошлое, скрытые грехи. "
        "1-й этаж = плоть. Верхние этажи = дух. Чердак = дух, духовное Царство. Спальня = отдых, близость, соглашение. "
        "Ванная/туалет = очищение, освобождение, покаяние. Столовая = общение, пир через Слово. Кухня = подготовка, "
        "наставничество, обучение, духовный голод. Гостиная = место собрания, малые группы. Крыша = покрытие, защита, "
        "власть, небесное откровение\n"
        "• Новый дом — второе рождение, изменения, новое движение, возрождение\n"
        "• Старый дом — старый человек, старые пути, прошлое, наследие\n"
        "• Библиотека — исследование Словом, знания, обилие Слова, место знаний\n"
        "• Высотное строительство — пророческие церкви, большое откровение\n"
        "• Многоуровневое строительство — церковь прогрессирующая, погружение к более глубоким истинам\n"
        "• Парк — слушать, наслаждаться семьей, наслаждаться Богом, поклоняться, мир, отдых\n"
        "• Тюрьма — рабство, бунт, беззаконие, преследование, ад, неверующие, потерянные души\n"
        "• Пристань/причал — место начинания служения, место оснащения, действие основных миссий\n"
        "• Джунгли — место запутывающего беспорядка, место конкурирующих церквей\n"
        "• Леса — группа церквей, группа людей\n"
        "• Остров — группа церквей\n"
        "• Гора — церковь, Сион, новое Движение Духа Святого, великая преграда, испытание, оппозиция\n"
        "• Строительство у крутого обрыва — опасность падения с высоты, риск, прыжок веры\n"
        "• Море/океан — народы, нации, структура из людей или наций\n"
        "• Река — Слово Истины; компромисс (отрицательно); пробуждение. Чистая река = хорошее учение. "
        "Грязная река = заблуждение, ложное учение\n"
        "• Озеро/пруд/бассейн — правда, откровение, хорошее или плохое учение, стоячая вода, необходимость очищения\n"
        "• Сад — новые места служения\n"
        "• Расчищение земли — новые работы служения, отзывчивое сердце к Богу\n"
        "• Забытые земли — не откликнулись на призыв Божий, нет убежденности\n\n"
        "🚗 ТРАНСПОРТ:\n"
        "• Самолет — служение течет в Царстве Небесном, служение в движении; летит слишком высоко/низко (отрицательно)\n"
        "• Велосипед — молодое/незрелое служение, служение в Святом Духе, возможность маневра\n"
        "• Дирижабль — тяжелое служение, преисполненное гордости, раздутое, движимое ветром учения\n"
        "• Автобус — большое служение, группа служений. Школьный = молодежное служение. Церковный = служение в Церкви\n"
        "• Корабль/лодка — поместное церковное служение. Парусное = полагается на Святого Духа. "
        "Гребное = работа по плоти. Круизное = соблазняет развлечениями. Невольничье = содержит пленников. "
        "Госпитальное = служение исцеления. Военное = служение ведущее войну. Рыболовное = евангелизационное. "
        "Баржа = переполненное грехом, компромиссом. Подводная лодка = сокрыто под одной повесткой дня. "
        "Байдарка = служение плоти, нет лидерства Духа. Грузовое = перегруженное\n"
        "• Автомобиль — личное служение, церковь. Кабриолет = служение откровения. Старый = старое служение. "
        "Новый = новое служение. Сломался = кризис, невозможность двигаться. Спущенные шины = без Духа Святого. "
        "Большой = сильное служение. Маленький = нужно больше молиться\n"
        "• Облака/небо — колесница, ангельское транспортное средство, посетит вдохновение\n"
        "• Эскалатор/лифт — люди двигаются в том же направлении; поднимаются в Духе; падают вниз или регрессируют\n"
        "• Грузовик — служение помощи, услуг, транспортировки\n"
        "• Фургон — группа семейных служений\n"
        "• Полуприцеп — большое, мощное служение, несущее огромное бремя\n"
        "• Ракета — мощное служение, углубляющееся в небеса\n"
        "• Парашют — смогли преодолеть испытания и невзгоды\n"
        "• Планер — полностью зависит от Святого Духа; увлекается ветром учения\n"
        "• Трактор — духовный фермер, сеятель семени, ходатай\n"
        "• Комбайн — евангелист, жнец, ходатай\n"
        "• Поезд — великое движение Божье, великая работа Духа Святого\n\n"
        "🐾 ЖИВОТНЫЕ (детальная символика):\n"
        "• Муравей — трудолюбивый, трудная работа, мудрость, подготовка к будущему, усердие\n"
        "• Пони/ослик — терпение, выносливость; упрямство, собственные желания (отрицательно)\n"
        "• Медведь — злые люди, опасность, хитрость, жестокие мужчины\n"
        "• Пчелы — постоянная занятость, сплетня, группа людей; производят мед, но хотят ужалить\n"
        "• Птица — чистый или нечистый. Голубь = Святой Дух. Ворона = злой дух. Орел = Святой Дух, пророки, сила Бога\n"
        "• Чудовище — мирское царство, жестокость, терзания\n"
        "• Бык — сила, труд, служение\n"
        "• Кот — нечистый дух, лукавый, таинственный; личное животное может быть хорошо; хитрый, ненадежный\n"
        "• Теленок — восхваление, благодарение\n"
        "• Верблюд — носильщик бремени, служение\n"
        "• Гусеница/червь — уничтожение, пожирающий, повреждение, голод\n"
        "• Петух — предупреждение, напоминание, ранний подъем, новое начало\n"
        "• Крокодил/аллигатор — сильное проявление демонической силы, сплетни, клевета, большой рот ранит зубами\n"
        "• Собака — неверующие, лицемеры; личные животные могут быть хорошо; раскол, нападение на дело Божие\n"
        "• Осел — жесткая голова, выносливость; собственная сила воли (отрицательно)\n"
        "• Голубь — Святой Дух, мягкость\n"
        "• Дракон/динозавр — высокий уровень демонической атаки, Левиафан, духи злобы поднебесной\n"
        "• Орлы/орел — парить в Духе, хорошо это или зло лидера, сила, быстрота, сила Бога, пророки, Святой Дух\n"
        "• Рыба — душа человека, чистый или нечистый человек, чуткость в работе служения\n"
        "• Мухи — злой дух, ложь, сплетни, мерзость, извращение, царство Сатаны, мучение, примесь, Вельзевул\n"
        "• Лиса — тайные грехи, хитрый человек, обман, хищник\n"
        "• Жаба — демонический дух, ложный характер, обман\n"
        "• Кузнечик/саранча — разрушение, уничтожение, голод, бесплодие, суд Божий, множество\n"
        "• Козел — чувственный, плотской христианин, неверие, группа христиан в грехе, лжепророк\n"
        "• Заяц/кролик — сатана, злой хозяин, нечистота\n"
        "• Олень — мягкость, робость, восприимчивость, лидер церкви, верующий жаждущий Божьих указаний\n"
        "• Конь — мощное Движение Бога, основные движения Бога на земле, сила, мощь, завоевание, война, вспашка, труд\n"
        "• Агнец (ягненок) — Иисус Христос, жертва, нежный, невинный, чистота, истина верующих\n"
        "• Вши — обвинение, ложь, обман, позор\n"
        "• Лев — Иисус Христос, завоеватель, небесное посещение, уничтожитель, царь\n"
        "• Вол — пашущие, жертва, собрание\n"
        "• Сова — нечистый дух, ночная птица; мудрость (положительно); демоническая сила (отрицательно)\n"
        "• Пеликан — одинокий человек, независимый, эффективный работник\n"
        "• Баран — муж овцы или козы, гордость, мощь\n"
        "• Ворон — злой дух, нечистый, собирающий грязь и мусор\n"
        "• Скорпион — злой дух, злые люди, боль, несправедливость\n"
        "• Змей/змея — сатана, нечисть, злые люди, лгун, обманщик, проклятие, критика, плотская мудрость\n"
        "• Овца — Божьи люди, верующие, искупленные, беззащитный, церковь, Израиль; скучный в размышлениях (отрицательно)\n"
        "• Паук — лжеучение, ткет паутину обмана, соблазняющее демоническое присутствие; мудрость (положительно)\n"
        "• Аист — одиночество, производительность\n"
        "• Горлица — Святой Дух\n"
        "• Дикий осел — нераскаявшийся человек, упрямый, со своей волей, бегущий и дикий\n"
        "• Волк — сатана, ложные учителя, лжепророки, обман, разрушитель, план по уничтожению, скрывается\n"
        "• Черви — инструмент судов Божьих, голод, смерть, презрение, скверны плоти, похоть\n\n"
        "👥 ПЕРСОНАЖИ И ЛЮДИ (детальная символика):\n"
        "• Ангел — посланник от Бога, может быть личностью из церкви, защитник; хороший или плохой ангел\n"
        "• Ребенок — служение, рождение свыше, служение в младенчестве, беспомощный, нетронутый, чистый, новый христианин, "
        "новое Движение Бога, духовная незрелость\n"
        "• Невеста — церковь, завет\n"
        "• Брат — Святой Дух, духовный брат из церкви, ты сам, пастор\n"
        "• Клоун, шут — детские работы, плотские работы, играть с Богом, насмешник\n"
        "• Дочь — ребенок Божий, служение которое твой ребенок в духе, черты ребенка внутри тебя\n"
        "• Доктор — целитель, власть; мудрость мира (отрицательно)\n"
        "• Водитель — один в управлении служением или браком; хорошо или зло\n"
        "• Пьяный — под влиянием злого духа, управляемый, бунт, потакает себе, зависимость, под контролем\n"
        "• Наемный сотрудник — слуга, демонстрация подчинения, протеже\n"
        "• Наниматель (работодатель) — кто-то кто платит, хорошая или плохая власть, пастор, лидер, наставник; сатана (отрицательно)\n"
        "• Семья — церковная семья, природная семья\n"
        "• Фермер — служитель, пастор, проповедник, евангелист\n"
        "• Отец — Бог Отец, Святой Дух, власть, наследие, традиция; сатана (отрицательно); естественный отец\n"
        "• Иностранец/незнакомец — не из стада, посторонний человек, кто-то кто наблюдает\n"
        "• Великан — ангел, демон, проблема, гора на которую необходимо взойти, крепость для завоевания, зависимость\n"
        "• Губернатор/мэр — правительство, ответственное лицо, правление или господство\n"
        "• Внук — кто-то кто вышел из твоего служения, духовное наследие, наследник\n"
        "• Дедушка/бабушка — духовное наследие, прошлое, мудрость\n"
        "• Жених — Христос, брак, союз, соглашение, обещание\n"
        "• Проститутка — прелюбодеяние, искушение, ловушка, мирская церковь, мятежный, идолопоклонник, компромисс\n"
        "• Муж — Христос, муж; сатана (отрицательно); руководство, покрытие\n"
        "• Адвокат — адвокат, Христос; законничество (отрицательно)\n"
        "• Мужчина — Божий посланник, демонический посланник, злой мотив, добрый незнакомец, Иисус\n"
        "• Мать — церковь, Иерусалим, милосердие и любовь, комфорт, Святой Дух; назойливый человек (отрицательно)\n"
        "• Старый человек — похоть, мудрость, плотской человек от Адама\n"
        "• Пастор/проповедник — представляет Бога, духовная власть, домашней церкви; жена может быть церковь\n"
        "• Полиция — духовная власть в Церкви, пастор или старейшина, защита; ангелы или демоны\n"
        "• Сестра — сестра во Христе, похожие качества в самом себе, церковь, ближний товарищ церкви\n"
        "• Солдат — духовная война, ангел; демон воюющий против тебя (отрицательно); преследование, противление\n"
        "• Сын — то же что дочь только другого пола, Христос, смиренный Отцу\n"
        "• Таксист — пожиратель, неверные весы\n"
        "• Учитель — Христос, Святой Дух, откровение от Бога, учитель в Церкви, важные инструкции\n"
        "• Вор — сатана, обманщик, непорядочный, дела плоти, Иуда в служении\n"
        "• Жена — церковь, жена, соединение, покорный, невеста Христова, Святой Дух\n"
        "• Ведьма — бунт, колдовство, оккультный, практики, клевета, проклятие, непокорная жена, управление над духом, "
        "совращение, мирская церковь\n"
        "• Женщина — ангел, демон, колдовство, соблазняющий дух, искушение, ты сам\n\n"
        "💡 Используй эту символику для точной интерпретации элементов сна. Каждый символ имеет конкретное психологическое значение!"
    )
    
    dream_elements_guide = (
        "\n\n📚 СПРАВОЧНИК ЭЛЕМЕНТОВ СНОВИДЕНИЙ для обогащения анализа:\n"
        "• ДЕЙСТВИЯ: идти, бежать, прыгать, падать, лететь, скрываться, спасаться, открывать, закрывать, "
        "держать за руку, наблюдать, кричать, шептать, танцевать, плавать, нырять\n"
        "• ОБЪЕКТЫ: ключи, замки, двери, окна, лестницы, мосты, часы, книги, карты, транспорт (машины, поезда, лодки), "
        "необычные объекты (летающие, растущие, светящиеся предметы)\n"
        "• МЕСТА: города, дома, природа (леса, поля, горы, реки, океаны), фантастические места (замки, лабиринты), "
        "небо (облака, туман, звёзды)\n"
        "• ПЕРСОНАЖИ: знакомые, незнакомцы, животные (кошки, волки, змеи, пауки, птицы), фантастические существа\n"
        "• ЭМОЦИИ: радость, страх, тревога, любовь, удивление, одиночество, восхищение, ностальгия, напряжение\n"
        "• СИМВОЛЫ: вода (эмоции, поток), свет (надежда, понимание), тьма (страх, тайна), лестницы (развитие), "
        "двери (выбор), ключи (решение), огонь (энергия), растения (рост)\n"
        "• ЧУВСТВЕННЫЕ ДЕТАЛИ: блеск, мерцание, тени, музыка, шёпот, тепло, холод, влажность, цвета, переливы\n"
        "• ПАТТЕРНЫ: внутренний рост, отношения, конфликты, приключения, переходы\n\n"
        "Используй эти элементы для 'разукрашивания' анализа — добавляй описания ощущений, звуков, цветов, движения, "
        "чтобы создать живой и интересный текст. Эмодзи по смыслу (🌊 для воды/эмоций, 🔥 для энергии/страсти, "
        "✨ для магии/перемен, 🌙 для тайны, 💫 для движения, 🌸 для роста, 🗝️ для решения)."
    )
    
    if lang == "uk":
        # Translate symbolism guide to Ukrainian (simplified)
        return (
            "Пиши як інтуїтивний нічний щоденник: тепло і м'яко, без згадок про ШІ і без дисклеймерів. "
            "Короткі абзаци з плавними переходами; символи вплітай у текст, уникай сухих списків. "
            "Використовуй 1–2 доречні емодзі на розділ. Уяви, що читач читає це вночі, і сон ще поруч."
            + dream_symbolism_guide + dream_elements_guide
        )
    if lang == "ru":
        return (
            "Пиши как интуитивный дневник ночью: тепло и мягко, без упоминаний ИИ и без дисклеймеров. "
            "Короткие абзацы с мягкими связками; символы вплетай в текст, избегай сухих списков. "
            "Используй 1–2 уместных эмодзи на раздел. Представь, что читатель читает это ночью, и сон ещё рядом."
            + dream_symbolism_guide + dream_elements_guide
        )
    return (
        "Write like an intuitive night diary: warm and gentle, no AI mentions, no disclaimers. "
        "Short paragraphs with smooth transitions; weave symbols into prose, avoid dry lists. "
        "Use 1–2 fitting emojis per section; imagine the reader at night, the dream still near."
        + dream_symbolism_guide + dream_elements_guide
    )


def build_interpret_prompt(struct_json: str, mode: str, lang: str) -> str:
    if lang == "uk":
        base = "На основі структури створи глибокий, цікавий аналіз: 1) Психологічна інтерпретація (розкрий СМИСЛ сну, що він може означати в реальному житті) 2) Езотерична (м'яко, тільки якщо доречно) 3) Порада/урок (2–3 пункти)."
    elif lang == "ru":
        base = "На основе структуры создай глубокий, интересный анализ: 1) Психологическая интерпретация (раскрой СМЫСЛ сна, что он может означать в реальной жизни) 2) Эзотерическая (мягко, только если уместно) 3) Совет/урок (2–3 пункта)."
    else:
        base = "Based on the structure, create a deep, engaging analysis: 1) Psychological interpretation (uncover the MEANING of the dream, what it might mean in real life) 2) Esoteric (gently, only if appropriate) 3) Advice/lesson (2–3 bullets)."
    header = build_style_header(lang)
    if lang == "ru":
        example = (
            "Формат ОТВЕТА СТРОГО ТАКОЙ:\n"
            "Анализ сна 🌙\n"
            "Эмоции: перечисли ключевые эмоции и 1–2 уместных эмодзи\n"
            "PSYCH: 8–15 предложений ЖИВОГО анализа с ДЕТАЛЬНЫМ РАЗБОРОМ КАЖДОГО ЭЛЕМЕНТА. ОБЯЗАТЕЛЬНО:\n"
            "  1) Разбери КАЖДЫЙ ключевой элемент сна отдельно (лестница, падение, океан, ключи, двери и т.д.)\n"
            "  2) Объясни что КОНКРЕТНО означает каждый элемент психологически (например: 'Лестница 🪜 — это путь, развитие, переход между этапами. Падение — не наказание, а момент отпускания контроля')\n"
            "  3) Покажи связь между элементами — как они работают вместе, что получается в итоге\n"
            "  4) Раскрой СМЫСЛ всего сна — что он значит в реальной жизни, какие внутренние процессы отражает\n"
            "  5) Используй эмодзи для символов (🪜 лестница, 🌊 вода/океан, 🔑 ключ, 🚪 дверь, ✨ свет, 🔥 огонь и т.д.)\n"
            "  6) Добавляй 'разукрашивание' — опиши ощущения, движение, атмосферу ('медленно падая', 'погружаясь в глубину', 'тепло воды')\n"
            "  7) Заверши итоговым выводом, объединяющим все элементы (например: 'Вместе получается: сон про отпускание контроля и погружение в свои эмоции 🤍')\n"
            "\nПРИМЕР ПРАВИЛЬНОГО ФОРМАТА РАЗБОРА:\n"
            "Падение на лестнице 🪜 — ощущение потери контроля или страха «сделать шаг не туда». Но не как наказание, а как переход: лестница — путь, а падение — момент, когда нужно отпустить старое.\n"
            "\nОказаться в океане 🌊 — сильный символ эмоций, подсознания, внутреннего мира. Вода здесь — очищение, новые ощущения, свобода. Ты буквально погружаешься в свои чувства, смываешь старые переживания.\n"
            "\nВместе получается: сон про отпускание контроля и погружение в свои эмоции, про переход к чему-то новому, но глубоко личному 🤍\n"
            "ESOTERIC: 1–2 абзаца, только если сон действительно символический/мистический (иначе оставь пусто)\n"
            "ADVICE: 2–3 строки практичного тёплого совета, основанного на смысле сна\n"
        )
    elif lang == "uk":
        example = (
            "Формат ВІДПОВІДІ СТРОГО ТАКИЙ:\n"
            "Аналіз сну 🌙\n"
            "Емоції: назви ключові емоції і 1–2 доречні емодзі\n"
            "PSYCH: 8–15 речень ЖИВОГО аналізу з ДЕТАЛЬНИМ РОЗБОРОМ КОЖНОГО ЕЛЕМЕНТА. ОБОВ'ЯЗКОВО:\n"
            "  1) Розбери КОЖНИЙ ключовий елемент сну окремо (сходи, падіння, океан, ключі, двері тощо)\n"
            "  2) Поясни що КОНКРЕТНО означає кожен елемент психологічно (наприклад: 'Сходи 🪜 — це шлях, розвиток, перехід між етапами. Падіння — не покарання, а момент відпускання контролю')\n"
            "  3) Покажи зв'язок між елементами — як вони працюють разом, що виходить в результаті\n"
            "  4) Розкрий СМИСЛ всього сну — що він означає в реальному житті, які внутрішні процеси відображає\n"
            "  5) Використовуй емодзі для символів (🪜 сходи, 🌊 вода/океан, 🔑 ключ, 🚪 двері, ✨ світло, 🔥 вогонь тощо)\n"
            "  6) Додавай 'розфарбовування' — опиши відчуття, рух, атмосферу ('повільно падаючи', 'занурюючись в глибину', 'тепло води')\n"
            "  7) Заверши підсумковим висновком, що об'єднує всі елементи (наприклад: 'Разом виходить: сон про відпускання контролю і занурення в свої емоції')\n"
            "ESOTERIC: 1–2 абзаци, лише якщо сон дійсно символічний/містичний (інакше порожньо)\n"
            "ADVICE: 2–3 рядки практичної поради, заснованої на сенсі сну\n"
        )
    else:
        example = (
            "RESPONSE FORMAT STRICTLY:\n"
            "Dream Analysis 🌙\n"
            "Emotions: list key emotions and 1–2 fitting emojis\n"
            "PSYCH: 8–15 sentences of LIVING analysis with DETAILED BREAKDOWN OF EACH ELEMENT. MUST:\n"
            "  1) Break down EACH key element of the dream separately (stairs, falling, ocean, keys, doors, etc.)\n"
            "  2) Explain what SPECIFICALLY each element means psychologically (e.g.: 'Stairs 🪜 — this is a path, development, transition between stages. Falling — not punishment, but a moment of letting go of control')\n"
            "  3) Show the connection between elements — how they work together, what emerges as a result\n"
            "  4) Reveal the MEANING of the whole dream — what it means in real life, what inner processes it reflects\n"
            "  5) Use emojis for symbols (🪜 stairs, 🌊 water/ocean, 🔑 key, 🚪 door, ✨ light, 🔥 fire, etc.)\n"
            "  6) Add 'coloring' — describe sensations, movement, atmosphere ('slowly falling', 'plunging into depth', 'warm water')\n"
            "  7) End with a summary conclusion that unites all elements (e.g.: 'Together it becomes: a dream about letting go of control and diving into one's emotions')\n"
            "ESOTERIC: 1–2 paragraphs only if the dream is truly symbolic/mystical (else empty)\n"
            "ADVICE: 2–3 practical lines based on the dream's meaning\n"
        )
    scaling_ru = (
        "Правила масштаба: Если сон бытовой/социальный — пиши кратко, ясно, но ВСЕ РАВНО раскрывай смысл и связь с реальностью. Без эзотерики, 1–2 мягких емодзи максимум. "
        "Если сон символический — пиши плавно, образно, вплітай символы в текст, РАСКРЫВАЙ их значение глубоко. "
        "Всегда опирайся на поля структуры: location, characters(name), actions, symbols, emotions, themes, summary. "
        "Никогда не используй шаблонные заготовки: формулировки должны быть уникальны и конкретны по содержанию сна. "
        "В PSYCH ОБЯЗАТЕЛЬНО объясни: что этот сон может означать в реальной жизни, какие внутренние процессы он отражает, какие послания несёт. Создай целый мир из сна, сделай интересно читать. "
        "ESOTERIC включай только если уместно; для простых снов оставь коротко или пусто."
    )
    scaling_uk = (
        "Правила масштабу: Якщо сон побутовий/соціальний — пиши коротко, ясно, але ВСЕ ОДНО розкривай сенс і зв'язок з реальністю. Без езотерики, 1–2 мʼякі емодзі максимум. "
        "Якщо сон символічний — пиши плавно, образно, вплітай символи у текст, РОЗКРИВАЙ їх значення глибоко. "
        "Завжди спирайся на поля структури: location, characters(name), actions, symbols, emotions, themes, summary. "
        "Ніколи не використовуй шаблонні заготовки: формулювання мають бути унікальні та конкретні до сну. "
        "В PSYCH ОБОВ'ЯЗКОВО поясни: що цей сон може означати в реальному житті, які внутрішні процеси він відображає, які послання несе. Створи цілий світ зі сну, зроби цікаво читати. "
        "ESOTERIC додавай лише якщо доречно; для простих снів — коротко або порожньо."
    )
    scaling_en = (
        "Scaling rules: If the dream is domestic/social — write briefly and clearly, but STILL uncover meaning and connection to reality. No esoterics, at most 1–2 gentle emojis. "
        "If symbolic — write softly and evocatively, weave symbols into prose, DEEPLY REVEAL their meaning. "
        "Always ground in structure fields: location, characters(name), actions, symbols, emotions, themes, summary. "
        "Never use boilerplate: wording must be unique and specific to this dream. "
        "In PSYCH MUST explain: what this dream might mean in real life, what inner processes it reflects, what messages it carries. Create a whole world from the dream, make it interesting to read. "
        "Include ESOTERIC only when appropriate; for simple dreams keep it short or empty."
    )
    scaling = scaling_ru if lang == "ru" else scaling_uk if lang == "uk" else scaling_en
    avoid_ru = ("Избегай штампов, если их не было в сне: 'дверь уже открывается', 'ключ в руке', '1–2 тихих шага', 'между мирами'. ")
    avoid_uk = ("Уникай штампів, якщо їх не було у сні: 'двері вже відчиняються', 'ключ у руці', '1–2 тихі кроки', 'між світами'. ")
    avoid_en = ("Avoid boilerplate if not present in the dream: 'the door opens within', 'key in hand', '1–2 quiet steps', 'between worlds'. ")
    avoid = avoid_ru if lang == "ru" else avoid_uk if lang == "uk" else avoid_en
    # Explicit rubric to avoid templates and enforce dynamic use of dream details
    if lang == "ru":
        rubric = (
            "\nКРИТИЧЕСКИ ВАЖНЫЕ ПРАВИЛА ГЕНЕРАЦИИ:\n"
            "1) СНАЧАЛА АВТОМАТИЧЕСКИ КЛАССИФИЦИРУЙ СОН по его признакам:\n"
            "   • Бытовой — реальные действия, знакомые места, обычные ситуации (прогулка, встреча, покупки)\n"
            "   • Романтический — отношения, близость, чувства, привязанность\n"
            "   • Символический/странный — необычные объекты, фантастические места, магические события, метафоры\n"
            "   • Тревожный — страх, тревога, опасность, преследования, падения\n"
            "   • Конфликтный — ссоры, борьба, недопонимания, напряжение\n"
            "   • Смешанный — комбинация нескольких типов\n"
            "2) АВТОМАТИЧЕСКИ ВЫДЕЛИ ключевые элементы из текста сна (даже необычные) и НАЙДИ их значения в СПРАВОЧНИКЕ СИМВОЛИКИ выше:\n"
            "   • Действия: падение, бегство, открытие, наблюдение, крик, танец, плавание — найди значение в справочнике\n"
            "   • Объекты: ключи, двери, лестницы, часы — найди значение в справочнике (лестницы = восхождение, развитие)\n"
            "   • Места: города, природа, океан, море — найди значение в справочнике (океан = эмоции, подсознание)\n"
            "   • Персонажи: знакомые, незнакомцы, животные — найди значение в справочнике\n"
            "   • Эмоции: по словам и описанию действий (радость, страх, тревога, любовь, удивление)\n"
            "   • Символы: вода, свет, тьма, огонь, растения — ИСПОЛЬЗУЙ конкретные значения из справочника символики!\n"
            "3) ГЛАВНОЕ — ДЕТАЛЬНЫЙ РАЗБОР ЭЛЕМЕНТОВ: В PSYCH ОБЯЗАТЕЛЬНО:\n"
            "   - Разбери КАЖДЫЙ элемент сна отдельно, используя ЗНАЧЕНИЯ ИЗ СПРАВОЧНИКА СИМВОЛИКИ (лестница = восхождение/развитие из справочника, падение = страх неудачи/чувство неполноценности из справочника, океан = эмоции/подсознание из справочника)\n"
            "   - Используй формат РАЗВЕРНУТО, как в примере: 'Падение на лестнице 🪜 — ощущение потери контроля или страха «сделать шаг не туда». Но не как наказание, а как переход: лестница — путь, а падение — момент, когда нужно отпустить старое.'\n"
            "   - Покажи связь между элементами — как они работают вместе\n"
            "   - Раскрой что этот сон означает в реальной жизни человека\n"
            "   - Какие внутренние процессы, переживания, страхи или надежды он отражает?\n"
            "   - Заверши итоговым выводом, объединяющим все элементы ('Вместе получается: сон про отпускание контроля и погружение в свои эмоции')\n"
            "   Создай из сна целый мир, сделай интересно и глубоко. НЕ просто упоминай элементы, а РАСКРЫВАЙ каждый отдельно и показывай их связь.\n"
            "4) ПИШИ в подходящем стиле с 'разукрашиванием':\n"
            "   • Для бытовых/романтических — кратко, тепло, но с раскрытием смысла\n"
            "   • Для символических/странных — образно, мягко, глубоко, вплетая символы, добавляй описания ощущений, звуков, цветов, движения\n"
            "   • Для тревожных/конфликтных — сочувственно и практично, опиши эмоциональную атмосферу\n"
            "   • Добавляй чувственные детали: блеск, мерцание, тени, звуки (шёпот, музыка, шаги), ощущения (тепло, холод, влажность)\n"
            "   • Используй 1–2 эмодзи по смыслу (🌊 вода/эмоции, 🔥 энергия, ✨ магия, 🌙 тайна, 💫 движение, 🗝️ решение)\n"
            "5) Используй только реальные детали сна из структуры. Не вставляй символы/метафоры, если их не было.\n"
            "6) КРИТИЧЕСКИ ВАЖНО — ИСПОЛЬЗУЙ СПРАВОЧНИК СИМВОЛИКИ: В header выше есть полный справочник символики снов. "
            "ОБЯЗАТЕЛЬНО используй конкретные значения из справочника для каждого элемента сна:\n"
            "   - Лестницы = восхождение, развитие, достижение (из справочника)\n"
            "   - Падение = страх неудачи, чувство неполноценности, страх потерять привязанность (из справочника)\n"
            "   - Вода/океан = эмоции, чувства, бессознательное, очищение (из справочника)\n"
            "   - И т.д. для всех элементов сна\n"
            "   НЕ придумывай значения, а ИСПОЛЬЗУЙ из справочника! Но интерпретируй их в контексте конкретного сна, "
            "показывай как они работают вместе. Добавляй 'разукрашивание' (описания ощущений, звуков, движения).\n"
            "7) Для бытовых: даже для простых снов раскрывай скрытый смысл — что это говорит о человеке, его переживаниях, отношениях, внутреннем состоянии. "
            "Добавляй описание атмосферы и эмоционального фона.\n"
            "8) НИКОГДА не используй одинаковые формулировки. Каждый ответ уникален и конкретен, с упоминанием минимум 3–4 деталей из структуры (объект/действие/эмоция/место/персонаж).\n"
            "9) Не цитируй и не пересказывай дословно текст сна; перескажи смысл своими словами и РАСКРЫВАЙ его значение.\n"
            "10) Делай анализ ЖИВЫМ и ИНТЕРЕСНЫМ для чтения — используй образный язык, создавай целостную картину, показывай связи между элементами сна и реальной жизнью. "
            "'Разукрашивай' описанием ощущений, звуков, цветов, движения, атмосферы. Пусть читатель почувствует сон.\n"
        )
    elif lang == "uk":
        rubric = (
            "\nКРИТИЧНО ВАЖЛИВІ ПРАВИЛА ГЕНЕРАЦІЇ:\n"
            "1) Спочатку класифікуй сон: Побутовий | Романтичний | Символічний/дивний | Тривожний | Конфліктний | Змішаний.\n"
            "2) Виділи ключові елементи: дії, обʼєкти, місця, персонажі, емоції, символи.\n"
            "3) ГОЛОВНЕ — ДЕТАЛЬНИЙ РОЗБІР ЕЛЕМЕНТІВ: В PSYCH ОБОВ'ЯЗКОВО:\n"
            "   - Розбери КОЖНИЙ елемент сну окремо з поясненням його психологічного сенсу\n"
            "   - Використовуй формат РОЗГОРНУТО, як у прикладі: 'Падіння на сходах 🪜 — відчуття втрати контролю або страху «зробити крок не туди». Але не як покарання, а як перехід: сходи — це шлях, а падіння — момент, коли потрібно відпустити старе.'\n"
            "   - Покажи зв'язок між елементами — як вони працюють разом\n"
            "   - Розкрий що цей сон означає в реальному житті людини\n"
            "   - Які внутрішні процеси, переживання, страхи або надії він відображає?\n"
            "   - Заверши підсумковим висновком, що об'єднує всі елементи\n"
            "   Створи зі сну цілий світ, зроби цікаво і глибоко. НЕ просто згадуй елементи, а РОЗКРИВАЙ кожен окремо і показуй їх зв'язок.\n"
            "4) ПИШИ у відповідному стилі з 'розфарбовуванням': додавай описи відчуттів, звуків, кольорів, руху, атмосфери. 1–2 емодзі за змістом.\n"
            "5) Використовуй лише реальні деталі сну зі структури. Не вставляй символи/метафори, якщо їх не було.\n"
            "6) КРИТИЧНО ВАЖЛИВО — ВИКОРИСТОВУЙ ДОВІДНИК СИМВОЛИКИ: В header вище є повний довідник символіки снів. "
            "ОБОВ'ЯЗКОВО використовуй конкретні значення з довідника для кожного елемента сну:\n"
            "   - Сходи = зростання, розвиток (з довідника)\n"
            "   - Падіння = страх невдачі, відчуття неповноцінності (з довідника)\n"
            "   - Вода/океан = емоції, почуття, підсвідомість (з довідника)\n"
            "   - І т.д. для всіх елементів сну\n"
            "   НЕ вигадуй значення, а ВИКОРИСТОВУЙ з довідника! Але інтерпретуй їх у контексті конкретного сну, показуй як вони працюють разом.\n"
            "7) Для побутових: навіть для простих снів розкривай прихований сенс — що це говорить про людину, її переживання, стосунки, внутрішній стан.\n"
            "8) НІКОЛИ не використовуй однакові формулювання. Кожна відповідь унікальна й конкретна, з мінімум 3–4 деталями зі структури (обʼєкт/дія/емоція/місце/персонаж).\n"
            "9) Не цитуй і не переказуй дослівно сон; передай сенс своїми словами і РОЗКРИВАЙ його значення.\n"
            "10) Роби аналіз ЖИВИМ і ЦІКАВИМ для читання — використовуй образну мову, створюй цілісну картину, показуй зв'язки між елементами сну і реальним життям. "
            "'Розфарбовуй' описом відчуттів, звуків, кольорів, руху, атмосфери. Нехай читач відчує сон.\n"
        )
    else:
        rubric = (
            "\nCRITICALLY IMPORTANT GENERATION RULES:\n"
            "1) First classify: Domestic | Romantic | Symbolic/Weird | Anxious | Conflict | Mixed.\n"
            "2) Extract key elements: actions, objects, places, characters, emotions, symbols.\n"
            "3) MAIN — MEANING REVELATION: In PSYCH MUST explain:\n"
            "   - What might this dream mean in the person's real life?\n"
            "   - What inner processes, experiences, fears or hopes does it reflect?\n"
            "   - How are symbols/actions/places/characters connected to the person's life?\n"
            "   - What hidden messages does the dream carry?\n"
            "   - What does the dream want to tell the person about their state, relationships, choices?\n"
            "   Create a whole world from the dream, make it interesting and deep. Don't just describe, REVEAL the meaning.\n"
            "4) MATCH the style with 'coloring': add descriptions of sensations, sounds, colors, movement, atmosphere. Use 1–2 emojis by meaning.\n"
            "5) Use only real dream details from structure. Don't add symbols/metaphors that weren't there.\n"
            "6) For symbolic: weave symbols into prose, but MUST reveal their meaning and connection to real life. Don't just list, explain the meaning.\n"
            "7) For domestic: even for simple dreams, reveal hidden meaning — what does it say about the person, their experiences, relationships, inner state.\n"
            "8) NEVER reuse the same wording. Each answer is unique and mentions at least 3–4 details from structure (object/action/emotion/place/character).\n"
            "9) Do not quote or restate the dream verbatim; paraphrase in your own words and REVEAL its meaning.\n"
            "10) Make analysis LIVING and INTERESTING to read — use figurative language, create a holistic picture, show connections between dream elements and real life. "
            "'Color' with descriptions of sensations, sounds, colors, movement, atmosphere. Let the reader feel the dream.\n"
        )
    # Extract dream text from structure
    dream_text_snippet = ""
    try:
        struct_data = json.loads(struct_json)
        dream_text_snippet = struct_data.get("_original_text", "") or struct_data.get("summary", "")
        dream_text_snippet = dream_text_snippet[:400] if dream_text_snippet else ""
    except:
        pass
    
    dream_text_label = (
        "Исходный текст сна:" if lang == "ru" else
        "Вихідний текст сну:" if lang == "uk" else
        "Original dream text:"
    )
    
    # Add explicit reminder to use symbolism reference
    symbolism_reminder = (
        "\n⚠️ КРИТИЧНО ВАЖНО: В header выше есть полный СПРАВОЧНИК СИМВОЛИКИ СНОВ (стихии, природа, люди, предметы, тело, действия, цвета, места, транспорт, животные, числа). "
        "ОБЯЗАТЕЛЬНО используй ЗНАЧЕНИЯ ИЗ ЭТОГО СПРАВОЧНИКА для каждого элемента сна! НЕ придумывай значения сам, а ИЩИ их в справочнике и используй!"
        if lang == "ru" else
        "\n⚠️ КРИТИЧНО ВАЖЛИВО: В header вище є повний ДОВІДНИК СИМВОЛИКИ СНІВ (стихії, природа, люди, предмети, тіло, дії, кольори, місця, транспорт, тварини, числа). "
        "ОБОВ'ЯЗКОВО використовуй ЗНАЧЕННЯ З ЦЬОГО ДОВІДНИКА для кожного елемента сну! НЕ вигадуй значення сам, а ШУКАЙ їх у довіднику і використовуй!"
        if lang == "uk" else
        "\n⚠️ CRITICALLY IMPORTANT: In the header above there is a complete DREAM SYMBOLISM REFERENCE (elements, nature, people, objects, body, actions, colors, places, transport, animals, numbers). "
        "MUST use MEANINGS FROM THIS REFERENCE for each dream element! DO NOT invent meanings yourself, but LOOK them up in the reference and use them!"
    )
    
    return (
        f"{header}\n\n{base}{symbolism_reminder}\n"
        f"Mode: {mode}.\n"
        f"{dream_text_label} {dream_text_snippet}\n\n"
        f"Structure (JSON): {struct_json}\n"
        f"{example}"
        f"{scaling}{avoid}"
        f"{rubric}"
        + (" Всегда включай все три секции (PSYCH, ESOTERIC — при уместности, ADVICE)." if lang == "ru" else (
           " Завжди включай усі три секції (PSYCH, ESOTERIC — за доречністю, ADVICE)." if lang == "uk" else
           " Always include the three sections (PSYCH, ESOTERIC — when appropriate, ADVICE)."
        ))
    )


def quick_heuristics(text: str, lang: str) -> Dict[str, Any]:
    t = (text or "").lower()
    symbols: List[str] = []
    for k in [
        "город","городе","city","дом","окно","вода","ключ","дерево","часы","свет","тень","музыка","дорога","небо"
    ]:
        if k in t and k not in symbols:
            symbols.append(k)
    themes: List[str] = []
    if any(w in t for w in ["переход","рассвет","проснулась","проснулся","нов","дверь","key","transition","transform"]):
        themes.append("transition")
    if any(w in t for w in ["вода","water","волна"]):
        themes.append("flow/emotion")
    if any(w in t for w in ["часы","время","без стрелок","time"]):
        themes.append("timelessness")
    emotions: List[Dict[str, Any]] = []
    # Let AI determine emotions from structure, just keep basic heuristics as fallback
    if any(w in t for w in ["страх","тревога","боязнь","fear","anx"]):
        emotions.append({"label": "anxiety", "score": 0.7})
    if any(w in t for w in ["спокой","мягк","calm","тихо","gentle"]):
        emotions.append({"label": "calm", "score": 0.7})
    summary = (text or "").strip()[:200]
    return {"symbols": symbols, "themes": themes, "emotions": emotions, "summary": summary}


def classify_dream(text: str, js: Dict[str, Any]) -> str:
    """Very light classifier for dream depth.
    Returns 'domestic' (simple/social) or 'symbolic'."""
    t = (text or "").lower()
    # Heuristics pointing to symbolic/surreal content
    surreal_keys = [
        "туман","fog","ключ","key","лестниц","stair","часы","clock","без стрелок","прозрачн","transparent",
        "свет","light","эхо","echo","зов","archetype","мист","esoter","маг",
        # частые символические триггеры
        "пада", "fall", "высот", "лес", "forest", "зеркал", "mirror", "дорог", "длинн", "туннел", "океан", "море",
        "летел", "летала", "погоня", "гонятся", "teeth", "зубы"
    ]
    if any(k in t for k in surreal_keys):
        return "symbolic"
    # If very short and mentions person-like names or simple social action
    simple_actions = ["гулял","гуляла","держались за руку","за ручку","walked","held hands","встретил","встретила"]
    if len(t) < 220 and any(a in t for a in simple_actions):
        return "domestic"
    # Symbols count from structure
    if len(js.get("symbols") or []) <= 1 and len(t) < 300:
        return "domestic"
    return "symbolic"


def validate_ai_output(text: str, js: Dict[str, Any], psych: str, esoteric: str, advice: str) -> Tuple[bool, str]:
    """Ensure the AI mentions at least two concrete dream details and avoids boilerplate not in text.
    Returns (ok, message)."""
    t = (text or "").lower()
    combined = " ".join([psych or "", esoteric or "", advice or ""]).lower()
    
    # Check for generic/template responses
    generic_patterns = [
        "символический сон про внутреннее движение и чувство пути",
        "символічний сон про внутрішній рух і відчуття шляху",
        "symbolic dream about inner movement and a sense of path",
        "внутреннее движение и чувство пути",
        "внутрішній рух і відчуття шляху",
        "inner movement and a sense of path",
        "про внутреннее движение",
        "про внутрішній рух",
        "about inner movement"
    ]
    for pattern in generic_patterns:
        if pattern in combined.lower():
            return False, "Ответ слишком общий и шаблонный. Раскрой конкретный смысл сна, используя детали из структуры."
    
    # Check minimum length for meaningful analysis
    psych_lower = (psych or "").lower().strip()
    if len(psych_lower) < 100:  # Too short for meaningful analysis
        return False, "Анализ слишком короткий. Раскрой смысл сна подробнее, минимум 5–7 предложений с конкретными деталями."
    
    # collect details
    details: List[str] = []
    for s in (js.get("symbols") or []):
        if isinstance(s, str) and s:
            details.append(s.lower())
    for a in (js.get("actions") or []):
        if isinstance(a, str) and a:
            details.append(a.lower())
    for c in (js.get("characters") or []):
        if isinstance(c, dict):
            n = (c.get("name") or "").lower()
            if n:
                details.append(n)
    for e in (js.get("emotions") or []):
        lbl = (e.get("label") or "").lower()
        if lbl:
            details.append(lbl)
    location = (js.get("location") or "").lower()
    if location:
        details.append(location)
    
    # count matches - need at least 2-3 details from dream
    ref = sum(1 for d in set(details) if d and d in combined)
    if ref < 2:
        detail_list = ", ".join(list(set(details))[:5])
        return False, f"Недостаточно конкретики — упомяни минимум две детали из сна. Доступные детали: {detail_list}. Используй их для раскрытия смысла."
    forbidden = [
        "дверь уже открывается", "ключ в руке", "1–2 тихих шага", "the door opens within"
    ]
    for f in forbidden:
        if f in combined and f not in t:
            return False, f"Убери штамп '{f}' — его не было в описании сна."
    # avoid echoing summary verbatim
    summary = (js.get("summary") or "").strip()
    if len(summary) >= 24 and summary.lower()[:24] in combined:
        return False, "Не пересказывай сон дословно — переформулируй своими словами, используя детали."
    
    # Check if analysis explains meaning (key words that indicate meaning explanation)
    meaning_indicators = [
        "означает", "может означать", "отражает", "связан", "показывает", "говорит",
        "означає", "може означати", "відображає", "пов'язаний", "показує", "говорить",
        "means", "might mean", "reflects", "connected", "shows", "tells"
    ]
    has_meaning = any(indicator in psych_lower for indicator in meaning_indicators)
    if not has_meaning and len(psych_lower) > 50:
        return False, "В анализе не раскрыт СМЫСЛ сна. Обязательно объясни: что этот сон может означать в реальной жизни, какие внутренние процессы он отражает."
    
    return True, "ok"


def build_tarot_reference(lang: str) -> str:
    """Справочник значений карт Таро для интерпретации раскладов"""
    if lang == "ru":
        return (
            "\n\n📚 СПРАВОЧНИК ЗНАЧЕНИЙ КАРТ ТАРО (используй для точной интерпретации):\n\n"
            "СТАРШИЕ АРКАНЫ:\n"
            "0 Шут — глупость, мания, сумасбродство; небрежность, апатия (обратное)\n"
            "I Маг — мастерство, дипломатия, сила воли; целитель, душевная болезнь (обратное)\n"
            "II Верховная Жрица — секреты, тайны, мудрость; страсть, поверхностное знание (обратное)\n"
            "III Императрица — плодотворность, действие, инициатива; свет, истина (обратное)\n"
            "IV Император — стабильность, власть, покровительство; благотворительность, незрелость (обратное)\n"
            "V Верховный жрец — брачный союз, вдохновение; общество, соглашение (обратное)\n"
            "VI Влюблённые — привлекательность, любовь, красота; недостаток (обратное)\n"
            "VII Колесница — помощь, провидение, триумф; бунт, поражение (обратное)\n"
            "VIII Сила — власть, энергичность, храбрость; деспотизм, слабость (обратное)\n"
            "IX Отшельник — расчетливость; предательство, притворство (обратное)\n"
            "X Колесо Фортуны — удел, судьба, удача, счастье; рост, изобилие (обратное)\n"
            "XI Справедливость — беспристрастность, честность; фанатизм, предвзятость (обратное)\n"
            "XII Повешенный — мудрость, жертва, интуиция; эгоизм (обратное)\n"
            "XIII Смерть — конец, уничтожение; инертность, сонливость (обратное)\n"
            "XIV Умеренность — экономия, умеренность, компромисс; разделение (обратное)\n"
            "XV Дьявол — опустошение, насилие, страстность; фатальное зло (обратное)\n"
            "XVI Башня — страдание, горе, бедствие; притеснение, заключение (обратное)\n"
            "XVII Звезда — утрата, кража; надежда (обратное)\n"
            "XVIII Луна — тайные враги, опасность, обман; нестабильность (обратное)\n"
            "XIX Солнце — материальное благополучие, удачный брак; то же в меньшей степени (обратное)\n"
            "XX Суд — изменение положения, возрождение; слабость, осуждение (обратное)\n"
            "XXI Мир — гарантированная удача, путешествие; инертность, застой (обратное)\n\n"
            "МЛАДШИЕ АРКАНЫ (кратко):\n"
            "ЖЕЗЛЫ — творчество, энергия, действие, инициатива\n"
            "КУБКИ — эмоции, любовь, чувства, отношения\n"
            "МЕЧИ — конфликты, решения, размышления, борьба\n"
            "ПЕНТАКЛИ — материальное, деньги, работа, практичность\n\n"
            "💡 ОБЯЗАТЕЛЬНО: используй конкретные значения карт из справочника, но интерпретируй их в контексте темы расклада. "
            "Связывай карты между собой, показывай как они работают вместе. Пиши образно, интересно, но точно."
        )
    elif lang == "uk":
        return (
            "\n\n📚 ДОВІДНИК ЗНАЧЕНЬ КАРТ ТАРО (використовуй для точної інтерпретації):\n\n"
            "СТАРШІ АРКАНИ:\n"
            "0 Шут — дурість, манія; недбалість, апатія (зворотне)\n"
            "I Маг — майстерність, дипломатія, сила волі; цілитель (зворотне)\n"
            "II Верховна Жриця — секрети, таємниці, мудрість; пристрасть (зворотне)\n"
            "III Імператриця — плідність, дія; світло, істина (зворотне)\n"
            "IV Імператор — стабільність, влада; благодійність (зворотне)\n"
            "V Верховний жрець — шлюбний союз, натхнення; суспільство (зворотне)\n"
            "VI Закохані — привабливість, любов; нестача (зворотне)\n"
            "VII Колісниця — допомога, тріумф; бунт (зворотне)\n"
            "VIII Сила — влада, енергійність; деспотизм (зворотне)\n"
            "IX Відлюдник — розсудливість; зрада (зворотне)\n"
            "X Колесо Фортуни — доля, удача; зростання (зворотне)\n"
            "XI Справедливість — безсторонність; фанатизм (зворотне)\n"
            "XII Повішений — мудрість, жертва; егоїзм (зворотне)\n"
            "XIII Смерть — кінець; інертність (зворотне)\n"
            "XIV Помірність — економія, компроміс; розділення (зворотне)\n"
            "XV Диявол — спустошення, насильство; фатальне зло (зворотне)\n"
            "XVI Вежа — страждання, лихо; пригнічення (зворотне)\n"
            "XVII Зірка — втрата; надія (зворотне)\n"
            "XVIII Місяць — таємні вороги, обман; нестабільність (зворотне)\n"
            "XIX Сонце — матеріальне благополуччя; те ж у меншій мірі (зворотне)\n"
            "XX Суд — зміна, відродження; слабкість (зворотне)\n"
            "XXI Світ — удача, подорож; інертність (зворотне)\n\n"
            "МОЛОДШІ АРКАНИ: ЖЕЗЛИ (творчість), КУБКИ (емоції), МЕЧІ (конфлікти), ПЕНТАКЛІ (матеріальне)\n\n"
            "💡 ОБОВ'ЯЗКОВО: використовуй конкретні значення карт, але інтерпретуй їх у контексті теми. "
            "Зв'язуй карти між собою, показуй як вони працюють разом. Пиши образно, цікаво, але точно."
        )
    else:
        return (
            "\n\n📚 TAROT CARD MEANINGS REFERENCE (use for accurate interpretation):\n\n"
            "MAJOR ARCANA:\n"
            "0 Fool — folly, mania; carelessness (reversed)\n"
            "I Magician — skill, diplomacy, willpower; healer (reversed)\n"
            "II High Priestess — secrets, wisdom; passion (reversed)\n"
            "III Empress — fruitfulness, action; light, truth (reversed)\n"
            "IV Emperor — stability, power; charity (reversed)\n"
            "V Hierophant — union, inspiration; society (reversed)\n"
            "VI Lovers — attraction, love; lack (reversed)\n"
            "VII Chariot — help, triumph; rebellion (reversed)\n"
            "VIII Strength — power, energy; despotism (reversed)\n"
            "IX Hermit — prudence; betrayal (reversed)\n"
            "X Wheel of Fortune — fate, luck; growth (reversed)\n"
            "XI Justice — impartiality; fanaticism (reversed)\n"
            "XII Hanged Man — wisdom, sacrifice; selfishness (reversed)\n"
            "XIII Death — end; inertia (reversed)\n"
            "XIV Temperance — economy, compromise; division (reversed)\n"
            "XV Devil — devastation, violence; fatal evil (reversed)\n"
            "XVI Tower — suffering, disaster; oppression (reversed)\n"
            "XVII Star — loss; hope (reversed)\n"
            "XVIII Moon — secret enemies, deception; instability (reversed)\n"
            "XIX Sun — material prosperity; same to lesser degree (reversed)\n"
            "XX Judgment — change, rebirth; weakness (reversed)\n"
            "XXI World — guaranteed luck, journey; inertia (reversed)\n\n"
            "MINOR ARCANA: WANDS (creativity), CUPS (emotions), SWORDS (conflicts), PENTACLES (material)\n\n"
            "💡 MUST: use specific card meanings but interpret them in context of the spread topic. "
            "Connect cards together, show how they work together. Write vividly, interestingly, but accurately."
        )


def build_tarot_prompt(spread: int, topic: str, lang: str, by_dream: bool = False) -> str:
    header = build_style_header(lang)
    tarot_ref = build_tarot_reference(lang)
    names_uk = {1: "1 карта (порада)", 3: "3 карти (минуле/теперішнє/майбутнє)", 5: "5 карт (глибокий аналіз)"}
    names_ru = {1: "1 карта (совет)", 3: "3 карты (прошлое/настоящее/будущее)", 5: "5 карт (глубокий анализ)"}
    names_en = {1: "1 card (advice)", 3: "3 cards (past/present/future)", 5: "5 cards (deep analysis)"}
    name = (names_uk if lang == "uk" else names_ru if lang == "ru" else names_en).get(max(1, min(5, spread)), names_en[3])
    if lang == "uk":
        base = (
            f"ЗАДАЧА: Створи розклад Таро: {name}. Тема: {topic}.\n\n"
            "КРИТИЧНО ВАЖЛИВО:\n"
            "1) ВИБЕРИ конкретні карти Таро для розкладу (назви карт: Шут, Маг, Верховна Жриця, Імператриця, "
            "Імператор, Верховний жрець, Закохані, Колісниця, Сила, Відлюдник, Колесо Фортуни, Справедливість, "
            "Повішений, Смерть, Помірність, Диявол, Вежа, Зірка, Місяць, Сонце, Суд, Світ, або карти Молодших Арканів)\n"
            "2) ВИКОРИСТОВУЙ значення карт зі справочника вище — кожна карта має конкретні значення (прямі та зворотні)\n"
            "3) ІНТЕРПРЕТУЙ карти в контексті теми розкладу — як вони стосуються питання/ситуації\n"
            "4) ПОКАЖИ зв'язок між картами — як вони працюють разом, що вони розкривають\n"
            "5) ПИШИ образно, цікаво, але точно — використовуй конкретні значення, але роби текст живим\n"
            "6) ДЛЯ 3-5 карт: розкривай кожну карту окремо, потім покажи загальну картину\n"
            + ("7) ПРИВ'ЯЖИ значення карт до символів сну, емоцій, мотивів з попереднього аналізу. " if by_dream else "")
            + "\nФОРМАТ: 3–5 абзаців. Початок — загальна картина розкладу. Потім — розбір кожної карти (для 3–5 карт). "
            "Завершення — висновок, що об'єднує всі карти та дає відповідь на тему."
        )
    elif lang == "ru":
        base = (
            f"ЗАДАЧА: Сделай расклад Таро: {name}. Тема: {topic}.\n\n"
            "КРИТИЧНО ВАЖНО:\n"
            "1) ВЫБЕРИ конкретные карты Таро для расклада (названия карт: Шут, Маг, Верховная Жрица, Императрица, "
            "Император, Верховный жрец, Влюблённые, Колесница, Сила, Отшельник, Колесо Фортуны, Справедливость, "
            "Повешенный, Смерть, Умеренность, Дьявол, Башня, Звезда, Луна, Солнце, Суд, Мир, или карты Младших Арканов)\n"
            "2) ИСПОЛЬЗУЙ значения карт из справочника выше — каждая карта имеет конкретные значения (прямые и обратные)\n"
            "3) ИНТЕРПРЕТИРУЙ карты в контексте темы расклада — как они относятся к вопросу/ситуации\n"
            "4) ПОКАЖИ связь между картами — как они работают вместе, что они раскрывают\n"
            "5) ПИШИ образно, интересно, но точно — используй конкретные значения, но делай текст живым\n"
            "6) ДЛЯ 3–5 карт: раскрывай каждую карту отдельно, затем покажи общую картину\n"
            + ("7) СВЯЖИ значения карт с символами сна, эмоциями, мотивами из предыдущего анализа. " if by_dream else "")
            + "\nФОРМАТ: 3–5 абзацев. Начало — общая картина расклада. Затем — разбор каждой карты (для 3–5 карт). "
            "Завершение — вывод, объединяющий все карты и дающий ответ на тему."
        )
    else:
        base = (
            f"TASK: Create a Tarot spread: {name}. Topic: {topic}.\n\n"
            "CRITICALLY IMPORTANT:\n"
            "1) CHOOSE specific Tarot cards for the spread (card names: Fool, Magician, High Priestess, Empress, "
            "Emperor, Hierophant, Lovers, Chariot, Strength, Hermit, Wheel of Fortune, Justice, Hanged Man, Death, "
            "Temperance, Devil, Tower, Star, Moon, Sun, Judgment, World, or Minor Arcana cards)\n"
            "2) USE card meanings from the reference above — each card has specific meanings (upright and reversed)\n"
            "3) INTERPRET cards in context of the spread topic — how they relate to the question/situation\n"
            "4) SHOW connection between cards — how they work together, what they reveal\n"
            "5) WRITE vividly, interestingly, but accurately — use specific meanings but make text alive\n"
            "6) FOR 3–5 cards: reveal each card separately, then show the overall picture\n"
            + ("7) BIND card meanings to dream symbols, emotions, motifs from previous analysis. " if by_dream else "")
            + "\nFORMAT: 3–5 paragraphs. Start — overall picture of the spread. Then — breakdown of each card (for 3–5 cards). "
            "End — conclusion that unites all cards and answers the topic."
        )
    return f"{header}{tarot_ref}\n\n{base}"


async def call_gemini(prompt: str) -> str:
    client = gemini_client()
    if not client:
        return ""
    try:
        resp = await asyncio.to_thread(
            client.models.generate_content,
            model=GEMINI_MODEL,
            contents=prompt,
            generation_config={
                "temperature": 0.85,
                "top_p": 0.9,
                "top_k": 40,
                "max_output_tokens": 2200,
            },
        )
        # Try common accessors
        text = getattr(resp, "text", None)
        if text:
            return text
        # Extract from candidates/parts
        try:
            candidates = getattr(resp, "candidates", None) or []
            parts_text: list[str] = []
            for cand in candidates:
                content = getattr(cand, "content", None)
                if not content:
                    continue
                parts = getattr(content, "parts", None) or []
                for p in parts:
                    t = getattr(p, "text", None)
                    if t:
                        parts_text.append(t)
            if parts_text:
                return "\n".join(parts_text)
        except Exception:
            pass
        return ""
    except Exception:
        return ""


async def analyze_dream(text: str, mode: str, lang: str) -> Tuple[Dict[str, Any], str, str, str]:
    struct_prompt = build_struct_prompt(text, lang)
    struct_raw = await call_gemini(struct_prompt)
    js: Dict[str, Any]
    try:
        
        m = re.search(r"\{[\s\S]*\}$", struct_raw.strip())
        js = json.loads(m.group(0) if m else struct_raw)
    except Exception:
        js = {
            "location": None,
            "characters": [],
            "actions": [],
            "symbols": [],
            "emotions": [],
            "themes": [],
            "archetypes": [],
            "summary": "",
        }

    # Fallback: если модель не дала summary, возьмем первые ~200 символов исходного текста
    try:
        if not (js.get("summary") or "").strip():
            js["summary"] = (text or "").strip()[:200]
    except Exception:
        pass

    # Heuristic backfill for empty fields
    try:
        h = quick_heuristics(text, lang)
        if not (js.get("symbols") or []):
            js["symbols"] = h.get("symbols", [])
        if not (js.get("themes") or []):
            js["themes"] = h.get("themes", [])
        if not (js.get("emotions") or []):
            js["emotions"] = h.get("emotions", [])
    except Exception:
        pass

    # Classify dream depth to scale style
    depth = classify_dream(text, js)
    # Ensure summary contains the original dream text for context
    if not (js.get("summary") or "").strip() or len((js.get("summary") or "").strip()) < 50:
        js["summary"] = (text or "").strip()[:300]
    
    # Add original dream text to structure for context
    js["_original_text"] = (text or "").strip()[:500]
    
    interp_prompt = build_interpret_prompt(json.dumps(js, ensure_ascii=False), mode, lang)
    # Add scaling guidance into prompt
    if lang == "ru":
        interp_prompt += (
            "\nГлубина сна: " + ("Бытовой/социальный" if depth == "domestic" else "Символический") + ". "
            "Если сон бытовой/социальный — пиши кратко и ясно, без эзотерики и метафор, только по сути. "
            "Используй символы только если они явно присутствуют."
        )
    elif lang == "uk":
        interp_prompt += (
            "\nГлибина сну: " + ("Побутовий/соціальний" if depth == "domestic" else "Символічний") + ". "
            "Якщо сон побутовий — пиши коротко і ясно, без езотерики і зайвих метафор. "
            "Використовуй символи лише якщо вони явно присутні."
        )
    else:
        interp_prompt += (
            "\nDepth: " + ("Domestic/Social" if depth == "domestic" else "Symbolic") + ". "
            "If the dream is domestic/social, write briefly and clearly, no esoterics, minimal metaphors. "
            "Use symbols only if explicitly present."
        )
    interp_raw = await call_gemini(interp_prompt)
    # Retry once if empty
    if not interp_raw:
        interp_raw = await call_gemini(interp_prompt)

    psych, esoteric, advice = "", "", ""
    if interp_raw:
       
        parts = re.split(r"(?im)^\s*(PSYCH|ESOTERIC|ADVICE)\s*:?\s*$", interp_raw)
       
        bucket = {}
        for i in range(1, len(parts), 2):
            key = parts[i].upper()
            val = parts[i + 1].strip() if i + 1 < len(parts) else ""
            bucket[key] = val
        psych = bucket.get("PSYCH", "")
        esoteric = bucket.get("ESOTERIC", "")
        advice = bucket.get("ADVICE", "")
        # Фолбэк: если модель не размечала секции, используем весь ответ как PSYCH
        if not psych and not esoteric and not advice:
            psych = interp_raw.strip()

    # If AI returned empty psych, reprompt once with critique
    if not psych:
        critique = (
            "Перепиши ответ: используй детали сна из структуры (location/characters/actions/symbols/emotions/themes/summary). "
            "Для бытового — кратко и ясно; для символического — образно, без сухих списков."
        ) if lang == "ru" else (
            "Перепиши відповідь: використовуй деталі сну зі структури. Побутовий — коротко; символічний — образно."
        ) if lang == "uk" else (
            "Rewrite: ground in structure details. Domestic — brief; Symbolic — evocative, no dry lists."
        )
        retry_raw = await call_gemini(interp_prompt + "\n\n" + critique)
        if retry_raw:
            parts = re.split(r"(?im)^\s*(PSYCH|ESOTERIC|ADVICE)\s*:?\s*$", retry_raw)
            bucket = {}
            for i in range(1, len(parts), 2):
                key = parts[i].upper()
                val = parts[i + 1].strip() if i + 1 < len(parts) else ""
                bucket[key] = val
            psych = bucket.get("PSYCH", psych)
            esoteric = bucket.get("ESOTERIC", esoteric)
            advice = bucket.get("ADVICE", advice)

    # Ensure non-empty sections even for short dreams
    if not psych:
        th = js.get("themes") or []
        sym = js.get("symbols") or []
        summ = (js.get("summary") or "").strip()
        if depth == "domestic":
            # Plain, clear, no mysticism — synthesize from detected hints (no verbatim echo)
            s = (text or "").lower()
            names = ", ".join([c.get("name") for c in (js.get("characters") or []) if isinstance(c, dict) and c.get("name")])
            hints: List[str] = []
            # School/late/teacher
            if any(k in s for k in ["школ", "урок", "класс", "урок", "teacher", "class"]) or any(k in s for k in ["опоздал", "опоздала", "запізнився", "запізнилась", "late"]):
                if lang == "ru":
                    hints.append("про ожидания и ответственность: хочется успевать, но без лишнего давления")
                elif lang == "uk":
                    hints.append("про очікування і відповідальність: хочеться встигати без зайвого тиску")
                else:
                    hints.append("about expectations and responsibility — wanting to keep up without extra pressure")
            # Cafe/laughter/video
            if any(k in s for k in ["кафе", "coffee", "bar", "смех", "смеял", "сміяли", "видео", "video"]):
                if lang == "ru":
                    hints.append("про лёгкость и тёплый контакт — быть рядом и разделять радость")
                elif lang == "uk":
                    hints.append("про легкість і теплий контакт — бути поряд і ділитися радістю")
                else:
                    hints.append("about lightness and warm connection — being together and sharing joy")
            # Hand-holding
            if any(k in s for k in ["за руку", "держались за руку", "held hands", "hand in hand"]):
                if lang == "ru":
                    hints.append("про близость и доверие — тяготение к простому теплу")
                elif lang == "uk":
                    hints.append("про близькість і довіру — потяг до простого тепла")
                else:
                    hints.append("about closeness and trust — a pull toward simple warmth")
            # Purchase/clothes
            if any(k in s for k in ["купил", "купила", "купить", "покуп", "примерил", "примерила", "свитер", "кофта", "одеж", "куртка", "платье"]) or any(k in s for k in ["купив", "придбав", "светр", "одяг"]):
                if lang == "ru":
                    hints.append("про обновление образа и комфорт — подобрать то, что сидит по тебе")
                elif lang == "uk":
                    hints.append("про оновлення і комфорт — підібрати те, що пасує саме тобі")
                else:
                    hints.append("about renewal and comfort — choosing what truly fits you")

            if lang == "ru":
                base = "Короткий бытовой сон" + (f" про {names}" if names else "") + ": "
                psych = base + ("; ".join(hints) if hints else "про простые чувства и заботу о себе")
            elif lang == "uk":
                base = "Короткий побутовий сон" + (f" про {names}" if names else "") + ": "
                psych = base + ("; ".join(hints) if hints else "про прості відчуття і турботу про себе")
            else:
                base = "A brief domestic dream" + (f" about {names}" if names else "") + ": "
                psych = base + ("; ".join(hints) if hints else "about simple feelings and self-care")
            esoteric = ""
            if not advice:
                if lang == "ru":
                    advice = random.choice([
                        "Прислушайся к своему комфорту и теплу — выбери самый мягкий шаг.",
                        "Назови своё чувство простыми словами и сделай маленькое действие.",
                    ])
                elif lang == "uk":
                    advice = random.choice([
                        "Прислухайся до свого комфорту — обери найлегший крок.",
                        "Назви почуття простими словами і зроби невеличку дію.",
                    ])
                else:
                    advice = random.choice([
                        "Notice what feels comfortable and warm — take the gentlest step.",
                        "Name the feeling in simple words and take a small action.",
                    ])
        else:
            # Symbolic fallback - create specific analysis based on dream details
            s = (text or "").lower()
            symbols = [str(sym) for sym in (js.get("symbols") or [])[:3]]
            actions = [str(act) for act in (js.get("actions") or [])[:3]]
            characters = [c.get("name", "") if isinstance(c, dict) else str(c) for c in (js.get("characters") or [])[:3] if c]
            location = js.get("location") or ""
            emotions_list = [e.get("label", "") for e in (js.get("emotions") or []) if isinstance(e, dict) and e.get("label")]
            
            # Build specific analysis from dream details instead of generic template
            if not psych:
                # Collect all details in simple list
                all_details = []
                if symbols:
                    all_details.extend([str(s) for s in symbols[:3]])
                if actions:
                    all_details.extend([str(a) for a in actions[:3]])
                if characters:
                    all_details.extend([str(c) for c in characters[:3] if c])
                if location:
                    all_details.append(str(location))
                
                detail_str = ", ".join(all_details[:5]) if all_details else ""
                
                if lang == "ru":
                    # Create more specific analysis - retry with stronger prompt
                    retry_prompt_specific = (
                        f"Ты — эксперт по анализу снов. Раскрой ГЛУБОКИЙ СМЫСЛ этого сна.\n\n"
                        f"ИСХОДНЫЙ ТЕКСТ СНА: {text[:400]}\n\n"
                        f"СТРУКТУРА: {json.dumps(js, ensure_ascii=False)[:500]}\n\n"
                        "ЗАДАЧА: Напиши глубокий психологический анализ (5–10 предложений). ОБЯЗАТЕЛЬНО:\n"
                        "1. Раскрой ЧТО этот сон может означать в реальной жизни человека\n"
                        "2. Какие внутренние процессы, переживания, страхи или надежды он отражает\n"
                        "3. Как символы/действия/места/персонажи связаны с жизнью человека\n"
                        "4. Какие скрытые послания несёт сон\n"
                        "5. Используй конкретные детали из сна (не просто перечисляй, а объясняй их смысл)\n\n"
                        "Пиши тепло, образно, интересно. Создай из сна целый мир. Избегай шаблонных фраз типа 'внутреннее движение' или 'чувство пути'."
                    )
                    retry_result = await call_gemini(retry_prompt_specific)
                    if retry_result and len(retry_result.strip()) > 100:
                        psych = retry_result.strip()
                    else:
                        # If AI didn't return analysis, try once more with even stronger prompt
                        if not retry_result or len(retry_result.strip()) < 100:
                            final_prompt = (
                                f"ТЫ ДОЛЖЕН ПРОАНАЛИЗИРОВАТЬ ЭТОТ СОН СЕЙЧАС.\n\n"
                                f"СОН: {text}\n\n"
                                f"СТРУКТУРА: {json.dumps(js, ensure_ascii=False)}\n\n"
                                "ЗАДАНИЕ: Напиши глубокий психологический анализ этого сна (минимум 8–12 предложений).\n\n"
                                "ОБЯЗАТЕЛЬНО РАСКРОЙ:\n"
                                "1. Что означает ПАДЕНИЕ с лестницы в психологическом смысле\n"
                                "2. Что символизирует ОКЕАН в этом контексте\n"
                                "3. Какие переживания, страхи или внутренние процессы отражает этот сон\n"
                                "4. Что сон может сказать о реальной жизни человека\n"
                                "5. Как эти символы связаны между собой и что это значит\n\n"
                                "Используй конкретные детали из сна: лестница, падение, океан. Объясни их смысл.\n"
                                "Пиши тепло, образно, интересно. Избегай общих фраз."
                            )
                            final_result = await call_gemini(final_prompt)
                            if final_result and len(final_result.strip()) > 150:
                                psych = final_result.strip()
                            else:
                                # Last resort - minimal but specific
                                if detail_str:
                                    psych = f"Символический сон про {detail_str}. Этот сон содержит важные послания о внутреннем состоянии и переживаниях. "
                                    if "пада" in (text or "").lower():
                                        psych += "Падение может символизировать потерю контроля или страх неудачи. "
                                    if "океан" in (text or "").lower() or "море" in (text or "").lower():
                                        psych += "Океан часто связан с эмоциями и бессознательным — возможно, сон указывает на глубокие переживания, которые требуют внимания."
                                else:
                                    psych = "Этот сон отражает важные внутренние процессы. Детали сна (лестница, падение, океан) несут глубокий символический смысл о состоянии человека и его переживаниях."
                elif lang == "uk":
                    retry_prompt_specific = (
                        f"Ти — експерт з аналізу снів. Розкрий ГЛИБОКИЙ СМИСЛ цього сну.\n\n"
                        f"ВИХІДНИЙ ТЕКСТ СНУ: {text[:400]}\n\n"
                        f"СТРУКТУРА: {json.dumps(js, ensure_ascii=False)[:500]}\n\n"
                        "ЗАВДАННЯ: Напиши глибокий психологічний аналіз (5–10 речень). ОБОВ'ЯЗКОВО:\n"
                        "1. Розкрий ЩО цей сон може означати в реальному житті людини\n"
                        "2. Які внутрішні процеси, переживання, страхи або надії він відображає\n"
                        "3. Як символи/дії/місця/персонажі пов'язані з життям людини\n"
                        "4. Які приховані послання несе сон\n"
                        "5. Використовуй конкретні деталі зі сну (не просто перераховуй, а пояснюй їх сенс)\n\n"
                        "Пиши тепло, образно, цікаво. Створи зі сну цілий світ. Уникай шаблонних фраз типу 'внутрішній рух' або 'відчуття шляху'."
                    )
                    retry_result = await call_gemini(retry_prompt_specific)
                    if retry_result and len(retry_result.strip()) > 100:
                        psych = retry_result.strip()
                    else:
                        # Try once more with stronger prompt
                        final_prompt = (
                            f"ТИ ПОВИНЕН ПРОАНАЛІЗУВАТИ ЦЕЙ СОН ЗАРАЗ.\n\n"
                            f"СОН: {text}\n\n"
                            f"СТРУКТУРА: {json.dumps(js, ensure_ascii=False)}\n\n"
                            "ЗАВДАННЯ: Напиши глибокий психологічний аналіз цього сну (мінімум 8–12 речень).\n\n"
                            "ОБОВ'ЯЗКОВО РОЗКРИЙ:\n"
                            "1. Що означає ПАДІННЯ зі сходів у психологічному сенсі\n"
                            "2. Що символізує ОКЕАН в цьому контексті\n"
                            "3. Які переживання, страхи або внутрішні процеси відображає цей сон\n"
                            "4. Що сон може сказати про реальне життя людини\n\n"
                            "Використовуй конкретні деталі зі сну. Поясни їх сенс. Пиши тепло, образно."
                        )
                        final_result = await call_gemini(final_prompt)
                        if final_result and len(final_result.strip()) > 150:
                            psych = final_result.strip()
                        else:
                            if detail_str:
                                psych = f"Символічний сон про {detail_str}. Цей сон містить важливі послання про внутрішній стан і переживання."
                            else:
                                psych = "Цей сон відображає важливі внутрішні процеси. Деталі сну несуть глибокий символічний сенс."
                else:
                    retry_prompt_specific = (
                        f"You are a dream analysis expert. Uncover the DEEP MEANING of this dream.\n\n"
                        f"ORIGINAL DREAM TEXT: {text[:400]}\n\n"
                        f"STRUCTURE: {json.dumps(js, ensure_ascii=False)[:500]}\n\n"
                        "TASK: Write a deep psychological analysis (5–10 sentences). MUST:\n"
                        "1. Reveal WHAT this dream might mean in the person's real life\n"
                        "2. What inner processes, experiences, fears or hopes it reflects\n"
                        "3. How symbols/actions/places/characters are connected to the person's life\n"
                        "4. What hidden messages the dream carries\n"
                        "5. Use specific details from the dream (don't just list, explain their meaning)\n\n"
                        "Write warmly, evocatively, interestingly. Create a whole world from the dream. Avoid template phrases like 'inner movement' or 'sense of path'."
                    )
                    retry_result = await call_gemini(retry_prompt_specific)
                    if retry_result and len(retry_result.strip()) > 100:
                        psych = retry_result.strip()
                    else:
                        final_prompt = (
                            f"YOU MUST ANALYZE THIS DREAM NOW.\n\n"
                            f"DREAM: {text}\n\n"
                            f"STRUCTURE: {json.dumps(js, ensure_ascii=False)}\n\n"
                            "TASK: Write a deep psychological analysis of this dream (minimum 8–12 sentences).\n\n"
                            "MUST REVEAL:\n"
                            "1. What FALLING down stairs means psychologically\n"
                            "2. What OCEAN symbolizes in this context\n"
                            "3. What experiences, fears or inner processes this dream reflects\n"
                            "4. What the dream might say about the person's real life\n\n"
                            "Use specific dream details. Explain their meaning. Write warmly, evocatively."
                        )
                        final_result = await call_gemini(final_prompt)
                        if final_result and len(final_result.strip()) > 150:
                            psych = final_result.strip()
                        else:
                            if detail_str:
                                psych = f"Symbolic dream about {detail_str}. This dream contains important messages about inner state and experiences."
                            else:
                                psych = "This dream reflects important inner processes. Dream details carry deep symbolic meaning."
            
            if not esoteric:
                esoteric = ""
            if not advice:
                # Let AI generate advice from dream details - retry with specific prompt
                # Use dream text directly, not detail_str which might be empty
                if text:
                    advice_prompt = (
                        f"СОН: {text[:400]}\n\n"
                        f"АНАЛИЗ СНА: {psych[:300] if psych else 'Символический сон'}\n\n"
                    )
                    if lang == "ru":
                        advice_prompt += "Дай практичный, конкретный совет на основе этого сна (2–3 строки). Что человек может сделать в реальной жизни прямо сейчас?"
                    elif lang == "uk":
                        advice_prompt += "Дай практичну, конкретну пораду на основі цього сну (2–3 рядки). Що людина може зробити в реальному житті зараз?"
                    else:
                        advice_prompt += "Give practical, specific advice based on this dream (2–3 lines). What can the person do in real life right now?"
                    
                    advice_result = await call_gemini(advice_prompt)
                    if advice_result and len(advice_result.strip()) > 30:
                        advice = advice_result.strip()
                    else:
                        if lang == "ru":
                            advice = "Обрати внимание на детали сна и подумай, что они могут означать в твоей жизни."
                        elif lang == "uk":
                            advice = "Зверни увагу на деталі сну і подумай, що вони можуть означати в твоєму житті."
                        else:
                            advice = "Pay attention to dream details and think about what they might mean in your life."
                else:
                    if lang == "ru":
                        advice = "Обрати внимание на детали сна — они могут указать на то, что важно для тебя сейчас."
                    elif lang == "uk":
                        advice = "Зверни увагу на деталі сну — вони можуть вказати на те, що важливо для тебе зараз."
                    else:
                        advice = "Pay attention to dream details — they might point to what's important for you now."

    # Validate AI output; if weak, reprompt once with critique
    ok, msg = validate_ai_output(text, js, psych, esoteric, advice)
    if not ok:
        critique2 = (
            "Перепиши ответ: " + msg + " Опирайся на конкретные детали из структуры." if lang == "ru" else
            "Перепиши відповідь: " + msg + " Спирайся на конкретні деталі зі структури." if lang == "uk" else
            "Rewrite: " + msg + " Ground in concrete structure details."
        )
        retry2_raw = await call_gemini(interp_prompt + "\n\n" + critique2)
        if retry2_raw:
            parts = re.split(r"(?im)^\s*(PSYCH|ESOTERIC|ADVICE)\s*:?\s*$", retry2_raw)
            bucket = {}
            for i in range(1, len(parts), 2):
                key = parts[i].upper()
                val = parts[i + 1].strip() if i + 1 < len(parts) else ""
                bucket[key] = val
            psych = bucket.get("PSYCH", psych)
            esoteric = bucket.get("ESOTERIC", esoteric)
            advice = bucket.get("ADVICE", advice)

    # Persist depth for renderer
    try:
        js["_depth"] = depth
    except Exception:
        pass
    return js, psych, esoteric, advice


def render_analysis_text(js: Dict[str, Any], psych: str, esoteric: str, advice: str, lang: str) -> str:
    def fmt_list(name: str, vals: List[Any]) -> str:
        if not vals:
            return ""
        return f"{name}: " + ", ".join(
            [v if isinstance(v, str) else (v.get("name") or v.get("label") or str(v)) for v in vals]
        )

    loc = js.get("location") or ""
    chars = fmt_list("Characters", js.get("characters") or [])
    acts = fmt_list("Actions", js.get("actions") or [])
    syms = fmt_list("Symbols", js.get("symbols") or [])
    emos = ", ".join([f"{e.get('label','')}({e.get('score',0)})" for e in (js.get("emotions") or [])])
    thms = fmt_list("Themes", js.get("themes") or [])
    arch = fmt_list("Archetypes", js.get("archetypes") or [])
    summ = js.get("summary") or ""
    syms_list = js.get("symbols") or []
    depth_flag = (js.get("_depth") == "domestic")
    is_simple = depth_flag

    if lang == "uk":
        # М'яка денникова подача: короткі рядки, вплетені образи, без сухих списків
        header = "Аналіз сну 🌙"
        # Емоції: українською, без чисел
        uk_emo_map = {"calm": "спокій", "anxiety": "тривога", "joy": "радість", "sad": "смуток"}
        emo_words: List[str] = []
        for e in (js.get("emotions") or []):
            lbl = (e.get("label") or "").lower()
            if lbl:
                emo_words.append(uk_emo_map.get(lbl, lbl))
        emo_line = ", ".join(dict.fromkeys([w for w in emo_words if w])) or "спокійна присутність"

        # Теми у короткий сенсовий заголовок
        themes_uk = {"transition": "перехід", "timelessness": "поза часом", "flow/emotion": "рух через відчуття"}
        th = [themes_uk.get(t, t) for t in (js.get("themes") or [])]
        head_core = ", ".join(dict.fromkeys([t for t in th if t])) or "внутрішній пошук"

        # Вплетені інтерпретації символів
        sym_words = [s if isinstance(s, str) else str(s) for s in (js.get("symbols") or [])]
        uk_symbol_map = {
            "зупинка": "Зупинка — пауза між етапами. Минуле поруч, але тане в тумані 🚏",
            "туман": "Туман — мʼяка невизначеність без страху",
            "карта": "Карта, що змінюється — шлях ще складається. Дивись серцем 👁️",
            "без обличчя": "Без обличчя — знайомий стан, частина тебе, вже прожите 🤍",
            "відлуння": "Імʼя з‑під землі — поклик внутрішнього голосу 🌱",
            "сходи": "Сходи вниз, як угору — заглиблюючись, ти зростаєш 🪜",
            "лист": "Лист без слів — сенс уже зрозумілий, просто не сказаний уголос 💌",
            "світло": "Світло дитинства — відчуття безпеки і твоєї суті 🌙",
            "час": "Час бере за руку — не поспішай, усе вчасно ⏳",
            "вода": "Тепла вода під ногами — рух через відчуття",
            "годинник": "Годинник без стрілок — поза звичним ритмом",
            "місто": "Прозоре місто — межі між зовнішнім і внутрішнім стираються",
            "небо": "Низьке небо — близькість переживання, зосередженість",
        }
        symbol_lines: List[str] = []
        for s in sym_words[:8]:
            k = s.lower()
            for key, line in uk_symbol_map.items():
                if key in k:
                    symbol_lines.append(line)
                    break

        parts = [
            header,
            (f"Емоції: {emo_line} 🌊" if emo_line else ""),
            (psych or ""),
            (esoteric or ""),
            (f"Порада: {advice}" if advice else ""),
        ]
    elif lang == "ru":
        # Мягкая дневниковая подача: короткие строки, вплетённые образы, без сухих списков
        header = "Анализ сна 🌙"
        # Эмоции: по‑русски, без чисел
        ru_emo_map = {"calm": "спокойствие", "anxiety": "тревога", "joy": "радость", "sad": "печаль"}
        emo_words: List[str] = []
        for e in (js.get("emotions") or []):
            lbl = (e.get("label") or "").lower()
            if lbl:
                emo_words.append(ru_emo_map.get(lbl, lbl))
        emo_line = ", ".join(dict.fromkeys([w for w in emo_words if w])) or "спокойное присутствие"

        # Темы в короткий смысл заголовка
        themes_ru = {"transition": "переход", "timelessness": "вне времени", "flow/emotion": "движение через чувство"}
        th = [themes_ru.get(t, t) for t in (js.get("themes") or [])]
        head_core = ", ".join(dict.fromkeys([t for t in th if t])) or "внутренний поиск"

        # Вплетённые интерпретации символов
        sym_words = [s if isinstance(s, str) else str(s) for s in (js.get("symbols") or [])]
        ru_symbol_map = {
            "остановка": "Остановка — пауза между этапами. Прошлое рядом, но уходит в туман 🚏",
            "туман": "Туман — мягкая неопределённость без страха",
            "карта": "Карта, что меняется — путь ещё складывается. Смотри сердцем 👁️",
            "человек без лица": "Безликий — знакомое состояние, часть тебя, уже прожитый опыт 🤍",
            "эхо": "Имя из‑под земли — зов внутреннего голоса 🌱",
            "лестница": "Лестница вниз, как вверх — углубляясь, ты растёшь 🪜",
            "письмо": "Письмо без слов — смысл уже понятен, просто не сказан вслух 💌",
            "свет": "Свет детства — чувство безопасности и настоящей тебя 🌙",
            "время": "Время берёт за руку — не спеши, всё вовремя ⏳",
            "вода": "Вода под ногами — движение через чувства",
            "часы": "Часы без стрелок — выход из привычного ритма",
            "город": "Прозрачный город — границы между внешним и внутренним стираются",
            "небо": "Низкое небо — близость переживания, сосредоточенность",
        }
        symbol_lines: List[str] = []
        for s in sym_words[:8]:
            k = s.lower()
            for key, line in ru_symbol_map.items():
                if key in k:
                    symbol_lines.append(line)
                    break

        parts = [
            header,
            (f"Эмоции: {emo_line} 🌊" if emo_line else ""),
            (psych or ""),
            (esoteric or ""),
            (f"Совет: {advice}" if advice else ""),
        ]
    else:
        # Soft, diary-like English rendering
        header = "Dream Analysis 🌙"
        # Emotions: English words only, no scores
        emo_words = [
            (e.get("label") or "").lower() for e in (js.get("emotions") or []) if (e.get("label") or "").strip()
        ]
        emo_line = ", ".join(dict.fromkeys([w for w in emo_words if w])) or "calm presence"

        themes_en = {"transition": "transition", "timelessness": "out of time", "flow/emotion": "moving by feeling"}
        th = [themes_en.get(t, t) for t in (js.get("themes") or [])]
        head_core = ", ".join(dict.fromkeys([t for t in th if t])) or "inner seeking"

        sym_words = [s if isinstance(s, str) else str(s) for s in (js.get("symbols") or [])]
        en_symbol_map = {
            "stop": "A stop — a pause between phases. The past is near, yet fading in mist 🚏",
            "fog": "Fog — gentle uncertainty without fear",
            "map": "A changing map — the path is still forming. Look with the heart 👁️",
            "faceless": "Faceless — a familiar state, a part of you already lived 🤍",
            "echo": "Your name from below — your inner voice calling 🌱",
            "stair": "Stairs down as up — going deeper, you grow 🪜",
            "letter": "A wordless letter — meaning known, not yet spoken 💌",
            "light": "Childhood light — safety and your true self 🌙",
            "time": "Time takes your hand — no rush, all in time ⏳",
            "water": "Warm water underfoot — moving through feeling",
            "clock": "Clocks without hands — outside the usual rhythm",
            "city": "Transparent city — inner and outer blur",
            "sky": "Low sky — closeness of experience, focus",
        }
        symbol_lines: List[str] = []
        for s in sym_words[:8]:
            k = s.lower()
            for key, line in en_symbol_map.items():
                if key in k:
                    symbol_lines.append(line)
                    break

        parts = [
            header,
            (f"Emotions: {emo_line} 🌊" if emo_line else ""),
            (psych or ""),
            (esoteric or ""),
            (f"Advice: {advice}" if advice else ""),
        ]
    return "\n".join([p for p in parts if p])


dp = Dispatcher()


@dp.message(Command("start"))
async def cmd_start(message: Message):
    initial_lang = detect_lang(message.text or message.from_user.language_code or "")
    get_or_create_user(message.from_user.id, message.from_user.username, initial_lang)
    lang = get_lang_for_user(message.from_user.id, initial_lang)
    ui = choose_ui_text(lang)
    await message.answer(ui["hello"], reply_markup=main_menu_kb(lang))


@dp.message(Command("mode"))
async def cmd_mode(message: Message):
    lang = get_lang_for_user(message.from_user.id, detect_lang(message.text or ""))
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        if lang == "uk":
            await message.answer("Режими: Mixed | Psychological | Custom. Використай: /mode Mixed")
        elif lang == "ru":
            await message.answer("Режимы: Mixed | Psychological | Custom. Используй: /mode Mixed")
        else:
            await message.answer("Modes: Mixed | Psychological | Custom. Use: /mode Mixed")
        return
    mode = args[1].strip()
    if mode.lower() in ["mixed", "psychological", "custom"]:
        set_user_mode(message.from_user.id, mode.capitalize() if mode.lower() != "psychological" else "Psychological")
        await message.answer(f"Mode set: {mode}")
    else:
        await message.answer("Unknown mode. Use: Mixed | Psychological | Custom")


@dp.message(Command("dream"))
async def cmd_dream(message: Message):
    lang = get_lang_for_user(message.from_user.id, detect_lang(message.text or ""))
    ui = choose_ui_text(lang)
    await message.answer(ui["prompt_dream"])


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    lang = get_lang_for_user(message.from_user.id, detect_lang(message.text or ""))
    ui = choose_ui_text(lang)
    user_id = get_or_create_user(message.from_user.id, message.from_user.username, lang)
    st = get_user_stats(user_id)
    top_themes = ", ".join([f"{k}({v})" for k, v in st["top_themes"]]) or "—"
    top_arch = ", ".join([f"{k}({v})" for k, v in st["top_archetypes"]]) or "—"
    emos = ", ".join([f"{k}={v}" for k, v in st["avg_emotions"].items()]) or "—"
    txt = (
        f"{ui['stats_title']}\n"
        f"Всего снов: {st['total_dreams']}\n"
        f"С анализом: {st['total_analyses']}\n"
        f"Топ темы: {top_themes}\n"
        f"Архетипы: {top_arch}\n"
        f"Эмоции(avg): {emos}"
    )
    await message.answer(txt)


@dp.message(Command("settings"))
async def cmd_settings(message: Message):
    lang = get_lang_for_user(message.from_user.id, detect_lang(message.text or ""))
    u = get_user(message.from_user.id)
    mode = row_get(u, "default_mode", "Mixed")
    notif = (u["notifications_enabled"] if u and "notifications_enabled" in u.keys() else 0) if u else 0
    tz = (u["timezone"] if u and "timezone" in u.keys() else "Europe/Kyiv") if u else "Europe/Kyiv"
    prem = user_is_premium(message.from_user.id)
    if lang == "uk":
        await message.answer(f"Налаштування:\nРежим: {mode}\nСповіщення: {'on' if notif else 'off'}\nЧасовий пояс: {tz}\nРанкове: 08:00, Вечірнє: 20:00\nПреміум: {'так' if prem else 'ні'}", reply_markup=settings_menu_kb(lang))
    elif lang == "ru":
        await message.answer(f"Настройки:\nРежим: {mode}\nУведомления: {'on' if notif else 'off'}\nЧасовой пояс: {tz}\nУтром: 08:00, Вечером: 20:00\nПремиум: {'да' if prem else 'нет'}", reply_markup=settings_menu_kb(lang))
    else:
        await message.answer(f"Settings:\nMode: {mode}\nNotifications: {'on' if notif else 'off'}\nTimezone: {tz}\nMorning: 08:00, Evening: 20:00\nPremium: {'yes' if prem else 'no'}", reply_markup=settings_menu_kb(lang))


@dp.message(Command("tz"))
async def cmd_tz(message: Message):
    lang = get_lang_for_user(message.from_user.id, detect_lang(message.text or ""))
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        prompt = "Надішліть IANA часовий пояс, напр.: /tz Europe/Paris" if lang == "uk" else ("Пришлите IANA таймзону, например: /tz Europe/Paris" if lang == "ru" else "Send IANA timezone, e.g.: /tz Europe/Paris")
        await message.answer(prompt)
        return
    tz = args[1].strip()
    try:
        _ = ZoneInfo(tz)
    except Exception:
        bad = "Невірний часовий пояс" if lang == "uk" else ("Неверный часовой пояс" if lang == "ru" else "Invalid timezone")
        await message.answer(f"{bad}. Examples: Europe/Kyiv, Europe/Paris, Europe/London")
        return
    set_timezone_for_user(message.from_user.id, tz)
    ok = "Оновлено." if lang == "uk" else ("Обновлено." if lang == "ru" else "Updated.")
    await message.answer(f"{ok} Timezone = {tz}")


@dp.message(Command("ask"))
async def cmd_ask(message: Message):
    lang = get_lang_for_user(message.from_user.id, detect_lang(message.text or ""))
    ui = choose_ui_text(lang)
    question = (message.text or "").split(maxsplit=1)
    if len(question) < 2:
        await message.answer(ui["ask_need_text"])
        return

    q = question[1].strip()
    user_id = get_or_create_user(message.from_user.id, message.from_user.username, lang)

   
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT a.json_struct FROM analyses a
        JOIN dreams d ON a.dream_id=d.id
        WHERE d.user_id=?
        ORDER BY a.id DESC LIMIT 10
        """,
        (user_id,),
    )
    ctx_rows = cur.fetchall()
    conn.close()
    summaries = []
    for r in ctx_rows:
        try:
            js = json.loads(r[0]) if r and r[0] else {}
            summ = js.get("summary")
            if summ:
                summaries.append(summ)
        except Exception:
            continue

    if not GOOGLE_API_KEY or genai_new is None:
        await message.answer(ui["no_api"])
        return

    if lang == "uk":
        prompt = (
            f"Питання: {q}\n"
            f"Короткі резюме снів: {summaries[:5]}\n"
            "Дай персональну відповідь, спираючись на повторювані мотиви. Без діагнозів."
        )
    elif lang == "ru":
        prompt = (
            f"Вопрос: {q}\n"
            f"Краткие резюме снов: {summaries[:5]}\n"
            "Дай персональный ответ, опираясь на повторяющиеся мотивы. Без диагнозов."
        )
    else:
        prompt = (
            f"Question: {q}\n"
            f"Short dream summaries: {summaries[:5]}\n"
            "Provide a careful, non-diagnostic, personalized answer referencing patterns."
        )

    await message.chat.do("typing")
    ans = await call_gemini(prompt)
    if not ans:
        ans = "No answer available."

    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO qa (user_id, question, answer, created_at) VALUES (?,?,?,?)",
        (user_id, q, ans, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()

    await message.answer(ans)


def parse_style_and_text(s: str) -> Tuple[Optional[str], str]:
    m = re.match(r"\s*style\s*:\s*([\w-]+)\s*(.*)$", s, re.IGNORECASE)
    if m:
        return m.group(1), m.group(2).strip()
    return None, s.strip()


@dp.message(Command("image"))
async def cmd_image(message: Message):
    lang = get_lang_for_user(message.from_user.id, detect_lang(message.text or ""))
    ui = choose_ui_text(lang)
    txt = (message.text or "").split(maxsplit=1)
    if len(txt) < 2:
        if lang == "uk":
            await message.answer("Використай: /image короткий опис сну")
        elif lang == "ru":
            await message.answer("Используй: /image краткое описание сна")
        else:
            await message.answer("Use: /image short dream description")
        return

    if not user_is_premium(message.from_user.id):
        await message.answer(ui["image_paid"])
        return

    style, dream_text = parse_style_and_text(txt[1])
    struct_prompt = build_struct_prompt(dream_text, lang)
    struct_raw = await call_gemini(struct_prompt)
    if not struct_raw:
        await message.answer(ui["no_api"])
        return

    js = {}
    try:
        m = re.search(r"\{[\s\S]*\}$", struct_raw.strip())
        js = json.loads(m.group(0) if m else struct_raw)
    except Exception:
        pass

    style_hint = f" Стиль: {style}." if style else ""
    if lang == "uk":
        prom = (
            "Сформуй короткий опис сцени для генерації зображення (<=120 слів): "
            "сеттінг, ключові символи, домінуючі кольори/світло, настрій за емоціями.\n"
            f"Структура: {json.dumps(js, ensure_ascii=False)}{style_hint}"
        )
    elif lang == "ru":
        prom = (
            "Сформируй краткое описание сцены для генерации изображения (<=120 слов): "
            "сеттинг, ключевые символы, доминирующие цвета/свет, настроение по эмоциям.\n"
            f"Структура: {json.dumps(js, ensure_ascii=False)}{style_hint}"
        )
    else:
        prom = (
            "Create a concise scene description for image generation (<=120 words): "
            "setting, key symbols, dominant colors/light, mood from emotions.\n"
            f"Structure: {json.dumps(js, ensure_ascii=False)}{style_hint}"
        )

    desc = await call_gemini(prom)
    await message.answer(f"{ui['image_ok']}\n{(desc or '').strip()}")


def normalize_mode(m: Optional[str]) -> str:
    if not m:
        return "Mixed"
    ml = m.lower()
    if ml.startswith("psych"):
        return "Psychological"
    if ml.startswith("cust"):
        return "Custom"
    return "Mixed"


@dp.message(Command("history"))
async def cmd_history(message: Message):
    lang = get_lang_for_user(message.from_user.id, detect_lang(message.text or ""))
    user_id = get_or_create_user(message.from_user.id, message.from_user.username, lang)
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT a.json_struct, d.created_at FROM analyses a
        JOIN dreams d ON a.dream_id=d.id
        WHERE d.user_id=? ORDER BY d.id DESC LIMIT 5
        """,
        (user_id,),
    )
    rows = cur.fetchall()
    conn.close()
    parts = []
    for r in rows:
        try:
            js = json.loads(r[0]) if r and r[0] else {}
            date = r[1][:10] if r and r[1] else ""
            summ = js.get("summary") or ""
            themes = ", ".join(js.get("themes") or [])
            parts.append(f"{date}: {summ}\n{('Темы: ' + themes) if themes else ''}")
        except Exception:
            continue
    if not parts:
        parts = ["Нет записей."] if lang == "ru" else (["Немає записів."] if lang == "uk" else ["No records."])
    await message.answer("\n\n".join(parts))


@dp.message(Command("tarot"))
async def cmd_tarot(message: Message):
    lang = get_lang_for_user(message.from_user.id, detect_lang(message.text or ""))
    if not GOOGLE_API_KEY or genai_new is None:
        await message.answer(choose_ui_text(lang)["no_api"])
        return
    args = (message.text or "").split(maxsplit=2)
    spread = 3
    topic = ""
    if len(args) >= 2 and args[1].isdigit():
        spread = max(1, min(5, int(args[1])))
        topic = args[2] if len(args) >= 3 else ""
    elif len(args) >= 2:
        topic = args[1]
    prompt = build_tarot_prompt(spread, topic, lang, by_dream=False)
    await message.chat.do("typing")
    out = await call_gemini(prompt)
    await message.answer(out or "")


@dp.message(Command("compat"))
async def cmd_compat(message: Message):
    lang = get_lang_for_user(message.from_user.id, detect_lang(message.text or ""))
    if not GOOGLE_API_KEY or genai_new is None:
        await message.answer(choose_ui_text(lang)["no_api"])
        return
    txt = (message.text or "").split(maxsplit=1)
    if len(txt) < 2:
        if lang == "uk":
            await message.answer("Введи дані так: /compat Ім'я1 YYYY-MM-DD; Ім'я2 YYYY-MM-DD")
        elif lang == "ru":
            await message.answer("Введи так: /compat Имя1 YYYY-MM-DD; Имя2 YYYY-MM-DD")
        else:
            await message.answer("Use: /compat Name1 YYYY-MM-DD; Name2 YYYY-MM-DD")
        return
    pair = txt[1]
    if lang == "uk":
        prompt = f"Проаналізуй сумісність двох людей за іменами та датами: {pair}. Дай емоційну сумісність, рекомендації, зони гармонії і конфлікту."
    elif lang == "ru":
        prompt = f"Проанализируй совместимость двух людей по именам и датам: {pair}. Дай эмоциональную совместимость, рекомендации, зоны гармонии и конфликта."
    else:
        prompt = f"Analyze compatibility of two people by names and birthdates: {pair}. Provide emotional compatibility, recommendations, harmony/conflict zones."
    await message.chat.do("typing")
    out = await call_gemini(prompt)
    await message.answer(out or "")


@dp.message(Command("daily"))
async def cmd_daily(message: Message):
    lang = get_lang_for_user(message.from_user.id, detect_lang(message.text or ""))
    args = (message.text or "").split()
    enabled = None
    hour = None
    if len(args) >= 2:
        a = args[1].lower()
        if a in ["on", "off"]:
            enabled = 1 if a == "on" else 0
        elif a.isdigit():
            hour = int(a)
    if len(args) >= 3 and args[2].isdigit():
        hour = int(args[2])
    uid = message.from_user.id
    if enabled is None and hour is None:
        u = get_user(uid)
        curr = 'on' if row_get(u, 'notifications_enabled', 0) else 'off'
        h = row_get(u, 'daily_hour', 9)
        if lang == "uk":
            await message.answer(f"Статус: {curr}, година: {h}. Використай: /daily on 9 або /daily off")
        elif lang == "ru":
            await message.answer(f"Статус: {curr}, час: {h}. Используй: /daily on 9 или /daily off")
        else:
            await message.answer(f"Status: {curr}, hour: {h}. Use: /daily on 9 or /daily off")
        return
    if enabled is not None:
        set_notifications(uid, enabled, hour)
    elif hour is not None:
        set_notifications(uid, row_get(get_user(uid), 'notifications_enabled', 0), hour)
    if lang == "uk":
        await message.answer("Оновлено.")
    elif lang == "ru":
        await message.answer("Обновлено.")
    else:
        await message.answer("Updated.")


@dp.message(F.text & ~F.text.startswith("/"))
async def handle_free_text(message: Message):
    user_text = message.text or ""
    lang = get_lang_for_user(message.from_user.id, detect_lang(user_text or ""))
    ui = choose_ui_text(lang)
    user_id = get_or_create_user(message.from_user.id, message.from_user.username, lang)

    # If user sent a city name in English, map to timezone and confirm
    txt_low = user_text.strip().lower()
    if txt_low in CITY_TO_TZ:
        tz = CITY_TO_TZ[txt_low]
        set_timezone_for_user(message.from_user.id, tz)
        if lang == "uk":
            await message.answer(f"Часовий пояс оновлено: {tz} ✅")
        elif lang == "ru":
            await message.answer(f"Часовой пояс обновлён: {tz} ✅")
        else:
            await message.answer(f"Timezone updated: {tz} ✅")
        # Continue to show settings menu for convenience
        await message.answer(menu_labels(lang)["settings"], reply_markup=settings_menu_kb(lang))
        return

    # Reply menu buttons: open corresponding inline submenus
    ml = menu_labels(lang)
    if user_text.strip() == ml["compat"]:
        await message.answer(ml["compat"], reply_markup=compat_menu_kb(lang))
        return
    if user_text.strip() == ml["interpret"]:
        await message.answer(ml["interpret"], reply_markup=interpret_menu_kb(lang))
        return
    if user_text.strip() == ml["spreads"]:
        await message.answer(ml["spreads"], reply_markup=spreads_menu_kb(lang))
        return
    if user_text.strip() == ml["diary"]:
        await message.answer(ml["diary"], reply_markup=diary_menu_kb(lang))
        return
    if user_text.strip() == ml["settings"]:
        await message.answer(ml["settings"], reply_markup=settings_menu_kb(lang))
        return

    if not GOOGLE_API_KEY or genai_new is None:
        await message.answer(ui["no_api"])
        return

    await message.answer(ui["processing"])
    dream_id = insert_dream(user_id, user_text, GEMINI_MODEL)

    u = get_user(message.from_user.id)
    mode = normalize_mode(row_get(u, "default_mode", "Mixed"))
    js, psych, esoteric, advice = await analyze_dream(user_text, mode=mode, lang=lang)
    insert_analysis(
        dream_id,
        language=lang,
        mode=mode,
        json_struct=json.dumps(js, ensure_ascii=False),
        mixed=f"{psych}\n\n{esoteric}",
        psych=psych,
        esoteric=esoteric,
        advice=advice,
    )

    rendered = render_analysis_text(js, psych, esoteric, advice, lang)
    await message.answer(rendered)


@dp.callback_query(F.data.startswith("compat:"))
async def cb_compat(call: CallbackQuery):
    lang = get_lang_for_user(call.from_user.id, detect_lang(call.message.text or ""))
    action = call.data.split(":", 1)[1]
    if action == "by_birthdates":
        if lang == "uk":
            txt = "Введи: /compat Ім'я1 YYYY-MM-DD; Ім'я2 YYYY-MM-DD"
        elif lang == "ru":
            txt = "Введи: /compat Имя1 YYYY-MM-DD; Имя2 YYYY-MM-DD"
        else:
            txt = "Use: /compat Name1 YYYY-MM-DD; Name2 YYYY-MM-DD"
        await call.message.answer(txt)
    elif action == "by_dreams":
        if lang == "uk":
            txt = "Надішли ключові символи обох снів у форматі: Символи А: ...; Символи Б: ... — і я порівняю."
        elif lang == "ru":
            txt = "Пришли ключевые символы двух снов в формате: Символы A: ...; Символы B: ... — и я сравню."
        else:
            txt = "Send key symbols of two dreams as: Symbols A: ...; Symbols B: ... — I'll compare."
        await call.message.answer(txt)
    elif action == "by_archetypes":
        if lang == "uk":
            txt = "Міні‑тест архетипів: скоро."
        elif lang == "ru":
            txt = "Мини‑тест архетипов: скоро."
        else:
            txt = "Archetype mini‑test: coming soon."
        await call.message.answer(txt)
    await call.answer()


@dp.callback_query(F.data.startswith("interpret:"))
async def cb_interpret(call: CallbackQuery):
    lang = get_lang_for_user(call.from_user.id, detect_lang(call.message.text or ""))
    parts = call.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action in ("mixed", "psych", "custom"):
        mode = "Mixed" if action == "mixed" else ("Psychological" if action == "psych" else "Custom")
        set_user_mode(call.from_user.id, mode)
        if lang == "uk":
            txt = f"Режим за замовчуванням встановлено: {mode} ✅ Надішліть сон — я проаналізую у цьому стилі."
        elif lang == "ru":
            txt = f"Режим по умолчанию установлен: {mode} ✅ Пришлите сон — я проанализирую в этом стиле."
        else:
            txt = f"Default mode set: {mode} ✅ Send a dream — I’ll analyze in this style."
        await call.message.answer(txt)
    elif action == "set_mode":
        # ask to choose default mode via inline again or suggest /mode
        if lang == "uk":
            txt = "Використай /mode Mixed | Psychological | Custom — щоб встановити режим за замовчуванням."
        elif lang == "ru":
            txt = "Используй /mode Mixed | Psychological | Custom — чтобы установить режим по умолчанию."
        else:
            txt = "Use /mode Mixed | Psychological | Custom to set the default mode."
        await call.message.answer(txt)
    else:
        # guide to send a dream now; analysis uses saved default mode
        if lang == "uk":
            txt = "Надішли текст сну одним повідомленням — я проаналізую. Щоб зберегти режим, скористайся /mode."
        elif lang == "ru":
            txt = "Пришли текст сна одним сообщением — я проанализирую. Чтобы сохранить режим, используй /mode."
        else:
            txt = "Send your dream in a single message — I'll analyze it. To save mode, use /mode."
        await call.message.answer(txt)
    await call.answer()


@dp.callback_query(F.data.startswith("spreads:"))
async def cb_spreads(call: CallbackQuery):
    lang = get_lang_for_user(call.from_user.id, detect_lang(call.message.text or ""))
    action = call.data.split(":", 1)[1]
    if action == "one":
        cmd = "/tarot 1"
    elif action == "three":
        cmd = "/tarot 3"
    elif action == "five":
        cmd = "/tarot 5"
    else:
        cmd = "/tarot 3"
    if lang == "uk":
        txt = f"Використай: {cmd} тема"
    elif lang == "ru":
        txt = f"Используй: {cmd} тема"
    else:
        txt = f"Use: {cmd} topic"
    await call.message.answer(txt)
    await call.answer()


@dp.callback_query(F.data.startswith("diary:"))
async def cb_diary(call: CallbackQuery):
    lang = get_lang_for_user(call.from_user.id, detect_lang(call.message.text or ""))
    action = call.data.split(":", 1)[1]
    user_id = get_or_create_user(call.from_user.id, call.from_user.username, lang)
    if action == "history":
        # reuse logic from /history
        conn = db_conn()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT a.json_struct, d.created_at FROM analyses a
            JOIN dreams d ON a.dream_id=d.id
            WHERE d.user_id=? ORDER BY d.id DESC LIMIT 5
            """,
            (user_id,),
        )
        rows = cur.fetchall()
        conn.close()
        parts = []
        for r in rows:
            try:
                js = json.loads(r[0]) if r and r[0] else {}
                date = r[1][:10] if r and r[1] else ""
                summ = js.get("summary") or ""
                themes = ", ".join(js.get("themes") or [])
                parts.append(f"{date}: {summ}\n{('Темы: ' + themes) if themes else ''}")
            except Exception:
                continue
        if not parts:
            parts = ["Нет записей."] if lang == "ru" else (["Немає записів."] if lang == "uk" else ["No records."])
        await call.message.answer("\n\n".join(parts))
    elif action == "stats":
        st = get_user_stats(user_id)
        top_themes = ", ".join([f"{k}({v})" for k, v in st["top_themes"]]) or "—"
        top_arch = ", ".join([f"{k}({v})" for k, v in st["top_archetypes"]]) or "—"
        emos = ", ".join([f"{k}={v}" for k, v in st["avg_emotions"].items()]) or "—"
        title = choose_ui_text(lang)["stats_title"]
        txt = (
            f"{title}\n"
            f"Всего снов: {st['total_dreams']}\n"
            f"С анализом: {st['total_analyses']}\n"
            f"Топ темы: {top_themes}\n"
            f"Архетипы: {top_arch}\n"
            f"Эмоции(avg): {emos}"
        )
        await call.message.answer(txt)
    elif action == "symbol_map":
        if lang == "uk":
            await call.message.answer("Карта символів: скоро.")
        elif lang == "ru":
            await call.message.answer("Карта символов: скоро.")
        else:
            await call.message.answer("Symbol map: coming soon.")
    elif action == "warnings":
        if lang == "uk":
            await call.message.answer("Попередження: скоро.")
        elif lang == "ru":
            await call.message.answer("Предупреждения: скоро.")
        else:
            await call.message.answer("Warnings: coming soon.")
    await call.answer()


@dp.callback_query(F.data.startswith("settings:"))
async def cb_settings(call: CallbackQuery):
    lang = get_lang_for_user(call.from_user.id, detect_lang(call.message.text or ""))
    parts = call.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action == "notifications_on":
        set_notifications(call.from_user.id, 1)
        if lang == "uk":
            await call.message.answer("Сповіщення увімкнено ✅\n\nЩо це дає:\n– Ранком (08:00) — ніжне запитання про сон і короткий настрій дня ☀️\n– Ввечері (20:00) — запитання як минув день 🌙\n\nНапишіть англійською назву міста (наприклад, Kyiv, Paris, London) — я підлаштую час.")
        elif lang == "ru":
            await call.message.answer("Уведомления включены ✅\n\nЧто это даёт:\n– Утром (08:00) — нежный вопрос о сне и мягкий настрой дня ☀️\n– Вечером (20:00) — вопрос как прошёл день 🌙\n\nНапишите на английском название города (например, Kyiv, Paris, London) — я подстрою время. Или используйте /tz Europe/Paris")
        else:
            await call.message.answer("Notifications enabled ✅\n\nYou’ll get:\n– Morning (08:00) — a gentle dream check-in and day mood ☀️\n– Evening (20:00) — how your day went 🌙\n\nSend your city in English (e.g., Kyiv, Paris, London), and I’ll set your timezone. Or use /tz Europe/Paris")
    elif action == "notifications_off":
        set_notifications(call.from_user.id, 0)
        if lang == "uk":
            await call.message.answer("Сповіщення вимкнено ❌\nМи більше не писатимемо першими. Ви завжди можете повернути їх у Налаштуваннях.")
        elif lang == "ru":
            await call.message.answer("Уведомления выключены ❌\nМы больше не будем писать первыми. Вы всегда можете включить их в Настройках.")
        else:
            await call.message.answer("Notifications disabled ❌\nWe won’t text you first anymore. You can re-enable them in Settings anytime.")
    elif action == "mode":
        # Suggest using /mode to persist
        if lang == "uk":
            await call.message.answer("Використай команду /mode Mixed | Psychological | Custom")
        elif lang == "ru":
            await call.message.answer("Используй команду /mode Mixed | Psychological | Custom")
        else:
            await call.message.answer("Use /mode Mixed | Psychological | Custom")
    elif action == "languages":
        await call.message.answer(
            "Виберіть мову:" if lang == "uk" else ("Выберите язык:" if lang == "ru" else "Choose a language:"),
            reply_markup=settings_languages_kb(lang),
        )
    elif action == "timezone":
        note = "Виберіть часовий пояс або використайте /tz" if lang == "uk" else ("Выберите часовой пояс или используйте /tz" if lang == "ru" else "Choose a timezone or use /tz")
        await call.message.answer(note, reply_markup=settings_timezone_kb(lang))
    elif action == "language" and len(parts) >= 3:
        code = parts[2]
        set_language_for_user(call.from_user.id, code)
        # Re-render confirmation + main menu in selected language
        confirm = {
            "uk": "Мову оновлено.",
            "ru": "Язык обновлён.",
            "en": "Language updated.",
        }.get(code, "Language updated.")
        await call.message.answer(confirm, reply_markup=main_menu_kb(code))
    elif action == "tz" and len(parts) >= 3:
        tz = parts[2]
        try:
            _ = ZoneInfo(tz)
            set_timezone_for_user(call.from_user.id, tz)
            msg = "Часовий пояс оновлено." if lang == "uk" else ("Часовой пояс обновлён." if lang == "ru" else "Timezone updated.")
            await call.message.answer(f"{msg} {tz}")
        except Exception:
            bad = "Невірний часовий пояс" if lang == "uk" else ("Неверный часовой пояс" if lang == "ru" else "Invalid timezone")
            await call.message.answer(f"{bad}.")
    await call.answer()


async def main() -> None:
    db_migrate()
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    async def notify_loop():
        while True:
            try:
                now_utc = datetime.utcnow()
                conn = db_conn()
                cur = conn.cursor()
                cur.execute("SELECT tg_user_id, language, timezone, last_morning_sent, last_evening_sent FROM users WHERE notifications_enabled=1")
                rows = cur.fetchall()
                conn.close()
                for r in rows:
                    tg_id = r[0]
                    lang = r[1] or "ru"
                    tz = r[2] or "Europe/Kyiv"
                    last_m = r[3]
                    last_e = r[4]
                    try:
                        local_now = now_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(tz))
                    except Exception:
                        local_now = now_utc
                    today = local_now.date().isoformat()
                    if local_now.hour == 8 and last_m != today:
                        text = morning_text(lang)
                        try:
                            await bot.send_message(chat_id=tg_id, text=text)
                            conn2 = db_conn(); cur2 = conn2.cursor()
                            cur2.execute("UPDATE users SET last_morning_sent=? WHERE tg_user_id=?", (today, tg_id))
                            conn2.commit(); conn2.close()
                        except Exception:
                            pass
                    if local_now.hour == 20 and last_e != today:
                        text = evening_text(lang)
                        try:
                            await bot.send_message(chat_id=tg_id, text=text)
                            conn3 = db_conn(); cur3 = conn3.cursor()
                            cur3.execute("UPDATE users SET last_evening_sent=? WHERE tg_user_id=?", (today, tg_id))
                            conn3.commit(); conn3.close()
                        except Exception:
                            pass
            except Exception:
                pass
            await asyncio.sleep(300)

    asyncio.create_task(notify_loop())
    await Dispatcher.start_polling(dp, bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass

