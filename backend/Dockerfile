FROM ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff AS uv
FROM python:3.15.0-alpine3.24@sha256:2699b90f4d562a22e2dd076c82f964032f64814204bff36d271a54ba4cc2c7e2 AS builder
COPY --from=uv /uv /usr/local/bin/uv
RUN python -m pip uninstall -y pip && rm -rf /usr/local/lib/python3.13/ensurepip
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project
COPY app ./app
COPY scripts ./scripts
COPY alembic.ini ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project

# Test tools are confined to this explicitly selected stage.
FROM builder AS test
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app PYTHONUNBUFFERED=1
RUN apk add --no-cache bash
CMD ["pytest"]

FROM python:3.15.0-alpine3.24@sha256:2699b90f4d562a22e2dd076c82f964032f64814204bff36d271a54ba4cc2c7e2 AS runtime
RUN python -m pip uninstall -y pip && rm -rf /usr/local/lib/python3.13/ensurepip
RUN apk add --no-cache bash && addgroup -g 10001 app && adduser -D -H -u 10001 -G app app
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY --from=builder --chown=10001:10001 /app/.venv ./.venv
COPY --from=builder --chown=10001:10001 /app/app/*.py ./app/
COPY --from=builder --chown=10001:10001 /app/app/api ./app/api
COPY --from=builder --chown=10001:10001 /app/app/core ./app/core
COPY --from=builder --chown=10001:10001 /app/app/alembic ./app/alembic
COPY --from=builder --chown=10001:10001 /app/app/email-templates/build ./app/email-templates/build
COPY --from=builder --chown=10001:10001 /app/scripts ./scripts
COPY --from=builder --chown=10001:10001 /app/alembic.ini ./
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/utils/health-check/', timeout=3)"]
CMD ["fastapi", "run", "--workers", "4", "app/main.py"]
