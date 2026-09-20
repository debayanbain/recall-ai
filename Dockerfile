# syntax=docker/dockerfile:1
#
# RecallAI backend -- production image.
#
# ONE image, four workloads. Only the command changes:
#
#   api       uvicorn app.main:app --host 0.0.0.0 --port 8000   (default CMD)
#   worker    celery -A app.queue.celery_app.celery_app worker
#   beat      celery -A app.queue.celery_app.celery_app beat    (exactly one replica)
#   migrate   alembic upgrade head                              (a k8s Job, not a Deployment)
#
# Deliberate: a worker running different code from the API is silent and worse than a
# crash -- the bot still answers, just with the logic you thought you had replaced. One
# image, one digest, one version of the truth.
#
# Built linux/arm64 on Apple Silicon; the t4g node is arm64 too, so what passes locally
# is what runs on the cluster. For a mixed cluster:
#   docker buildx build --platform linux/amd64,linux/arm64 -t <registry>/recall-backend:<tag> --push .
#
# Runs as uid 10001 -- pin the same value in the Deployment so the manifest and the
# image cannot disagree:
#   securityContext: { runAsNonRoot: true, runAsUser: 10001, allowPrivilegeEscalation: false }
#
# No HEALTHCHECK: containerd ignores it. Liveness and readiness are probes on /health
# and /ready in the Deployment.

############################
# Stage 1 -- resolve deps
############################
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Deps only, from the lockfile, in their own layer: a source edit must not re-resolve
# 200 packages. --frozen fails rather than quietly resolving something newer than what
# was tested -- the whole point of shipping a lockfile. --no-install-project keeps the
# app out of the venv; it is COPYed in below and imported from /app.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

############################
# Stage 2 -- runtime
############################
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

RUN useradd --system --create-home --uid 10001 app

WORKDIR /app

# WORKDIR creates /app as root, so a non-root process cannot mkdir inside it. The dev
# log sink writes LOG_DIR/<source>-<date>.jsonl and fails with EACCES otherwise. In the
# cluster ENV != dev, so logs go to stdout and this directory stays empty -- which is
# also what lets the pod run with a read-only root filesystem if you want one.
RUN install -d -o app -g app /app /app/logs

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app app ./app
COPY --chown=app:app migrations ./migrations
# The one-off backfills (connections, thumbnails, rerun_item) run as Jobs off this same
# image. Leaving them out means a data fix needs a second image built by hand.
COPY --chown=app:app scripts ./scripts
COPY --chown=app:app alembic.ini pyproject.toml ./

USER app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
