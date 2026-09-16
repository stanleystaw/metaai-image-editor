FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    META_AI_HEADED=false \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app

COPY requirements.txt pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir -e ".[api]" && \
    playwright install --with-deps chromium && \
    rm -rf /var/lib/apt/lists/* /root/.cache/pip

COPY . .

RUN useradd --create-home --uid 10001 appuser && \
    mkdir -p /tmp/metaai-uploads && \
    chown -R appuser:appuser /app /tmp/metaai-uploads /ms-playwright
USER appuser

EXPOSE 8000

# Render injects PORT dynamically. Keep one worker: one Playwright page cannot
# safely process concurrent edits.
CMD ["sh", "-c", "uvicorn metaai_api.api_server:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
