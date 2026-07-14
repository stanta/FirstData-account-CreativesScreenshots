#FROM mcr.microsoft.com/playwright/python:v1.61.0-noble
FROM python:3.14-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY reports_ad ./reports_ad

RUN pip install --upgrade pip uv \
    && uv sync --frozen --no-dev \
    && mkdir -p /tmp/reports-ad \
    && rm -rf /root/.cache/pip

ENV PATH="/app/.venv/bin:${PATH}" \
    REPORTS_AD_HEADLESS=1 \
    REPORTS_AD_TEMP_ROOT=/tmp/reports-ad

CMD ["python", "-m", "reports_ad"]
