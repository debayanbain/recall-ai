#!/usr/bin/env bash
# Flower, Celery's web UI, for development.
#
# It exists in the dev stack because the failure this project actually hit is invisible
# from the application: the worker was simply not running, the webhook still answered
# 202, and the only symptom was a Telegram user being told the processing service was
# restarting. Flower shows worker liveness, queue depth, task history and per-task
# tracebacks, so "is anything consuming the queue?" is a glance rather than an
# investigation.
#
# **Bound to 127.0.0.1, deliberately.** Flower renders task arguments, and this project's
# tasks carry Telegram chat ids and vault item ids. There is no authentication in front
# of it, so it must not be reachable from the network -- and in particular it must never
# be published through the ngrok tunnel that the rest of the dev stack runs behind.
#
# Never fails the stack. If Flower cannot start, the runner prints that and the API and
# worker carry on: a dashboard is an observability convenience, not a dependency.
set -euo pipefail

cd "$(dirname "$0")/.."

PORT="${FLOWER_PORT:-5555}"

# Installed via the dev extra is the fast path. `uv run --with` is the fallback for a
# checkout whose `make install` predates flower being added -- it resolves and caches on
# first use, which is why the runner does not block on the URL being live.
if uv run --no-sync python -c "import flower" >/dev/null 2>&1; then
  exec uv run --no-sync celery -A app.queue.celery_app.celery_app flower \
    --address=127.0.0.1 --port="$PORT"
fi

echo "flower    not installed in this env -- fetching it (run 'make install' to keep it)" >&2
exec uv run --with flower celery -A app.queue.celery_app.celery_app flower \
  --address=127.0.0.1 --port="$PORT"
