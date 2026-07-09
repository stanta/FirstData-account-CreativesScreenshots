FROM python:3.14-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock README.md ./
COPY reports_ad ./reports_ad

RUN pip install --upgrade pip uv \
    && uv sync --frozen --no-dev \
    && /app/.venv/bin/python -m playwright install --with-deps chromium \
    && mkdir -p /tmp/reports-ad \
    && rm -rf /root/.cache/pip /root/.cache/ms-playwright /var/lib/apt/lists/*

ENV PATH="/app/.venv/bin:${PATH}" \
    REPORTS_AD_HEADLESS=1 \
    REPORTS_AD_TEMP_ROOT=/tmp/reports-ad

CMD ["python", "-m", "reports_ad"]
