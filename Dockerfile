# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_SYSTEM_PYTHON=1 \
    UV_COMPILE_BYTECODE=1

# uv: fast, reproducible dependency install.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install deps first for layer caching.
COPY pyproject.toml ./
RUN uv pip install --system .

# App source.
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./

EXPOSE 8000

# Default: API server. Worker overrides command in compose.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
