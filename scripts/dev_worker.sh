#!/usr/bin/env bash
# The Celery worker, for development, with the two properties the bare `make worker`
# does not have.
#
# **It reloads on code change.** `uvicorn --reload` watches `app/` and restarts itself;
# the worker never did, so every edit left a worker running yesterday's code while the
# API ran today's. That failure is silent and it is nasty: the bot answers, it just
# answers with the logic you thought you had replaced. `watchfiles` (already a
# dependency -- it is what uvicorn's own reloader uses) fixes it by restarting the worker
# on the same signal.
#
# **It is startable from another script**, so the dev runners can bring it up alongside
# the API instead of relying on someone remembering a second terminal. A worker that has
# to be started by hand is a worker that is sometimes not started, and the symptom of
# that is a Telegram user being told the service is restarting.
#
# Celery's prefork pool does not tolerate being reloaded in place, so watchfiles is told
# to restart the whole process. `--concurrency` stays low here on purpose: this is a
# laptop, and the API, the reloader and two workers already share it.
set -euo pipefail

cd "$(dirname "$0")/.."

CONCURRENCY="${CELERY_CONCURRENCY:-2}"
LOGLEVEL="${CELERY_LOGLEVEL:-info}"

# A worker with no broker does not fail: it retries the connection forever, quietly, and
# looks exactly like a worker that is running fine. Say so once, up front, rather than
# leaving it to be discovered when a message goes unanswered.
if ! redis-cli ping >/dev/null 2>&1; then
  echo "worker    WARNING: redis is not answering on the default port." >&2
  echo "worker    Start it (brew services start redis) or the queue will never drain." >&2
fi

exec uv run watchfiles \
  "celery -A app.queue.celery_app.celery_app worker --loglevel=$LOGLEVEL --concurrency=$CONCURRENCY" \
  app
