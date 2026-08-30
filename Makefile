.PHONY: install migrate revision dev dev-tunnel tunnel worker flower beat api openapi lint typecheck test check \
	telegram-webhook telegram-webhook-info telegram-webhook-delete

install:            ## sync deps incl. dev extras
	uv sync --extra dev

migrate:            ## apply migrations
	uv run alembic upgrade head

revision:           ## autogenerate a migration: make revision m="add x"
	uv run alembic revision --autogenerate -m "$(m)"

# The worker is started BY the dev runners, not left to a second terminal. A worker that
# has to be remembered is a worker that is sometimes not running, and the symptom of that
# reaches a real person: the bot answers "my processing service is restarting" because
# nothing is consuming the queue. The API cannot start it itself -- under `--reload` it
# would spawn a duplicate on every code change, and in a real deployment the worker is a
# separate deployable -- so it belongs to the thing that runs the dev stack.
#
# Flower comes up with them, on localhost only -- see scripts/dev_flower.sh for why that
# bind address is not negotiable. Its URL is polled briefly before being printed so the
# line is a link that works rather than a promise; on a cold cache the fetch takes longer
# than the poll, so the link is printed either way with the log to look at.
#
# Backgrounded like ngrok above, and cleaned up by the SAME trap. That is not a style
# choice: `trap ... EXIT` replaces any previous EXIT trap rather than adding to it, so a
# second one here would silently orphan the tunnel on Ctrl-C. One trap, set last, cleans
# up every pid that was started -- `$${NGROK_PID:-}` because ngrok is optional and the
# variable may never be set. The final `pkill` catches celery's prefork children, which
# outlive a plain kill of the parent often enough to matter.
define START_SERVICES
./scripts/dev_worker.sh > /tmp/recall-worker.log 2>&1 & \
WORKER_PID=$$!; \
./scripts/dev_flower.sh > /tmp/recall-flower.log 2>&1 & \
FLOWER_PID=$$!; \
trap 'for p in $${NGROK_PID:-} $${WORKER_PID:-} $${FLOWER_PID:-}; do \
        pkill -P $$p 2>/dev/null; kill $$p 2>/dev/null; \
      done; \
      pkill -f "celery -A app.queue.celery_app" 2>/dev/null; true' EXIT INT TERM; \
echo "worker    started (pid $$WORKER_PID) -- log: /tmp/recall-worker.log"; \
FLOWER_URL=""; \
for _ in 1 2 3 4 5 6 7 8 9 10; do \
	curl -sf -o /dev/null http://127.0.0.1:$${FLOWER_PORT:-5555}/ 2>/dev/null \
		&& { FLOWER_URL="http://127.0.0.1:$${FLOWER_PORT:-5555}"; break; }; \
	sleep 0.4; \
done; \
if [ -n "$$FLOWER_URL" ]; then \
	echo "flower    $$FLOWER_URL"; \
else \
	echo "flower    http://127.0.0.1:$${FLOWER_PORT:-5555} (starting -- see /tmp/recall-flower.log)"; \
fi;
endef

