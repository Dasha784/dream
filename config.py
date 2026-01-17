
import os
from typing import Optional

# Telegram Bot Token
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "8468925466:AAEIv1fN1cIB2rxJvbed1WbeZ78R1nku6cc")

# Google Gemini API
GOOGLE_API_KEY: str = os.getenv("GOOGLE_API_KEY", "AIzaSyCY-Jcknnpzvzc9h_9vkRRhofLjjAN_6PQ")
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

# Database
DB_PATH: str = os.getenv("DREAMMAP_DB", os.path.join(os.path.dirname(__file__), "dreammap.sqlite3"))

# Валидация
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Please set TELEGRAM_BOT_TOKEN in environment variables.")

# Опционально: можно также читать из локального config.json (если нужно для разработки)
# Пример: если есть config.json локально, он будет использован вместо env
try:
    import json
    config_file = os.path.join(os.path.dirname(__file__), "config.json")
    if os.path.exists(config_file):
        with open(config_file, "r", encoding="utf-8") as f:
            local_config = json.load(f)
            TELEGRAM_BOT_TOKEN = local_config.get("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
            GOOGLE_API_KEY = local_config.get("GOOGLE_API_KEY", GOOGLE_API_KEY)
            GEMINI_MODEL = local_config.get("GEMINI_MODEL", GEMINI_MODEL)
            DB_PATH = local_config.get("DREAMMAP_DB", DB_PATH)
except Exception:
    pass  # Если нет config.json - используем только переменные окружения
