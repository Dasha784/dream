import os
import asyncio
import json
import sqlite3
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message

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
    await message.answer(ui["hello"])


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

    dream_text = txt[1].strip()
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

    if lang == "uk":
        prom = (
            "Сформуй короткий опис сцени для генерації зображення (<=120 слів): "
            "сеттінг, ключові символи, домінуючі кольори/світло, настрій за емоціями.\n"
            f"Структура: {json.dumps(js, ensure_ascii=False)}"
        )
    elif lang == "ru":
        prom = (
            "Сформируй краткое описание сцены для генерации изображения (<=120 слов): "
            "сеттинг, ключевые символы, доминирующие цвета/свет, настроение по эмоциям.\n"
            f"Структура: {json.dumps(js, ensure_ascii=False)}"
        )
    else:
        prom = (
            "Create a concise scene description for image generation (<=120 words): "
            "setting, key symbols, dominant colors/light, mood from emotions.\n"
            f"Structure: {json.dumps(js, ensure_ascii=False)}"
        )

    desc = await call_gemini(prom)
    await message.answer(f"{ui['image_ok']}\n{(desc or '').strip()}")


@dp.message(F.text & ~F.text.startswith("/"))
async def handle_free_text(message: Message):
    user_text = message.text or ""
    lang = detect_lang(user_text or message.from_user.language_code or "")
    ui = choose_ui_text(lang)
    user_id = get_or_create_user(message.from_user.id, message.from_user.username, lang)

    if not GOOGLE_API_KEY or genai is None:
        await message.answer(ui["no_api"])
        return

    await message.answer(ui["processing"])
    dream_id = insert_dream(user_id, user_text, GEMINI_MODEL)

    js, psych, esoteric, advice = await analyze_dream(user_text, mode="Mixed", lang=lang)
    insert_analysis(
        dream_id,
        language=lang,
        mode="Mixed",
        json_struct=json.dumps(js, ensure_ascii=False),
        mixed=f"{psych}\n\n{esoteric}",
        psych=psych,
        esoteric=esoteric,
        advice=advice,
    )

    rendered = render_analysis_text(js, psych, esoteric, advice, lang)
    await message.answer(rendered)


async def main() -> None:
    db_migrate()
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    await Dispatcher.start_polling(dp, bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass

