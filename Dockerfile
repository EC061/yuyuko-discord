# Multi-arch image (linux/amd64 + linux/arm64)
# Debian 13 (Trixie) provides glibc 2.41 (>= 2.38 required for prebuilt honker manylinux_2_38 wheels)
FROM python:3.15-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DATA_DIR=/data

WORKDIR /app

# Install CJK fonts for crisp Chinese card rendering + libatomic1 for Playwright driver
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    libatomic1 \
    fonts-noto-cjk \
    fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.6 /uv /uvx /usr/local/bin/

# Install dependencies & Chromium BEFORE copying app source so code edits hit Docker cache
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project \
    && /app/.venv/bin/playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* /root/.cache

# Now copy application source & runtime assets and install project into .venv
COPY src ./src
COPY locales ./locales
COPY templates ./templates
RUN uv sync --frozen --no-dev

# Run as non-root user
RUN useradd -u 10001 -m -s /usr/sbin/nologin botuser \
    && mkdir -p /data \
    && chown -R botuser:botuser /data /app \
    && chmod -R a+rX /ms-playwright

USER botuser

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD ["wows-bot", "--healthcheck"]

ENTRYPOINT ["wows-bot"]
