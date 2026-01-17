FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata git \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Сначала копируем requirements.txt для кэширования слоев Docker
COPY requirements.txt /app/requirements.txt

# Устанавливаем зависимости
RUN pip install --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

# Копируем остальные файлы
COPY . /app

# Проверка установки критических пакетов
RUN python -c "import aiogram; print(f'✓ aiogram {aiogram.__version__} installed')" || exit 1
RUN python -c "import google.genai; print('✓ google-genai installed')" || echo "⚠ Warning: google-genai not installed (optional)"

# Переменные окружения можно задавать здесь или через docker run/docker-compose
# ENV TELEGRAM_BOT_TOKEN=""
# ENV GOOGLE_API_KEY=""
# ENV GEMINI_MODEL="gemini-1.5-flash"
# ENV DREAMMAP_DB="/app/data/dreammap.sqlite3"

# Создаем директорию для базы данных если нужно
RUN mkdir -p /app/data

CMD ["python", "-u", "dream.py"]
