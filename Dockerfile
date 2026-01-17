
FROM python:3.11-slim


RUN apt-get update && apt-get install -y \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY . /app

RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt



ENV GOOGLE_API_KEY "ВАШ_НОВЫЙ_API_КЛЮЧ_ИЗ_GOOGLE_AI_STUDIO"
ENV TELEGRAM_BOT_TOKEN "ТОКЕН_ВАШЕГО_ТГ_БОТА"
ENV GEMINI_MODEL "gemini-1.5-flash"
ENV DREAMMAP_DB "c:\Users\dasha\OneDrive\Desktop\работа\dreammap.sqlite3"
CMD ["python", "dream.py"]
