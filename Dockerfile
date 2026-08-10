FROM mcr.microsoft.com/playwright/python:v1.60.0-jammy

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency files first for layer caching
COPY pyproject.toml uv.lock ./

# Install dependencies (no dev, no editable, sync from lockfile)
RUN uv sync --frozen --no-dev --no-install-project

# Copy source
COPY src/ ./src/
COPY config.yaml status.json README.md ./

# Install the project itself
RUN uv sync --frozen --no-dev

# Playwright browsers already in base image — no install needed.
# Actually launch chromium (not just import the module) so a base-image /
# pip-version mismatch fails the build instead of shipping silently.
RUN uv run python -c "from playwright.sync_api import sync_playwright; \
    pw = sync_playwright().start(); \
    browser = pw.chromium.launch(); \
    browser.close(); \
    pw.stop(); \
    print('playwright chromium launch ok')"

ENV PATH="/app/.venv/bin:$PATH"

ENTRYPOINT ["python", "-m", "better_bot.bot"]
CMD []
