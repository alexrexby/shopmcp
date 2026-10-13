# One image, one store: the store config is mounted at /app/store, the index lives in /data.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.11 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    SHOPMCP_DATA_DIR=/data

WORKDIR /app

# Dependencies first: this layer survives source edits.
COPY pyproject.toml uv.lock .python-version README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# /data is created and owned here: a fresh named volume copies this ownership, a host
# bind mount does not (on a Linux host it stays root:root and uid 10001 cannot write).
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin shopmcp \
    && mkdir -p /data /app/store \
    && chown 10001:10001 /data
VOLUME /data
USER 10001

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"]
CMD ["shopmcp", "serve", "/app/store/store.toml"]
