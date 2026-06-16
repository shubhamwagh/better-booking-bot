FROM mcr.microsoft.com/playwright/python:v1.52.0-jammy

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency files first for layer caching
COPY pyproject.toml uv.lock ./

# Install dependencies (no dev, no editable, sync from lockfile)
RUN uv sync --frozen --no-dev --no-install-project

# Copy source
COPY src/ ./src/
COPY config.yaml README.md ./

# Install the project itself
RUN uv sync --frozen --no-dev

# Playwright browsers already in base image — no install needed
# Verify chromium is available
RUN uv run python -c "from playwright.sync_api import sync_playwright; print('playwright ok')"

ENV PATH="/app/.venv/bin:$PATH"

ENTRYPOINT ["python", "-m", "better_bot.bot"]
CMD []
