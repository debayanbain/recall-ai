#!/usr/bin/env bash
# Size the queue to the load, and give the memory back when the load goes away.
#
# Three things move here, and they are three different levers. Worth being precise about
# which one does what, because "scale Redis" is usually the wrong one:
#
# 1. **Redis memory (this script).** `maxmemory` is a ceiling, not an allocation -- Redis
#    grows into it as tasks queue and frees as they are consumed. This raises the ceiling
#    before a backlog can hit it and lowers it again once the queue has been quiet, and
#    moves the container's own limit with it via `docker update`. `activedefrag` (set in
#    compose) is what makes the freed memory actually return to the OS instead of sitting
#    in fragmented jemalloc pages, which is the usual reason a broker "never shrinks".
#
# 2. **Worker processes (Celery, by itself).** `--autoscale=MAX,MIN` grows the prefork
#    pool when tasks are waiting and retires idle children after a minute. Nothing here
#    has to drive it; it is already the fastest-reacting layer.
#
# 3. **Worker containers (this script).** When the backlog is deeper than one machine's
#    pool can chew through, `docker compose up --scale worker=N` adds whole workers.
#    Skipped entirely unless the `workers` profile is already running -- in development
#    the worker lives on the host under `make dev`, and scaling containers alongside it
#    would put two pools on one queue.
#
# **Redis itself is deliberately not scaled horizontally.** Redis Cluster is the only way
# to add Redis capacity, and kombu -- what Celery speaks to a broker through -- does not
# support cluster mode. A clustered broker is not a bigger queue, it is a broken one. If
# one Redis ever genuinely saturates (it will not: this workload is a handful of ops per
# capture), the move is a dedicated broker instance, not shards.
#
# Every decision is bounded and hysteretic on purpose. An autoscaler with one threshold
# oscillates around it; these use separate up and down triggers plus a cooldown, so a
# burst that lasts one tick does not resize anything twice.
set -euo pipefail

cd "$(dirname "$0")/.."

CONTAINER="${REDIS_CONTAINER:-recall-redis}"
INTERVAL="${AUTOSCALE_INTERVAL:-15}"          # seconds between checks

# --- Redis memory bounds -------------------------------------------------------------
MEM_MIN_MB="${REDIS_MIN_MB:-128}"             # never shrink below this
MEM_MAX_MB="${REDIS_MAX_MB:-2048}"            # never grow beyond this
# Container limit as a multiple of maxmemory. THREE, not two, and this is not padding:
# an AOF rewrite forks, and a fork under writes can transiently approach double the
# dataset in resident pages on top of client buffers. A 2x limit was measured killing the
# broker mid-rewrite -- the process is SIGKILLed by the cgroup, so there is no shutdown
# log and `OOMKilled` reads false afterwards, which makes it look like a mystery restart.
# It recovered from the AOF, which is exactly why that setting is not optional either.
CONTAINER_MULT="${REDIS_CONTAINER_MULT:-3}"
CONTAINER_MIN_MB="${REDIS_CONTAINER_MIN_MB:-256}"  # absolute floor for the limit
GROW_AT="${REDIS_GROW_AT:-70}"                # % of maxmemory that triggers a grow
SHRINK_AT="${REDIS_SHRINK_AT:-25}"            # % below which a shrink is considered
SHRINK_TICKS="${REDIS_SHRINK_TICKS:-4}"       # consecutive quiet checks before shrinking

# --- worker container bounds ---------------------------------------------------------
WORKER_MIN="${WORKER_MIN:-1}"
WORKER_MAX="${WORKER_MAX:-6}"
TASKS_PER_WORKER="${TASKS_PER_WORKER:-20}"    # backlog one container is expected to absorb

quiet_ticks=0
current_replicas=0

log() { printf '%s  autoscale  %s\n' "$(date +%H:%M:%S)" "$*"; }

redis_cli() { docker exec "$CONTAINER" redis-cli "$@"; }

# `INFO` fields arrive as `name:value\r`; strip the carriage return or arithmetic breaks.
info_field() { redis_cli info "$1" | awk -F: -v k="$2" '$1==k {gsub(/\r/,"",$2); print $2}'; }

# Whether the containerised workers are actually running. When they are not, the host
# worker owns the queue and container scaling must stay out of it.
worker_profile_running() {
  [ "$(docker compose ps -q worker 2>/dev/null | wc -l | tr -d ' ')" != "0" ]
}

resize_redis() {
  local target_mb="$1" used_mb="$2" max_mb="$3"

  # Headroom above the data for client buffers and, above all, the copy-on-write of a
  # background rewrite. See CONTAINER_MULT.
  local container_mb=$((target_mb * CONTAINER_MULT))
  [ "$container_mb" -lt "$CONTAINER_MIN_MB" ] && container_mb="$CONTAINER_MIN_MB"

  # A limit near what the process is already using is not a limit, it is a scheduled
  # kill: the next fork crosses it and the cgroup ends the broker mid-write.
  local floor_mb=$((used_mb * CONTAINER_MULT + CONTAINER_MIN_MB))
  [ "$container_mb" -lt "$floor_mb" ] && container_mb="$floor_mb"

  redis_cli config set maxmemory "${target_mb}mb" >/dev/null
  # --memory-swap equal to --memory disables swap for the container: a swapping Redis has
  # latency characteristics that make it worse than a smaller one.
  docker update --memory "${container_mb}m" --memory-swap "${container_mb}m" \
    "$CONTAINER" >/dev/null 2>&1 \
    || log "docker update refused ${container_mb}m (kernel may not allow shrinking a live limit)"

  log "redis maxmemory ${max_mb}mb -> ${target_mb}mb (used ${used_mb}mb, container ${container_mb}mb)"
}

scale_workers() {
  local depth="$1"

  worker_profile_running || return 0

  local want=$(( (depth + TASKS_PER_WORKER - 1) / TASKS_PER_WORKER ))
  [ "$want" -lt "$WORKER_MIN" ] && want="$WORKER_MIN"
  [ "$want" -gt "$WORKER_MAX" ] && want="$WORKER_MAX"

  # Scaling down while a task is mid-flight is safe here: `task_acks_late` means the
  # broker only drops a message once the task finishes, so a container that goes away
  # returns its work to the queue instead of losing it.
  if [ "$want" != "$current_replicas" ]; then
    docker compose --profile workers up -d --no-recreate --scale "worker=$want" worker >/dev/null
    log "workers $current_replicas -> $want (backlog $depth)"
    current_replicas="$want"
  fi
}

if ! docker info >/dev/null 2>&1; then
  echo "autoscale  Docker is not running -- start Docker Desktop first." >&2
  exit 1
fi
if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
  echo "autoscale  no '$CONTAINER' container -- run 'make redis' first." >&2
  exit 1
fi

log "watching every ${INTERVAL}s -- redis ${MEM_MIN_MB}-${MEM_MAX_MB}mb, workers ${WORKER_MIN}-${WORKER_MAX}"

while true; do
  used_mb=$(( $(info_field memory used_memory) / 1048576 ))
  max_bytes=$(redis_cli config get maxmemory | tail -1 | tr -d '\r')
  max_mb=$(( max_bytes / 1048576 ))
  [ "$max_mb" -lt 1 ] && max_mb="$MEM_MAX_MB"   # 0 means "unlimited"; treat as the ceiling

  # Celery's default queue. Depth is the honest load signal for a broker -- memory is a
  # consequence of it, and reacting to memory alone is always one step behind.
  depth=$(redis_cli llen celery | tr -d '\r')
  [ -z "$depth" ] && depth=0

  pct=$(( max_mb > 0 ? used_mb * 100 / max_mb : 0 ))

  if [ "$pct" -ge "$GROW_AT" ] && [ "$max_mb" -lt "$MEM_MAX_MB" ]; then
    target=$(( max_mb * 2 ))
    [ "$target" -gt "$MEM_MAX_MB" ] && target="$MEM_MAX_MB"
    resize_redis "$target" "$used_mb" "$max_mb"
    quiet_ticks=0
  elif [ "$pct" -le "$SHRINK_AT" ] && [ "$max_mb" -gt "$MEM_MIN_MB" ]; then
    quiet_ticks=$((quiet_ticks + 1))
    # Several consecutive quiet checks, not one: shrinking on a single dip is how an
    # autoscaler ends up resizing on every burst gap.
    if [ "$quiet_ticks" -ge "$SHRINK_TICKS" ]; then
      target=$(( max_mb / 2 ))
      [ "$target" -lt "$MEM_MIN_MB" ] && target="$MEM_MIN_MB"
      # Never shrink under the live working set plus room to breathe.
      floor=$(( used_mb * 2 + MEM_MIN_MB ))
      [ "$target" -lt "$floor" ] && target="$floor"
      if [ "$target" -lt "$max_mb" ]; then
        resize_redis "$target" "$used_mb" "$max_mb"
      fi
      quiet_ticks=0
    fi
  else
    quiet_ticks=0
  fi

  scale_workers "$depth"
  sleep "$INTERVAL"
done