# ngrok is backgrounded and uvicorn holds the foreground, so both run from one terminal.
# They CANNOT be chained with `&&`: uvicorn never exits, so the tunnel would never start
# (and on Ctrl-C uvicorn exits non-zero, which short-circuits `&&` anyway).
# The trap stops ngrok when uvicorn does, so Ctrl-C leaves no orphaned tunnel.
# Only Instagram Login needs the tunnel -- it refuses plaintext redirect URIs. Google and
# Facebook are happy on http://localhost.
# The domain is pinned via scripts/ngrok_domain.sh ($NGROK_DOMAIN, else the reserved
# domain already present in .env), so the URL survives restarts and the redirect URIs
# registered with Google/Meta stay valid. Without pinning, every restart breaks them.
# Before pointing the app at the tunnel, read "Instagram Login" in CLAUDE.md: the whole
# API has to move to the https origin or the session cookie lands on the wrong domain.
dev:                ## API with autoreload + ngrok tunnel, both in this terminal
	@if command -v ngrok >/dev/null 2>&1; then \
		D=$$(./scripts/ngrok_domain.sh); \
		ngrok http 8000 $${D:+--domain=$$D} --log=stdout \
			>/tmp/recall-ngrok.log 2>&1 & \
		NGROK_PID=$$!; \
		URL=""; \
		for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do \
			URL=$$(curl -sf http://127.0.0.1:4040/api/tunnels 2>/dev/null \
				| python3 -c 'import sys,json; print(next((t["public_url"] for t in json.load(sys.stdin).get("tunnels",[]) if t["public_url"].startswith("https")), ""))' 2>/dev/null); \
			[ -n "$$URL" ] && break; \
			sleep 0.4; \
		done; \
		if [ -n "$$URL" ]; then \
			echo "tunnel    $$URL"; \
			echo "callback  $$URL/api/v1/auth/instagram/callback"; \
		else \
			echo "tunnel    failed to start -- see /tmp/recall-ngrok.log"; \
		fi; \
	else \
		echo "tunnel    ngrok not installed -- API only (Instagram Login needs https)"; \
	fi; \
	$(START_SERVICES) \
	uv run uvicorn app.main:app --reload --port 8000

# Same as `dev`, but every OAuth redirect URI, BACKEND_URL and the cookie flags are
# repointed at the tunnel for this process only -- .env is never edited, because
# pydantic-settings ranks real env vars above the .env file. Required for Instagram
# Login; a half-moved config fails with `invalid_state` (see the script's header).
dev-tunnel:         ## API + https tunnel, OAuth config auto-pointed at it
	@./scripts/dev_tunnel.sh

tunnel:             ## https tunnel on its own, without the API
	@D=$$(./scripts/ngrok_domain.sh); ngrok http 8000 $${D:+--domain=$$D}

# Flower is Celery's web UI: live worker list, queue depth, task history, and the
# per-task arguments and tracebacks. Worth knowing about because the failure this project
# actually hit -- a worker that was simply not running -- is invisible from the app and
# obvious here. `make dev` and `make dev-tunnel` already start it and print its URL; this
# target is for running it on its own against a stack someone else started.
#
# Bound to localhost by the script, which is not negotiable: it renders task arguments,
# and this project's tasks carry Telegram chat ids. Flower's own `/api/*` endpoints stay
# 401 without FLOWER_UNAUTHENTICATED_API -- leave that unset; the browser UI does not
# need it.
flower:             ## Celery web UI on http://127.0.0.1:5555
	@./scripts/dev_flower.sh

worker:             ## Celery prefork worker -- needs Redis; saves work without it
	uv run celery -A app.queue.celery_app.celery_app worker \
		--loglevel=info --concurrency=$${CELERY_CONCURRENCY:-4}

# Own terminal. Runs the sweeper that rescues Apify runs whose webhook never arrived;
# without it a lost callback leaves an item stuck in `processing` indefinitely.
beat:               ## Celery beat scheduler (stale-run sweeper)
	uv run celery -A app.queue.celery_app.celery_app beat --loglevel=info

api:                ## production-style API: no reload, bound to all interfaces
	uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 \
		--workers $${WEB_CONCURRENCY:-1} --proxy-headers --forwarded-allow-ips='*'

openapi:            ## regenerate docs/openapi.{json,yaml}
	uv run python scripts/export_openapi.py

# Telegram will not deliver to a plaintext URL, so PUBLIC_BASE_URL has to be the tunnel
# in development. The registered URL carries the webhook secret; the script never
# prints it back.
telegram-webhook:   ## point Telegram at PUBLIC_BASE_URL
	uv run python scripts/telegram_webhook.py register

telegram-webhook-info:   ## what Telegram currently thinks, incl. its last error
	uv run python scripts/telegram_webhook.py info

telegram-webhook-delete: ## stop delivery
	uv run python scripts/telegram_webhook.py delete

lint:
	uv run ruff check app tests

typecheck:
	uv run mypy app

test:
	uv run pytest -q

check: lint typecheck test   ## all gates
