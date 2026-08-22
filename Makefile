.PHONY: install migrate revision dev dev-tunnel tunnel worker api openapi lint typecheck test check up down logs

install:            ## sync deps incl. dev extras
	uv sync --extra dev

migrate:            ## apply migrations
	uv run alembic upgrade head

revision:           ## autogenerate a migration: make revision m="add x"
	uv run alembic revision --autogenerate -m "$(m)"

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
		trap 'pkill -P $$NGROK_PID 2>/dev/null; kill $$NGROK_PID 2>/dev/null; true' EXIT INT TERM; \
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
	uv run uvicorn app.main:app --reload --port 8000

# Same as `dev`, but every OAuth redirect URI, BACKEND_URL and the cookie flags are
# repointed at the tunnel for this process only -- .env is never edited, because
# pydantic-settings ranks real env vars above the .env file. Required for Instagram
# Login; a half-moved config fails with `invalid_state` (see the script's header).
dev-tunnel:         ## API + https tunnel, OAuth config auto-pointed at it
	@./scripts/dev_tunnel.sh

tunnel:             ## https tunnel on its own, without the API
	@D=$$(./scripts/ngrok_domain.sh); ngrok http 8000 $${D:+--domain=$$D}

worker:             ## background worker -- without it saves stay `pending`
	uv run arq app.queue.worker.WorkerSettings

api:                ## production-style API: no reload, bound to all interfaces
	uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 \
		--workers $${WEB_CONCURRENCY:-1} --proxy-headers --forwarded-allow-ips='*'

openapi:            ## regenerate docs/openapi.{json,yaml}
	uv run python scripts/export_openapi.py

lint:
	uv run ruff check app tests

typecheck:
	uv run mypy app

test:
	uv run pytest -q

check: lint typecheck test   ## all gates

up:                 ## full stack in Docker (db, redis, migrate, api, worker)
	docker compose up --build

down:
	docker compose down

logs:
	docker compose logs -f api worker
