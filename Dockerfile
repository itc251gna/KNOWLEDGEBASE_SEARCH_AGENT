FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && addgroup --system --gid 10001 app \
    && adduser --system --uid 10001 --ingroup app --home /app --no-create-home app \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY src /app/src
COPY web /app/web
COPY .env.example /app/.env.example

RUN mkdir -p /app/data /app/data/cache /app/data/backups \
    && chown -R app:app /app

ENV PYTHONPATH=/app/src

EXPOSE 8080

CMD ["python", "-m", "portal_search_agent.cli", "serve", "--host", "0.0.0.0", "--port", "8080"]
