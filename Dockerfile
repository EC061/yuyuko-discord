# Multi-arch image (linux/amd64 + linux/arm64)
# Debian 13 (Trixie) provides glibc 2.41 (>= 2.38 required for prebuilt honker manylinux_2_38 wheels)
FROM python:3.12-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_SYSTEM_PYTHON=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DATA_DIR=/data

WORKDIR /app

# Install CJK fonts for crisp Chinese card rendering + libatomic1 for Playwright driver
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    libatomic1 \
    fonts-noto-cjk \
    fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.6 /uv /uvx /usr/local/bin/

# Copy dependency manifests first for Docker layer caching
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --frozen --no-dev \
    && .venv/bin/playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* /root/.cache

# Copy runtime assets (templates + locales)
COPY locales ./locales
COPY templates ./templates

RUN mkdir -p /data

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD ["uv", "run", "--no-sync", "wows-bot", "--healthcheck"]

ENTRYPOINT ["uv", "run", "--no-sync", "wows-bot"]
