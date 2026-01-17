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



CMD ["python", "-u", "dream.py"]
