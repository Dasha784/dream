
FROM python:3.11-slim


RUN apt-get update && apt-get install -y \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY . /app

RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt


ENV TELEGRAM_BOT_TOKEN=8468925466:AAEIv1fN1cIB2rxJvbed1WbeZ78R1nku6cc
ENV GOOGLE_API_KEY=AIzaSyAFzpmXWjJpEj5VokanRhobA9aHL0ip87o
ENV DREAMMAP_DB=dreammap.sqlite3
ENV GEMINI_MODEL=gemini-1.5-flash

CMD ["python", "dream.py"]
