import os
import asyncio
import json
import sqlite3
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

try:
    import google.generativeai as genai
except Exception:
    genai = None 


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8468925466:AAEIv1fN1cIB2rxJvbed1WbeZ78R1nku6cc")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "AIzaSyAFzpmXWjJpEj5VokanRhobA9aHL0ip87o")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Please set TELEGRAM_BOT_TOKEN in environment variables.")

if GOOGLE_API_KEY and genai is not None:
    try:
        genai.configure(api_key=GOOGLE_API_KEY)
    except Exception:
        pass

DB_PATH = os.getenv("DREAMMAP_DB", os.path.join(os.path.dirname(__file__), "dreammap.sqlite3"))


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
    conn.commit()
    conn.close()


def get_lang_for_user(tg_user_id: int, fallback: str = "ru") -> str:
    u = get_user(tg_user_id)
    if u and u.get("language"):
        return u.get("language")
    return fallback


def set_language_for_user(tg_user_id: int, language: str) -> None:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET language=? WHERE tg_user_id=?", (language, tg_user_id))
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
            "image_ok": "Готую візуалізацію (демо-опис):",
            "ask_need_text": "Використай: /ask ваше запитання",
            "stats_title": "Статистика ваших снів",
        }
    if lang == "ru":
        return {
            "hello": "Привет! Пришли текст сна — верну структурированный анализ (Mixed). Команда /dream — тоже принимает сон.",
            "prompt_dream": "Пожалуйста, отправь текст сна одним сообщением.",
            "processing": "Обрабатываю сон…",
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
        "processing": "Processing your dream…",
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
        items = [("Режим за замовч.", "settings:mode"), ("Увімкнути нотиф.", "settings:notifications_on"), ("Вимкнути нотиф.", "settings:notifications_off"), ("Мова RU", "settings:language:ru"), ("Мова UK", "settings:language:uk"), ("Language EN", "settings:language:en")]
    elif lang == "ru":
        items = [("Режим по умолч.", "settings:mode"), ("Включить уведомл.", "settings:notifications_on"), ("Выключить уведомл.", "settings:notifications_off"), ("Язык RU", "settings:language:ru"), ("Мова UK", "settings:language:uk"), ("Language EN", "settings:language:en")]
    else:
        items = [("Default mode", "settings:mode"), ("Enable notif.", "settings:notifications_on"), ("Disable notif.", "settings:notifications_off"), ("Language RU", "settings:language:ru"), ("Language UK", "settings:language:uk"), ("Language EN", "settings:language:en")]
    kb = InlineKeyboardBuilder()
    for text, data in items:
        kb.button(text=text, callback_data=data)
    kb.adjust(2)
    return kb.as_markup()

def gemini_client():
    if not GOOGLE_API_KEY or genai is None:
        return None
    try:
        return genai.GenerativeModel(GEMINI_MODEL)
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


def build_interpret_prompt(struct_json: str, mode: str, lang: str) -> str:
    if lang == "uk":
        base = "На основі структури дай: 1) Психологічну інтерпретацію 2) Езотеричну (м’яко) 3) Пораду/урок (2–3 пункти)."
    elif lang == "ru":
        base = "На основе структуры дай: 1) Психологическую интерпретацию 2) Эзотерическую (мягко) 3) Совет/урок (2–3 пункта)."
    else:
        base = "Based on the structure, provide: 1) Psychological interpretation 2) Esoteric (gently) 3) Advice/lesson (2–3 bullets)."
    return (
        f"{base}\n"
        f"Mode: {mode}.\n"
        f"Structure (JSON): {struct_json}\n"
        "Return a compact response with three labeled sections: PSYCH, ESOTERIC, ADVICE."
    )


async def call_gemini(prompt: str) -> str:
    model = gemini_client()
    if not model:
        return ""
    try:
        resp = await asyncio.to_thread(model.generate_content, prompt)
        return resp.text or ""
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

    interp_prompt = build_interpret_prompt(json.dumps(js, ensure_ascii=False), mode, lang)
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

    if lang == "uk":
        header = "Аналіз сну (Mixed)"
        parts = [
            header,
            f"Локація: {loc}",
            chars,
            acts,
            syms,
            f"Емоції: {emos}",
            thms,
            arch,
            f"Стислий підсумок: {summ}",
            "— Психологічне —",
            psych or "(н/д)",
            "— Езотеричне —",
            esoteric or "(н/д)",
            "— Порада/Урок —",
            advice or "(н/д)",
        ]
    elif lang == "ru":
        header = "Анализ сна (Mixed)"
        parts = [
            header,
            f"Локация: {loc}",
            chars,
            acts,
            syms,
            f"Эмоции: {emos}",
            thms,
            arch,
            f"Краткое резюме: {summ}",
            "— Психологическая —",
            psych or "(н/д)",
            "— Эзотерическая —",
            esoteric or "(н/д)",
            "— Совет/Урок —",
            advice or "(н/д)",
        ]
    else:
        header = "Dream Analysis (Mixed)"
        parts = [
            header,
            f"Location: {loc}",
            chars,
            acts,
            syms,
            f"Emotions: {emos}",
            thms,
            arch,
            f"Summary: {summ}",
            "— Psychological —",
            psych or "(n/a)",
            "— Esoteric —",
            esoteric or "(n/a)",
            "— Advice/Lesson —",
            advice or "(n/a)",
        ]
    return "\n".join([p for p in parts if p])


dp = Dispatcher()


@dp.message(Command("start"))
async def cmd_start(message: Message):
    lang = detect_lang(message.text or message.from_user.language_code or "")
    ui = choose_ui_text(lang)
    get_or_create_user(message.from_user.id, message.from_user.username, lang)
    await message.answer(ui["hello"], reply_markup=main_menu_kb(lang))


@dp.message(Command("mode"))
async def cmd_mode(message: Message):
    lang = detect_lang(message.text or message.from_user.language_code or "")
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
    lang = detect_lang(message.text or message.from_user.language_code or "")
    ui = choose_ui_text(lang)
    await message.answer(ui["prompt_dream"])


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    lang = detect_lang(message.text or message.from_user.language_code or "")
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
    lang = detect_lang(message.text or message.from_user.language_code or "")
    u = get_user(message.from_user.id)
    mode = (u["default_mode"] if u and "default_mode" in u.keys() else "Mixed") if u else "Mixed"
    notif = (u["notifications_enabled"] if u and "notifications_enabled" in u.keys() else 0) if u else 0
    hour = (u["daily_hour"] if u and "daily_hour" in u.keys() else 9) if u else 9
    prem = user_is_premium(message.from_user.id)
    if lang == "uk":
        await message.answer(f"Налаштування:\nРежим: {mode}\nСповіщення: {'on' if notif else 'off'} {hour}:00\nПреміум: {'так' if prem else 'ні'}")
    elif lang == "ru":
        await message.answer(f"Настройки:\nРежим: {mode}\nУведомления: {'on' if notif else 'off'} {hour}:00\nПремиум: {'да' if prem else 'нет'}")
    else:
        await message.answer(f"Settings:\nMode: {mode}\nNotifications: {'on' if notif else 'off'} {hour}:00\nPremium: {'yes' if prem else 'no'}")


@dp.message(Command("ask"))
async def cmd_ask(message: Message):
    lang = detect_lang(message.text or message.from_user.language_code or "")
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

    if not GOOGLE_API_KEY or genai is None:
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
    lang = detect_lang(message.text or message.from_user.language_code or "")
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
    lang = detect_lang(message.text or message.from_user.language_code or "")
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
    lang = detect_lang(message.text or message.from_user.language_code or "")
    if not GOOGLE_API_KEY or genai is None:
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
    if lang == "uk":
        prompt = f"Створи розклад Таро на {spread} карт(и) з темою: {topic}. Опиши карти й інтерпретацію."
    elif lang == "ru":
        prompt = f"Сделай расклад Таро на {spread} карт(ы) по теме: {topic}. Опиши карты и интерпретацию."
    else:
        prompt = f"Create a Tarot spread of {spread} cards on: {topic}. Describe cards and interpretation."
    await message.chat.do("typing")
    out = await call_gemini(prompt)
    await message.answer(out or "")


@dp.message(Command("compat"))
async def cmd_compat(message: Message):
    lang = detect_lang(message.text or message.from_user.language_code or "")
    if not GOOGLE_API_KEY or genai is None:
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
    lang = detect_lang(message.text or message.from_user.language_code or "")
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
        curr = 'on' if (u and u.get('notifications_enabled')) else 'off'
        h = u.get('daily_hour') if u else 9
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
        set_notifications(uid, get_user(uid).get('notifications_enabled') or 0, hour)
    if lang == "uk":
        await message.answer("Оновлено.")
    elif lang == "ru":
        await message.answer("Обновлено.")
    else:
        await message.answer("Updated.")


@dp.message(F.text & ~F.text.startswith("/"))
async def handle_free_text(message: Message):
    user_text = message.text or ""
    lang = detect_lang(user_text or message.from_user.language_code or "")
    ui = choose_ui_text(lang)
    user_id = get_or_create_user(message.from_user.id, message.from_user.username, lang)

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

    if not GOOGLE_API_KEY or genai is None:
        await message.answer(ui["no_api"])
        return

    await message.answer(ui["processing"])
    dream_id = insert_dream(user_id, user_text, GEMINI_MODEL)

    u = get_user(message.from_user.id)
    mode = normalize_mode(u.get("default_mode") if u else "Mixed")
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
    if action == "set_mode":
     
        if lang == "uk":
            txt = "Використай /mode Mixed | Psychological | Custom — щоб встановити режим за замовчуванням."
        elif lang == "ru":
            txt = "Используй /mode Mixed | Psychological | Custom — чтобы установить режим по умолчанию."
        else:
            txt = "Use /mode Mixed | Psychological | Custom to set the default mode."
        await call.message.answer(txt)
    else:
     
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
        await call.message.answer("Notifications ON" if lang == "en" else ("Уведомления включены" if lang == "ru" else "Сповіщення увімкнено"))
    elif action == "notifications_off":
        set_notifications(call.from_user.id, 0)
        await call.message.answer("Notifications OFF" if lang == "en" else ("Уведомления выключены" if lang == "ru" else "Сповіщення вимкнено"))
    elif action == "mode":
 
        if lang == "uk":
            await call.message.answer("Використай команду /mode Mixed | Psychological | Custom")
        elif lang == "ru":
            await call.message.answer("Используй команду /mode Mixed | Psychological | Custom")
        else:
            await call.message.answer("Use /mode Mixed | Psychological | Custom")
    elif action == "language" and len(parts) >= 3:
        code = parts[2]
        set_language_for_user(call.from_user.id, code)
        await call.message.answer("Language updated." if code == "en" else ("Язык обновлён." if code == "ru" else "Мову оновлено."), reply_markup=main_menu_kb(code))
    await call.answer()


async def main() -> None:
    db_migrate()
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    async def notify_loop():
        while True:
            try:
                now = datetime.utcnow()
                date_str = now.date().isoformat()
                conn = db_conn()
                cur = conn.cursor()
                cur.execute("SELECT tg_user_id, daily_hour, last_daily_sent FROM users WHERE notifications_enabled=1")
                rows = cur.fetchall()
                conn.close()
                for r in rows:
                    tg_id = r[0]
                    hour = r[1] if r[1] is not None else 9
                    last = r[2]
                    if now.hour == int(hour) and last != date_str:
                        lang = "ru"
                        u = get_user(tg_id)
                        if u and u.get("language"):
                            lang = u.get("language")
                        if not GOOGLE_API_KEY or genai is None:
                            continue
                        if lang == "uk":
                            prompt = "Щоденна порада/карта дня для користувача. Коротко, бережно, 2–3 речення."
                        elif lang == "ru":
                            prompt = "Ежедневный совет/карта дня для пользователя. Коротко, бережно, 2–3 предложения."
                        else:
                            prompt = "Daily tip/card for the user. Short, gentle, 2–3 sentences."
                        txt = await call_gemini(prompt)
                        try:
                            await bot.send_message(chat_id=tg_id, text=txt or "Have a gentle day.")
                            mark_daily_sent(tg_id, date_str)
                        except Exception:
                            pass
            except Exception:
                pass
            await asyncio.sleep(600)

    asyncio.create_task(notify_loop())
    await Dispatcher.start_polling(dp, bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass

