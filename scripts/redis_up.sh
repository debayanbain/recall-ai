#!/usr/bin/env bash
# Bring up the project's Redis container and wait until it actually answers.
#
# Started by the dev runners rather than left to a person, for the same reason the worker
# is: an infrastructure dependency that has to be remembered is one that is sometimes not
# running, and the symptom reaches a real person. A Celery worker with no broker does not
# fail -- it retries the connection forever, quietly, and looks exactly like a healthy
# worker while every capture sits at `pending`.
#
# Waiting for the healthcheck is the point. `docker compose up -d` returns as soon as the
# container is *created*, which is a second or two before Redis is accepting connections;
# starting the worker in that window is how a cold start begins with a burst of
# connection errors in the log.
set -euo pipefail

cd "$(dirname "$0")/.."

PORT="${REDIS_PORT:-6380}"
CONTAINER="recall-redis"

if ! docker info >/dev/null 2>&1; then
  echo "redis     Docker is not running -- start Docker Desktop, then try again." >&2
  echo "redis     (this project's queue lives in the '$CONTAINER' container, not on the host)" >&2
  exit 1
fi

docker compose up -d redis >/dev/null

for _ in $(seq 1 40); do
  state="$(docker inspect -f '{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || echo starting)"
  [ "$state" = "healthy" ] && break
  sleep 0.5
done

if [ "${state:-}" != "healthy" ]; then
  echo "redis     container is up but not answering -- docker compose logs redis" >&2
  exit 1
fi

echo "redis     redis://localhost:$PORT (container $CONTAINER)"

# A host Redis on 6379 is not a problem -- it just is not ours. Said out loud because
# "why is my queue empty" is usually "something is pointed at the other one".
if lsof -nP -iTCP:6379 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "redis     note: something else is still listening on 6379; this project uses $PORT only"
fi
