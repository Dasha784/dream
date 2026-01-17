FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata git \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app
COPY . /app

RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# Do not put secrets in the image; set them as Railway Variables
# Example Railway Variables to set in dashboard:
#   TELEGRAM_BOT_TOKEN=<your token>
#   GOOGLE_API_KEY=<your Gemini key>
#   GEMINI_MODEL=gemini-1.5-flash
#   DREAMMAP_DB=/data/dreammap.sqlite3

CMD ["python", "-u", "dream.py"]
