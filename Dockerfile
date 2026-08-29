FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN groupadd --gid 1000 sourcepilot \
    && useradd --uid 1000 --gid sourcepilot --create-home sourcepilot

COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config

RUN pip install --no-cache-dir --editable . \
    && mkdir -p /app/data \
    && chown -R sourcepilot:sourcepilot /app

USER sourcepilot

EXPOSE 8420

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8420/', timeout=3).read()"]

CMD ["python", "-m", "uvicorn", "sourcepilot.api:app", "--app-dir", "src", "--host", "0.0.0.0", "--port", "8420"]
