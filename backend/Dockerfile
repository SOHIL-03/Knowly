# Knowly API (Cloud Run service) and briefing worker (Cloud Run Job) share this image.
# Build from the backend/ directory:  docker build -t knowly-api backend/
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt

# The package is `app`, so imports are `app.*` and the working directory is /srv.
COPY app ./app

RUN useradd --system --uid 10001 knowly
USER knowly

# Service:  uvicorn app.main:app      Job: override the command with `python -m app.job`
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips="*"
