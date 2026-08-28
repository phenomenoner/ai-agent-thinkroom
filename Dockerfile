ARG PYTHON_IMAGE=python:3.12-slim@sha256:7a8b475003c4fe15a2cd4e55e5cfc2f3560bdc9333d624f24cdd6d4340fd7a17
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.3@sha256:2d890623d310b57771ce840f0da5eed5fc6d657da05ffaa45d82797b53fa3abc

FROM ${UV_IMAGE} AS uv
FROM ${PYTHON_IMAGE} AS builder

WORKDIR /app
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY scripts/verify_locked_runtime.py ./scripts/verify_locked_runtime.py
RUN uv sync --locked --no-dev --no-editable \
    && .venv/bin/python scripts/verify_locked_runtime.py uv.lock --write-manifest /app/runtime-lock-manifest.json \
    && rm -rf /root/.cache/uv

FROM ${PYTHON_IMAGE} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH \
    THINKROOM_HOST=127.0.0.1 \
    THINKROOM_INTERNAL_PORT=8788 \
    THINKROOM_PROXY_PORT=8787 \
    THINKROOM_DATABASE_URL=sqlite+aiosqlite:////data/thinkroom.db

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/uv.lock /app/uv.lock
COPY --from=builder /app/runtime-lock-manifest.json /app/runtime-lock-manifest.json
COPY scripts ./scripts
RUN useradd --system --uid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin thinkroom \
    && mkdir -p /data \
    && chown 10001:10001 /data

USER 10001:10001
EXPOSE 8787
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "scripts/healthcheck.py"]
CMD ["python", "scripts/container_entrypoint.py"]
