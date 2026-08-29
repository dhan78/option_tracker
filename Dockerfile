# FastAPI + Highcharts option-tracker dashboard
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=America/New_York

WORKDIR /app

# OS tzdata so the host clock resolves to ET (market-hours math).
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*

# Install dependencies first for better layer caching.
COPY pyproject.toml README.md ./
COPY src ./src

# Editable install so webapp static/ assets resolve from the source tree at runtime.
RUN pip install -e .

# Run as a non-root user.
RUN useradd --create-home appuser && chown -R appuser /app
USER appuser

EXPOSE 8050

# Nasdaq API (data) and the Highcharts CDN (browser) require network egress at runtime.
CMD ["uvicorn", "option_tracker.webapp.app:app", "--host", "0.0.0.0", "--port", "8050"]
