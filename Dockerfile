# syntax=docker/dockerfile:1.7
# DOCKER_BUILDKIT=1 docker build -f reports_ad/Dockerfile reports_ad --progress=plain
#FROM mcr.microsoft.com/playwright/python:v1.61.0-noble
FROM python:3.14-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md ./

RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --upgrade pip setuptools wheel \
    && python -c 'import tomllib; from pathlib import Path; project = tomllib.loads(Path("pyproject.toml").read_text()); print("\n".join(dep.strip() for dep in project["project"].get("dependencies", []) if dep.strip() and not dep.strip().startswith("#")))' > /tmp/requirements.txt

RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install -r /tmp/requirements.txt

RUN apt-get update \
    && apt-get install -y --no-install-recommends cabextract wget fonts-dejavu-core fonts-liberation2 fonts-urw-base35 fontconfig \
    && mkdir -p /usr/share/fonts/truetype/msttcorefonts /usr/share/fonts/truetype/helvetica \
    && wget -O /tmp/arial32.exe https://downloads.sourceforge.net/corefonts/arial32.exe \
    && cabextract -q -d /usr/share/fonts/truetype/msttcorefonts /tmp/arial32.exe \
    && rm -f /tmp/arial32.exe \
    && ln -sf /usr/share/fonts/opentype/urw-base35/NimbusSans-Regular.otf /usr/share/fonts/truetype/helvetica/Helvetica.ttf \
    && ln -sf /usr/share/fonts/opentype/urw-base35/NimbusSans-Bold.otf /usr/share/fonts/truetype/helvetica/Helvetica-Bold.ttf \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

COPY reports_ad ./reports_ad

RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --no-deps . \
    && mkdir -p /tmp/reports-ad

ENV REPORTS_AD_HEADLESS=1 \
    REPORTS_AD_TEMP_ROOT=/tmp/reports-ad

CMD ["python", "-m", "reports_ad"]
