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
# to restart the whole process.
set -euo pipefail

cd "$(dirname "$0")/.."

LOGLEVEL="${CELERY_LOGLEVEL:-info}"

# `--autoscale=MAX,MIN` instead of a fixed `--concurrency`: the pool adds child processes
# while tasks are waiting and retires idle ones after a minute, so an empty queue costs
# one process and a burst costs as many as the bounds allow. On a laptop that matters in
# both directions -- the old fixed pool held its processes open all day next to uvicorn,
# the reloader and Flower.
AUTOSCALE_MAX="${CELERY_AUTOSCALE_MAX:-8}"
AUTOSCALE_MIN="${CELERY_AUTOSCALE_MIN:-1}"

# A worker with no broker does not fail: it retries the connection forever, quietly, and
# looks exactly like a worker that is running fine. Say so once, up front, rather than
# leaving it to be discovered when a message goes unanswered.
#
# Checked against the container's port, not the default one -- a host Redis answering on
# 6379 is not this project's queue and a green ping against it would be a lie.
REDIS_PORT="${REDIS_PORT:-6380}"
if ! docker exec recall-redis redis-cli ping >/dev/null 2>&1; then
  echo "worker    WARNING: the recall-redis container is not answering." >&2
  echo "worker    Run 'make redis' (needs Docker running) or the queue will never drain." >&2
fi

exec uv run watchfiles \
  "celery -A app.queue.celery_app.celery_app worker --loglevel=$LOGLEVEL --autoscale=$AUTOSCALE_MAX,$AUTOSCALE_MIN" \
  app
