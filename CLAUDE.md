# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync --extra dev                          # install deps (Python >=3.11)
```

The database is hosted (Neon), set via `DATABASE_URL` in `.env`. **Redis runs in Docker and
only in Docker** (`docker-compose.yml`, container `recall-redis`, host port **6380**) — see
"The queue's infrastructure" below. The API and worker are two separate processes; the
worker is only needed for AI processing:

```bash
uv run alembic upgrade head                         # migrate (needs Postgres w/ pgvector)
uv run uvicorn app.main:app --reload                # API   -> :8000, docs at /docs
uv run celery -A app.queue.celery_app.celery_app worker   # worker -- needs Redis
uv run celery -A app.queue.celery_app.celery_app beat     # sweeper for lost callbacks
make redis                                          # the broker (Docker); `make dev` does this
```

**In development, start the stack with `make dev` or `make dev-tunnel` — they bring the
worker up themselves.** The worker used to be a second terminal someone had to remember,
and a worker that has to be remembered is a worker that is sometimes not running. The
symptom reaches a real person: the webhook accepts the update, `is_stalled()` sees a
non-empty queue with nobody consuming it, and the sender is told "my processing service
is restarting". That message is correct — the update is durable and gets answered when a
worker returns — but the cause is almost always just that nothing started one.

`scripts/dev_worker.sh` also runs the worker under `watchfiles`, so it **reloads on code
change** like `uvicorn --reload` does. Without that the API ran new code while the worker
ran old, which is silent and worse than a crash: the bot still answers, just with the
logic you thought you had replaced. Both dev runners clean the worker up through the
*same* trap that stops the tunnel — `trap ... EXIT` replaces rather than appends, so a
second trap would orphan the first process.

Both runners also start **Flower**, Celery's web UI, and print its link in the terminal
next to the tunnel URL — worker liveness, queue depth, task history and per-task
tracebacks. `make flower` runs it alone against a stack someone else started. It is a
*dev extra*, never a runtime dependency: nothing in `app/` imports it, and it must not
ship with the service. **It binds 127.0.0.1 and is deliberately not published through the
tunnel** — it renders task arguments, which here include Telegram chat ids, and it has no
authentication in front of it. Its own `/api/*` endpoints answer 401 unless
`FLOWER_UNAUTHENTICATED_API` is set; leave it unset, the browser UI does not need it.
A worker that is simply not running is invisible from the app and a glance here.

## The queue's infrastructure

**Redis is a container and nothing else.** `docker-compose.yml` owns it; `make redis` brings
it up and waits for the healthcheck, and `make dev` / `make dev-tunnel` do that themselves
before starting the worker — a broker that has to be remembered is one that is sometimes not
running, and a Celery worker with no broker *does not fail*: it retries the connection
forever, quietly, looking exactly like a healthy worker while every capture sits at
`pending`.

**Host port 6380, not 6379.** Another project on this machine owns 6379; binding over it
would either refuse to start or silently share a keyspace with an application that knows
nothing about ours. Because the instance is now dedicated, `REDIS_URL` uses db **0**. If a
host Redis is still listening on 6379, `scripts/redis_up.sh` says so — "why is my queue
empty" is usually "something is pointed at the other one".

Three settings in the compose file are correctness, not tuning:

- **`--maxmemory-policy noeviction`.** Any `allkeys-*` policy lets Redis delete keys of its
  own accord under pressure, and on a Celery broker those keys *are* queued tasks: a capture
  acknowledged to the user and then silently never processed, with nothing anywhere to say
  so. With `noeviction` the **producer** gets an error instead, which `VaultService._enqueue`
  already handles fail-soft — the row stays `pending` and `sweep_stranded_items` re-queues
  it. Never "fix" an OOM error here by adding an eviction policy.
- **`--appendonly yes`** (with RDB off). The broker holds user captures between "saved" and
  "processed"; a restart with only snapshots drops whatever was queued since the last one.
  This was verified the hard way — see below.
- **`--auto-aof-rewrite-min-size 64mb`.** Without a floor Redis rewrites the AOF at 100%
  growth, which on a near-empty broker fires constantly (observed: *"rewriting of AOF on
  84224304% growth"*). Every rewrite **forks**, and a fork is where resident memory can
  briefly approach double the dataset.

**That fork is why the container limit is 3× `maxmemory`, not 2×.** A 2× limit was measured
killing the broker mid-rewrite: the cgroup SIGKILLs the process, so there is no shutdown line
in the log and `docker inspect` reports `OOMKilled=false` afterwards — it presents as a
mystery restart. Redis recovered every key from the AOF, which is the other half of why that
setting is not optional. `scripts/autoscale.sh` keeps the multiple.

**What actually scales, in the order it reacts:**

| Layer | Mechanism | Driven by |
|---|---|---|
| Worker processes | Celery `--autoscale=MAX,MIN` | Celery itself, per second |
| Redis memory | `CONFIG SET maxmemory` + `docker update --memory` | `scripts/autoscale.sh` |
| Worker containers | `docker compose up --scale worker=N` | `scripts/autoscale.sh`, from queue depth |

`maxmemory` is a **ceiling, not an allocation** — Redis grows into it as tasks queue and
frees as they are consumed, so the interesting question was never "does it grow" but "does
it give the memory back". `--activedefrag yes` is what makes it do so: without it jemalloc
holds fragmented pages, `used_memory_rss` stays at the high-water mark, and the broker looks
like it never shrinks even though the queue drained. The autoscaler uses **separate up and
down thresholds plus consecutive quiet ticks**, because a single threshold oscillates around
itself and resizes on every gap between bursts.

**Redis is deliberately not scaled horizontally.** Redis Cluster is the only way to add Redis
capacity, and kombu — what Celery speaks to a broker through — does not support cluster mode.
A clustered broker is not a bigger queue, it is a broken one. This workload is a handful of
ops per capture; if one instance ever genuinely saturates, the move is a dedicated broker
host, not shards.

`make workers` runs the containerised worker and beat (compose profile `workers`) for
scaling out. **Do not run it alongside `make dev`** — two pools on one queue is not more
throughput, it is two things claiming the same messages. And never `--scale beat`: it is a
scheduler, and two of them fire `sweep_stranded_items` twice, racing two verdicts onto one row.

Quality gates (CI is not wired up — `.github/workflows/` is empty, so run these by hand):

```bash
uv run ruff check app tests                         # line-length 100, rules E,F,I,UP,B,ASYNC
uv run mypy app                                     # strict = true
uv run pytest -q
uv run pytest tests/extractors/test_extractors.py::test_generic_url_falls_back_to_article   # single test
```

Baseline as of this file: `mypy` is clean (138 files) and `pytest -q` is 1075 passed / 104 skipped
(the skips are every DB-backed test — see below), but `ruff check` reports **28 pre-existing
errors** — 26 `UP045` (`Optional[X]` instead of `X | None`, mostly in `app/models/vault.py`) and
2 `E501`, all in `app/models/` and all `--fix`-able. Do not read a red ruff run as damage you
caused; check whether your files are among the offenders.

**No test may reach an AI provider.** `.env` carries a real key on a developer machine, so a
code path that reaches `get_chat_model()` (or the enrichment client) without a test having
stubbed it does not fail — it *works*, slowly, over the network, and bills per run while the
suite still passes green. That happened twice while the tool lane and combined enrichment were
being added. `tests/conftest.py`'s autouse `_no_provider_calls` closes every one of them: it
patches `factory.get_chat_model` / `build_chat_model` / `fallback_models`, the copy
`ai/chat/tools.py` imported by name, both the switch and the client for combined
enrichment, relation typing, and the **connection judge**. **A new outbound AI capability
must be added there in the same commit.** A suite that suddenly takes 40s instead of 10s is
the symptom. The judge is the fourth time that rule has been paid for, and the first that
ships with its switch **on** -- a derivation test that did not know about it would reach a
provider on any machine with a key, rather than merely failing to.

`tests/conftest.py` needs a real PostgreSQL (pgvector `Vector`, JSONB, `PGUUID`, GIN/HNSW
indexes rule out SQLite). It skips every DB test when none is reachable, so `pytest -q` stays
green without Docker -- **a green run does not mean the authz suite ran**. To run it for real:

```bash
# Any Postgres with pgvector. A Neon branch is the cheapest separate target.
export TEST_DATABASE_URL='postgresql+asyncpg://USER:PASS@HOST/recall_test?ssl=require'
make test
```

**`TEST_DATABASE_URL` must not be the live database.** The engine fixture runs `drop_all` and
every test truncates, so `conftest` raises at import if its host and database match
`DATABASE_URL` — it raises rather than skips, because a silent skip is how someone ends up
pointing it at production to "make the tests run". Fixtures seed rows directly rather than
through capture endpoints, because those enqueue Celery jobs and would need Redis.

Tests mirror the `app/` package tree -- `tests/auth`, `tests/vault`, `tests/extractors`,
`tests/processing`, `tests/ai`, `tests/integrations`, `tests/core` -- so a new test's home is
never a judgment call. Each folder needs an `__init__.py` (`tests` is a package; without one,
two same-named test modules in different folders collide on import). **There is exactly one
`conftest.py`, at `tests/`**, and it should stay that way: a fixture buried in
`tests/vault/conftest.py` is invisible from the test that inherits it, which is how a suite
becomes unreadable. `pytest tests/auth` runs one domain.

`pytest` runs with `asyncio_mode = "auto"` — do not add `@pytest.mark.asyncio`. There is no
`conftest.py` and no DB/Redis fixtures: the existing tests are pure and offline (extractor
routing, Gemini response parsing). Anything needing a database has no harness yet.

**Every new migration must be idempotent.** `0001_initial` calls
`SQLModel.metadata.create_all()`, so it builds the schema from the *current* models rather than a
frozen snapshot: on a fresh database it already creates whatever the latest models declare, and an
unguarded `ADD COLUMN` / `CREATE TABLE` in a later revision then aborts the entire upgrade (Alembic
wraps it in one transaction, so the DB rolls back to empty). Guard with
`sa.inspect(op.get_bind())` — see `auth/0004_oauth_accounts` and
`integrations/0005_instagram_accounts`.

**Revision files sit in per-domain folders, but they are still ONE linear chain.**
`migrations/versions/{core,auth,vault,integrations,processing}/` groups them for reading only —
order comes from each file's `down_revision`, never from the folder, and `alembic history` is the
only authority on it. The numeric filename prefix is kept so a sorted listing still shows the
sequence. That layout works because `alembic.ini` sets **`recursive_version_locations = true`**;
without it Alembic scans `versions/` one level deep, finds **zero** revisions, and `alembic
upgrade head` reports nothing to do — a silent no-op that looks exactly like an up-to-date
database. If migrations ever appear to vanish, check that flag first.

New migration: `alembic revision --autogenerate -m "..."` writes to `migrations/versions/` (the
root), then **move the file into the domain folder it belongs to** — the chain follows it, since a
revision is identified by its `revision` id and not by its path. Do not pass `--version-path`: it
only accepts a directory listed in `version_locations`, and listing the folders there while
recursion is on makes Alembic load every file twice. Do not list the domain folders in
`version_locations` either — a folder added later but not registered would be skipped in silence,
which is the one failure mode worth engineering against here.

`alembic.ini` leaves `sqlalchemy.url` empty on purpose — `migrations/env.py` injects it from
`settings.database_url_str` and imports `app.models` so `SQLModel.metadata` is populated. A model
that is not re-exported from `app/models/__init__.py` is invisible to autogenerate.

## Architecture

Everything saved is one **`VaultItem`** row discriminated by a `type` enum (`ContentType`) — there
are no per-platform tables. Adding a source means adding an extractor, never a table or a route.

Saving is deliberately two-phase and never blocks on AI:

```
POST /vault/save -> VaultItem(status=pending) -> enqueue "process_item" -> 201
                                                      |
   worker: get_extractor(url) -> extract -> summary -> tags -> category -> embedding -> completed
```

**Long extractions are fire-and-forget.** An Apify crawl can run for minutes, so
`ProcessingService` is two-phase: `process` calls `DeferredExtractor.start`, writes an
`extraction_runs` correlation row and *returns* — the worker is free while the provider works.
Apify then POSTs `/webhooks/apify/{secret}`, which queues `finalize_run`; that re-reads the run's
real status and dataset from Apify with our own token (the callback body is a signal, never data)
and finishes the item. A Celery beat task sweeps runs that never called back, so a lost webhook
degrades to a delay rather than an item stuck in `processing` forever. Fast extractors
(article, YouTube) stay single-phase — deferring a 300ms fetch would buy nothing.
`processing_status` deliberately does not gain a `scraping` value: it is a PG enum, and reusing
`processing` keeps the frontend's polling contract unchanged.

`ProcessingService.process` (`app/services/processing_service.py`) is the whole pipeline and the
only place that mutates `processing_status`. On any exception it records `processing_error`,
increments `retry_count`, and **re-raises** so Celery retries (`max_retries = 3`,
`task_time_limit = CELERY_TASK_TIME_LIMIT`).
`queue/tasks.py` must commit on that path too — it previously only committed on success, so the
failure bookkeeping rolled back and a permanently failing item sat at `pending` forever while the
UI polled it. It also re-raises a plain `RuntimeError`: provider SDK errors carry a live HTTP
client and Celery's result serialization chokes on them. Celery runs prefork workers with an
`asyncio.run` bridge per task, which is why DB work goes through `db.session.task_session()` and
its NullPool — the shared module-level pool would hand the next task a connection whose event
loop is already closed.
Tenacity retries in `ai/gemini.py` use `reraise=True` so the stored error is the provider's own
message rather than an opaque `RetryError[<Future ...>]`.

Layering is strict and dependency-inverted — `api -> services -> repositories -> models`:

| Layer | Rule |
|---|---|
| `api/` | Thin. Routers translate HTTP <-> schemas; all wiring via `app/api/deps.py`. |
| `services/` | Use-cases. No SQL, no HTTP objects. |
| `repositories/` | All queries. Sessions come in via constructor, never created here. |
| `extractors/` | Per-source logic lives ONLY here — never in routes or the worker. |
| `ai/` | `AIProvider` Protocol; business code never imports `GeminiProvider` directly. |

Swap points: `ai/factory.py` chooses the provider from `settings.AI_PROVIDER` (`gemini` and
`openai` are implemented; `claude` is declared in the Literal but not built);
`extractors/registry.py` picks by URL. **`_EXTRACTORS` order matters** — `ArticleExtractor` is the
catch-all and must stay last, so new extractors get appended *before* it.

## Invariants worth knowing before editing

- **Boot guards** (`app/core/config.py`): outside `ENV=dev`, `validate_deployment_config` refuses
  to start on a placeholder/short `SECRET_KEY`, on `COOKIE_SECURE=false`, or on a `"*"` entry in
  `CORS_ORIGINS` (the CORS middleware runs with `allow_credentials=True`, and Starlette answers a
  wildcard by echoing the caller's own Origin). Under `ENV=prod` it additionally requires every
  CORS origin to be `https://` and non-local, and `/docs`, `/redoc`, `/openapi.json` are withheld. The guard is a plain function called from
  `get_settings()`, **not** a pydantic validator — pydantic embeds the full settings input in
  `ValidationError`, which would print live secrets from the environment into crash logs. Keep any
  new config check out of the model for the same reason, and never interpolate a secret's value
  into an error.
- **Config**: `app/core/config.py` is the only place that reads the environment. Never call
  `os.environ` elsewhere. **There is no `.env.example`** — `.env` is the only env file, it is
  gitignored, and the field list lives in the `Settings` model. Do not recreate a template.
  Editing `.env` needs an API restart, not a `--reload` (the reloader watches `.py` only, and
  `settings` is `lru_cache`d at import). `settings` is an `lru_cache`d singleton imported at module scope, so
  changing env vars mid-test has no effect.
- **`DATABASE_URL` is normalised by `database_url_str`, and that is the only URL anything
  connects with** — API, worker and `migrations/env.py` alike, which is why the rewrite
  lives on the setting rather than beside one engine. A provider's copy-paste URL is
  *libpq*: `postgresql://` selects psycopg2, which is not installed (SQLAlchemy resolves
  the driver from the URL, not from `create_async_engine`), and `sslmode` /
  `channel_binding` are passed through to `asyncpg.connect()`, which has neither keyword.
  So the scheme becomes `postgresql+asyncpg`, `sslmode` is renamed to asyncpg's `ssl`
  (same modes, so the mode itself survives) and `channel_binding` is dropped. TLS is
  translated, **never invented from the host** — instead, outside dev the boot guard
  refuses a URL that did not ask for it, because asyncpg's default `prefer` falls back to
  plaintext silently. Nothing may interpolate the URL into an error: it holds the password.
- **Tenant scoping lives in the repository.** `VaultRepository.get()` takes a `user_id` and returns
  `None` on mismatch; `get_unscoped()` deliberately skips that check and exists *only* for the
  worker, which has no request user. Never reach for `get_unscoped` in an API path.
- **Transactions**: `get_session` commits once when the request ends, so services should `flush`
  (via `repo.add`) and let the boundary commit. The worker owns its own session and commits
  explicitly in `queue/worker.py`.
- **Auth** is a signed-JWT session cookie (`recall_session`), decoded in `deps.get_current_user`.
  There are no passwords — identity comes from OAuth (Google, Facebook, Instagram). X/Twitter is
  parked in `docs/parked/twitter_oauth.py`, deliberately outside `app/` so it stays out of
  `mypy app`; its docstring lists the five edits that re-enable it.
- **A login is TWO cookies, and only one of them is a JWT.** `recall_session` is a 15-minute
  access token (`Path=/`, verified by signature alone — nothing queries the database); the
  session itself is a `user_sessions` row addressed by `recall_refresh`, a 7-day opaque token
  scoped to `Path=/api/v1/auth`. `POST /auth/refresh` is what makes a user who returns days
  later skip the provider. Consequences worth knowing before editing:
  - **Only the digest is stored** (`token_hash`, SHA-256). A fast hash is correct here because
    the input is 48 random bytes, not a password — there is no dictionary to run, and the
    lookup has to be one indexed equality match.
  - **Refresh tokens are single-use.** Each refresh writes a new row in the same `family_id`
    and retires the old one (`revoked_reason="rotated"`). Retired rows are kept on purpose:
    a token presented after rotation means two parties hold it, so the whole family is revoked
    (`reuse_detected`). Never "fix" a double-refresh 401 by making rotation idempotent — that
    deletes the only theft signal the system has.
  - **The window slides, the chain does not.** Every rotation extends expiry by
    `REFRESH_TOKEN_EXPIRE_DAYS`, but `family_started_at` is copied forward and
    `REFRESH_TOKEN_ABSOLUTE_DAYS` (90) ends the chain regardless of activity.
  - **Revocation is not instant.** Nothing checks the database while an access token is valid,
    so `logout-all` takes effect within `ACCESS_TOKEN_EXPIRE_MINUTES`. That is why the boot
    guard caps it at 60 outside dev — raising it widens the hole, it does not "reduce load".
  - `get_current_user` answers `"Session expired"` for an expired signature and
    `"Invalid session"` for everything else; the SPA uses that to decide whether to try
    `/auth/refresh` before giving up. `/auth/refresh` itself collapses every rejection into one
    opaque 401 — a distinguishing error tells a token holder whether their token was ever real.
  - State-changing auth routes call `_assert_same_site`. SameSite=lax covers the default
    deployment; the Origin allowlist is what still covers a cross-site one that must set
    `SESSION_COOKIE_SAMESITE=none`.
  - `purge_expired_sessions` (beat, daily) deletes rows strictly by `expires_at` — never by
    `revoked_at`, since a revoked-but-unexpired row is exactly the replay evidence.
- **Adding an OAuth provider means adding a module, never a route.** `app/api/v1/auth.py` has
  exactly one `/{provider}/login` + `/{provider}/callback` pair; `services/oauth/registry.py`
  resolves the name. A provider is only offered when both its client id and secret are set, and
  `get_oauth_provider` returns `None` for unknown *and* unconfigured names so a probe cannot tell
  them apart. `/auth/providers` is what the frontend renders buttons from.
- **Account linking by email requires `email_verified`.** `AuthService._resolve_user` links a new
  provider to an existing user only when the provider asserts the address is verified; otherwise
  it creates a separate user. Without that gate, an account at a provider that lets you type any
  email is a takeover of the victim's Google-created account. X returns no email at all, and Facebook omits it for phone-only accounts, so
  those users get a synthetic address on `users.noreply.recall.invalid` — `is_placeholder_email`
  recognises it, and the frontend shows the provider name instead. With X parked, the only
  live case is a phone-only Facebook account.
- **Not every failure should be retried.** Celery retries a task three times, which is right for a
  timeout and wrong for a deleted post or a rejected API key — those spend four paid actor runs to
  reach the same answer. Extractors raise `PermanentExtractionError` (`extractors/base.py`) for
  those, and `ProcessingService` records the failure and *returns* instead of re-raising, so Celery
  stops. Anything unrecognised stays retryable on purpose.
- **Pasted Instagram links are scraped by Apify, making three Instagram paths in total.**
  `extractors/instagram.py` reads any *public* post or reel someone pastes and needs no user
  connection — Instagram serves a login wall to server-side fetches, so the generic article
  fetcher returns a page titled "Instagram" with zero characters. The extractor claims the URL
  even when `APIFY_TOKEN` is unset and fails naming the setting, because falling through to that
  empty page looks like success. It only assembles text and metadata; the summary, category and
  memory tags still come from `ProcessingService` via the `AIProvider`. Profile and story URLs are
  deliberately not claimed. Reels contribute caption, hashtags, mentions, audio and top comments —
  the *video itself* is not watched; `metadata.video_url` is stored for whenever that is added.
- **Facebook reels are free, and unlike Instagram they need no scraper.** Facebook answers a
  *non-browser* User-Agent with the full share preview: `og:title` carries the entire caption
  (`og:description` is truncated at ~200 chars, so it is only the fallback), `og:url` the
  canonical reel URL and the page slug. A Chrome-looking User-Agent gets **HTTP 400** — do not
  "fix" the header block by disguising it. `og:title` arrives as
  `"<stats> | <caption> | <page name>"`; the stats prefix is parsed into
  `metadata.views/reactions`, and the trailing page name is only trimmed when it matches the
  slug from `og:url`, since a caption may contain " | " itself. `share/r/<code>` shortlinks need
  no special case — the redirect loop resolves them, re-validating each hop through
  `assert_safe_url`. `Accept-Language: en-US` is not cosmetic: without it the engagement counts
  come back in the exit node's locale and the stats prefix stops parsing.
  `FacebookReelApifyExtractor` is the paid fallback and claims **nothing** unless
  `FACEBOOK_USE_APIFY=true`; turning it on also means replacing `APIFY_FACEBOOK_ACTOR`, because
  the first-party `facebook-reels-scraper` takes a *page* URL and walks its reels — handed a
  single reel link it returns an empty dataset, which surfaces as "may be private, deleted".
- **There are TWO Instagram integrations and they are not the same thing.** Sign-in
  (`services/oauth/instagram.py`, Instagram Login) establishes identity and uses the
  **Instagram** App ID/Secret; the connection (`services/instagram_service.py`, Facebook Login)
  links a Business account to an existing user and uses the **Facebook** App ID/Secret. Settings
  are named `INSTAGRAM_APP_ID` / `INSTAGRAM_LOGIN_*` vs `INSTAGRAM_CONNECT_*` for exactly this
  reason. Pointing one at the other's credentials fails at Meta with an unhelpful error.
- **The browser must reach the API on the same origin as the frontend.** `make dev-tunnel`
  therefore tunnels *Next* (:3000), not uvicorn, and Next rewrites `/api/*` to :8000. Tunnelling
  the API directly and leaving the SPA on localhost makes the session cookie third-party, which
  Chrome strips: login succeeds, then every request is anonymous and the app loops back to
  sign-in. `SameSite=None` does not rescue it -- browsers are removing the cross-site hop itself.
- **Instagram Login rejects plaintext redirect URIs.** `INSTAGRAM_LOGIN_REDIRECT_URI` must be
  `https://`, so local development needs a tunnel; there is deliberately no localhost default,
  and `is_configured()` requires the URI so the button stays hidden until it is set. Two other
  traps: the returned `code` carries a literal `#_` suffix that must be stripped before the token
  exchange, and the short-lived token must be traded for the 60-day one via
  `graph.instagram.com/access_token?grant_type=ig_exchange_token`.
- **Instagram is a connection, not a login.** `/api/v1/integrations/instagram/*` attaches an
  Instagram Business account to an *already authenticated* user; it never creates one. The
  permissions (`instagram_basic`, `pages_show_list`, `business_management`) are deliberately kept
  out of `FACEBOOK_SCOPES` — they need Meta App Review, and asking for business-Page access on a
  sign-in screen is both a conversion disaster and more authority than logging in needs.
- **Instagram reads use the Page token, not the user token.** Instagram Basic Display died in
  Dec 2024, so the only path is the Graph API: the IG account must be Business/Creator and linked
  to a Facebook Page, and `/me/accounts` yields that Page's own token. Page tokens minted from a
  long-lived (60-day) user token do not expire, which is what makes background ingestion possible.
  `instagram_accounts` stores both, encrypted, and `schemas/integrations.py` has no field for
  either — a Page token must never reach the browser.
- **The Instagram callback's state cookie is bound to the user id that started the flow**
  (`{user_id}.{nonce}`). Without that, an attacker could complete their own consent and lure a
  victim to the callback URL, grafting their Instagram onto the victim's account. Every branch —
  no cookie, no state, wrong nonce, different user — fails closed; `tests/integrations/test_integrations_instagram.py`
  pins each one.
- **The bot routes on the SHAPE of a message, never on guessed intent.** A link or a
  file is captured immediately; `/note <text>` is the only way plain text becomes a
  memory; everything else is answered by the chat model and **stored nowhere**
  (`services/telegram/dispatch.py`). The old default -- unrecognised text becomes a note
  -- was the right call only while there was no explicit way to keep a thought; with
  `/note` it just accumulates greetings the user discovers in bulk much later. The
  failure mode now is retyping something with `/note` in front, which is visible and
  recoverable. Everything that is not a capture, a command or a status
  question now takes **one** lane -- the agent -- because the split that used to exist
  guaranteed one of its two halves could not see the vault. `chain.answer`'s rule is
  still "speak only from the blocks", and it holds because that lane is never handed an
  empty context: every prompt carries a capability card and a snapshot of the newest
  saves. `first_url` beats phrasing, so "what is this? <link>" saves rather than asks.
- **"Did that save?" is answered by the database, and never by the model.** The
  conversation lane is given no memories by design, so asked whether a capture succeeded
  it answered the only honest thing it could -- *"I can't check what you've saved"* --
  while the row sat there `completed`. That is not a prompt bug and no prompt fixes it:
  the fix is a lane with **no provider call in it at all**. `router.Intent.STATUS`
  matches save-confirmation phrasing and `chat_engine/status.py` reads the newest rows
  through `VaultService.recent_saves` and reports `pending`/`processing` as "still
  processing", `completed` as saved, `failed` as failed and `skipped` as saved-without-a-
  summary. Five things go with it:
  * **STATUS is checked BEFORE RECALL**, and every pattern is anchored to the start of
    the message. "did i save it" carries a retrieval phrase and is not a retrieval
    question -- there is no subject, only an outcome -- while "did i save the perfume
    link" names what to look for and must stay a search. Anchoring is what separates
    them, and it also means no pattern here can backtrack across an unbounded body.
  * **The anaphor is the whole signal.** "it", "that", "this", "my last one" point at
    something the conversation already has, which is always the most recent capture.
  * **The wording is a script-keyed table**, for the same reason `_NO_MATCH` in
    `recall_chat` is one: with no model call there is nothing in the loop that could
    translate the reply. Times are **relative** ("2 min ago") because nothing on a chat
    surface carries the sender's timezone, and an absolute time would be this server's
    opinion rendered as the user's.
  * **`processing_error` is never echoed.** It is scrubbed on the way into the database
    and is still a provider's phrasing about our infrastructure; the user needs the
    outcome and what to do, not the stack.
  * **The lane survives a missing chat model.** `ChatEngine` takes `recall: RecallLanes |
    None` and answers `chat_unavailable` itself for the lanes that need a provider, so a
    deployment with no chat model still answers "is it saved?". With no vault reader
    wired up STATUS degrades to *retrieval* -- which answers from the vault or says it
    found nothing, and is the safe direction. (The no-vault conversation lane it used to
    be contrasted with no longer exists; retrieval is the only lane left below the
    agent.)
  `/status` is the same lane reachable by typing, which matters because the phrase list
  is English-first.
- **The RECALL lane can run its own searches, and that is the only place a model chooses
  what happens next.** `ai/chat/tools.py` binds three tools -- `SearchMemories`,
  `ListMemories`, `GetMemory` -- and `services/chat_engine/toolbox.py` is what executes
  them. It buys the question one planner call cannot express: "did I save the docker talk,
  and what did the speaker claim?" is find-then-read, and a single `MemoryQuery` picks one
  of the two. Six rules hold the boundary, and none of them is a precaution:
  * **There is no write tool, and there must not be.** Tool results are scraped captions
    and page text -- exactly the text an attacker gets to write -- so a `save_memory`
    bound to a model reading it is one caption away from filing something the user never
    asked for. Capture stays in the regex `CAPTURE` lane, which no message can argue with.
  * **`user_id` is not a tool argument.** It is fixed on the toolbox by the caller that
    resolved the account, so prompt injection has nothing to *ask* another tenant's rows
    with, and the repository re-applies the predicate underneath. `tests/chat_engine/
    test_toolbox.py` asserts no schema has such a field.
  * **`GetMemory` only accepts ids already surfaced this turn.** Not secrecy -- a short id
    is a prefix of a UUID its owner has -- but a model told by a *memory* to open
    something did not get that id from the vault.
  * **`evidence.assess` runs on every search** before its results are rendered, and
    `validate_answer` still runs on the way out against the ids the toolbox actually
    surfaced. The model chose the query; it does not choose what counts as a match.
  * **The loop is bounded** (`RECALL_MAX_TOOL_CALLS`, `RECALL_MAX_TOOL_ROUNDS`), and every
    tool call gets a `ToolMessage` even past the budget -- a call left unanswered is a
    malformed conversation and providers reject the *next* request outright.
  * **Failure degrades to the single-shot path**, which is older and better tested. That
    is what makes `RECALL_TOOLS_ENABLED` safe to default on.
- **Enrichment is one call now, not four, and the four that remain run together.**
  `ai/enrichment.py` asks for summary, tags, category and label in one OpenAI structured
  output (`strict` JSON schema), which is where the cost actually was: each per-field
  prompt shipped the whole 12000-character item again, so four calls billed roughly four
  times the input to produce about 120 tokens. The schema also removes the string
  handling that recovered tags from prose and mapped an unrecognised category to "Other".
  It is a module with its own switch rather than a fifth `AIProvider` method, for the same
  reason `transcription.py` and `vision.py` are: the Protocol is structural, so a provider
  missing a method fails at runtime inside the pipeline and every fake in `tests/` has to
  grow it. When it is off or fails, `ProcessingService._card_fields` falls back to the four
  `AIProvider` calls -- issued with `asyncio.gather`, since none of them reads another's
  output. Highlights stay separate on purpose: they are verbatim quotes checked against
  the body afterwards and they need the full `content`.
- **A second configured provider is a failover, never a fallback for selection.** An
  `AI_PROVIDER` that cannot be built still raises -- answering with a model the operator
  did not choose is not a recovery. But when both keys are present, `factory.resilient()`
  rebuilds the caller's chain on the alternate and tries it once. Three limits:
  **chat only, never embeddings** (Gemini's 768 padded dims and OpenAI's native 1536 are
  not comparable, so a query embedded by the alternate ranks against a space it was not
  drawn from); **only a provider with a key**, because a fallback that provisions itself
  is a bill nobody agreed to; and the primary always goes first, so a healthy deployment
  never touches it. `get_chat_model()` deliberately returns a bare `BaseChatModel` --
  `with_fallbacks` produces a `RunnableWithFallbacks`, which has neither `bind_tools` nor
  `with_structured_output`, so wrapping in the factory would silently remove the two
  features the tool lane and the planner are built on.
- **`POST /chat/ask` streams, and streaming did not cost the output checks.** The bot
  answers into a window nobody watches type; a web page is the opposite. But "correct it
  afterwards" is not available here -- the whole point of the URL rule is that a
  fabricated link is one a person is invited to *tap*. `validation.StreamValidator` works
  because both checkable things, `[a3f1c920]` and a URL, contain **no whitespace**: it
  releases only up to the last whitespace it has seen and holds the trailing run back, so
  everything displayed has been seen whole and put through the same `scrub` the finished
  path uses. It also enforces the length cap as it goes and collapses the gap a removal
  leaves, so the streamed and finished answers are identical strings
  (`tests/chat_engine/test_stream_validation.py` asserts that directly). The route carries
  `assert_same_site` and a per-user hourly cap (`ASK_PER_HOUR`, `core/rate_limit.py`)
  because it spends a model call per request, and its request body has exactly two fields
  -- everything deciding *which rows are read* comes from the session.
- **The streamed tool lane is driven by a graph, and that is the only reason LangGraph is
  a dependency.** A loop over `ainvoke` must receive a whole turn to know whether it was
  tool calls or the answer, so the surface that most needs words as they arrive was the
  one that could not have tools. `ai/chat/agent.py` uses `create_react_agent` with
  `stream_mode="messages"`; checkpointers, interrupts and persistence are deliberately
  unused, since this graph answers one question and ends. Two things to know before
  editing it: a `ToolMessage` chunk carries the fenced memory blocks and **must never be
  emitted** (it would put a raw `<memory>` block on the page, and text the model has not
  read yet in front of the person as though it were the answer); and a failure *before*
  any words is silent so the caller can answer by the single-shot route, while a failure
  *after* them is final -- repeating words the reader has already seen is worse than the
  shorter answer they have. The status events it emits carry the tool's stage and never
  its arguments, which are the model's own guess at the subject.
- **The Telegram bot's whole authorisation is one lookup.** An update carries no session,
  so `TelegramUpdateRouter` (`services/telegram/dispatch.py`) turning a sender into a user
  via `telegram_accounts` *is* the access control — everything before that lookup succeeds
  must not touch the vault. Two rules go with it: **private chats only** (a bot added to a
  group would read one member's vault aloud to the room), and an unlinked sender learns
  nothing — not a count, not a title, only how to connect.
- **`telegram_accounts.telegram_user_id` is unique GLOBALLY, not per user.** Scoping it to
  `(user_id, telegram_user_id)` would let a second account claim a Telegram identity
  someone else already linked, and every later message from that chat would resolve to
  whichever row was found first. `TelegramLinkService.consume` refuses rather than
  rebinding, and spends the token on the way out so a refusal leaves nothing to retry with.
- **Link tokens are single-use, hashed, and every rejection reads the same.** The raw value
  exists only inside the `t.me/<bot>?start=<token>` deep link; `telegram_link_tokens` stores
  the SHA-256 (fast hash is right — 32 random bytes, no dictionary, one indexed lookup).
  Unknown, spent and expired all answer "that link expired": distinguishing them tells
  whoever found a link in a screenshot what they are holding. Minting a link expires the
  user's previous one — pressing Connect twice must not leave a live credential behind.
- **The webhook is gated twice, and never answers 4xx to a real update.**
  `POST /webhooks/telegram/{secret}` compares the secret in the path *and* in Telegram's
  `X-Telegram-Bot-Api-Secret-Token` header, both with `compare_digest`, so a leaked URL is
  not enough. Anything we cannot act on is acknowledged with 202: Telegram redelivers
  non-2xx with the same bytes, so one 500 on a malformed update is an infinite retry loop.
  **`/api/v1/webhooks/` is exempt from `RateLimitMiddleware`** for the same reason — the
  limiter keys on client IP, all of Telegram's traffic shares one key, and a 429 is a
  delivery failure it retries. The real cap is per-`telegram_user_id` in Redis
  (`services/telegram/limits.py`), applied in the worker where the sender is known.
- **The reply address is re-derived, never read from the item.** `deliver_telegram_result`
  looks the chat up through `telegram_accounts` by `item.user_id`.
  `item_metadata["telegram_chat_id"]` is recorded for debugging only — trusting it would let
  a poisoned or mis-written metadata value route one user's content into another's chat.
  `item_metadata["source"] == "telegram"` is what triggers a reply at all, checked in
  `queue/tasks.py` after the commit rather than inside `ProcessingService` (the pipeline has
  no business knowing which surface a save came from).
- **The bot commits before it enqueues.** `VaultService.save_url` / `save_document` /
  `create_note` take `enqueue=False` so `_handle_telegram_update` can commit its
  `task_session()` first. On the web the enqueue-before-commit race (below) is a rare stuck
  card; for the bot it means the completion reply never fires and the user watches an
  acknowledgement that never resolves.
- **Replies are Telegram HTML, never MarkdownV2.** MarkdownV2 makes 18 characters special
  including `.` and `-`, so one unescaped article title returns HTTP 400 and the user gets
  nothing. HTML needs only `& < >`; `services/telegram/formatting.escape` wraps every
  interpolated value, because titles, tags and categories are model output derived from
  scraped pages. **Nothing in `services/telegram/client.py` may log a request URL** — the
  bot token is in the path, and `log_sink` redacts by key, not by value.
- **Telegram photos arrive with no filename**, and `documents.inspect` reads the extension
  from the filename only, so an un-synthesized name is a guaranteed `DocumentError`.
  `capture.py` derives it from the `getFile` path's suffix, then from the declared MIME
  type; the magic-byte check still decides what the bytes actually are. Telegram voice
  messages are still refused out loud — ASR now exists (`services/transcription.py`) but
  nothing on this surface calls it, and a silent drop reads as a bug. Wiring it means
  `getFile` on the `voice`/`audio` payload and a call to `save_voice_note`; the audio MIME
  types deliberately stay out of `documents._ALLOWED`, which is the *document* allowlist.
- **LangChain is confined to `app/ai/chat/`.** Routers, services and Celery tasks reach it
  only through `services/recall_chat.py`, the same way business code never imports
  `GeminiProvider`. It supplies the multi-turn chat model, `with_structured_output` for
  query planning, and the prompt/LCEL plumbing that the four-method `AIProvider` Protocol
  has no room for. **Embeddings deliberately stay on `AIProvider`**: the stored vectors were
  written by it, and under Gemini they are 768 dims zero-padded to 1536, so a second
  embedding stack would rank against a space it was not drawn from — plausible ordering over
  noise, with nothing to notice. Chat history is ~40 lines over the existing redis client
  (`ai/chat/history.py`) rather than `langchain-redis`, which drags in a vector-store stack
  this project does not use.
- **Retrieved memories are data, not instructions.** `ai/chat/chain.py` fences each one in a
  `<memory>` block and tells the model the contents are quoted material; the chain binds no
  tools. A scraped Instagram caption saying "ignore previous instructions" is something a
  person can write on purpose. Zero hits short-circuits to a fixed sentence with no model
  call — an empty context invites the model to invent a memory, and pays for the privilege.
  A purely time-scoped question ("what did I save this week?") is answered by
  `list_filtered` alone: no embedding, no vector search, no answer model.
- **Top-k is not truth, and the filter runs before the prompt exists.** A vector search
  cannot return "nothing": `ORDER BY embedding <=> $1 LIMIT 8` hands back the eight
  least-unrelated rows however far away they are, so the old zero-hit short circuit
  caught an empty vault and nothing else. `MemoryRetriever.recall` now returns
  `RetrievedMemory(item, score)` — `score = 1 - cosine_distance`, clamped — and
  `services/chat_engine/evidence.py::assess` turns that into one of three states before
  a prompt is built. `no_evidence` (nothing cleared `RECALL_MIN_SCORE`) is answered by
  the fixed sentence with **no model call at all**, exactly like zero rows; `insufficient`
  still answers but the chain is handed `GUIDANCE_WEAK`, which tells it to say the match
  is weak rather than stretch it; `supported` is the ordinary path. Two filters, doing
  different jobs: the absolute floor is what the *embedding model's* scale decides — it
  is configuration because Gemini's similarities sit high and bunched while OpenAI's
  spread low, and a constant compiled in would be wrong for one of them with nothing to
  notice — while `RECALL_SCORE_MARGIN` is relative and provider-independent, dropping
  memories far weaker than the best hit so one strong match is not diluted by seven
  mediocre ones the model would try to connect. **Retune the floor whenever the embedding
  provider changes**, alongside the re-embed that change already requires — and *measure*
  it rather than carrying a number over. The first draft shipped Gemini's 0.55 onto an
  OpenAI (`text-embedding-3-small`) vault, where a measured true match scores 0.373 and
  noise tops out near 0.27: it would have reported items the user really had saved as
  missing. The measured distribution and the current values are in `app/core/config.py`.
- **A memory block is labelled with the item's own short id, not its position.**
  `cards.short_id` is the single definition, used by the card, by the fence in
  `ai/chat/chain.py` and by the answer validator — `"memory 3"` names a different row on
  the next question, which makes an invented citation indistinguishable from a real one.
- **The answer is checked against its evidence on the way out**
  (`services/chat_engine/validation.py`). A prompt is a request, not a constraint, and
  the answer that breaks a rule is the one nobody notices because it reads like the
  others. Deterministic checks only: a citation naming a memory that was never supplied
  is removed and logged as `recall_answer_corrected` (the clearest fabrication signal the
  system has), a URL appearing in no block is replaced (both a false claim about the
  vault *and* a link a person is invited to tap, in text derived from scraped pages), an
  empty reply becomes a failure rather than a blank message, and the length is capped.
  **The checked text is what goes into chat history** — history is replayed into the next
  prompt, so keeping the raw reply would let a fabrication come back as context and be
  built on. Verifying a *claim* ("saved on 12 August") is deliberately not attempted:
  that is claim extraction plus per-claim verification, a real design and not a regex.
  `RecallAnswer.memory_ids` carries the evidence ids so a wrong answer stays traceable;
  nothing renders them yet.
- **Every routing decision in the chat path was English-only, and that was one bug in two
  halves.** `planner.looks_like_question` matches English opening words, so "আমার নোট
  দেখাও" ("show my notes") was not a question; `scope._normalise` strips everything
  outside `[a-z0-9' ]`, so the same sentence became an empty string, which the gate read
  as an emoji -- a *reaction* -- and allowed into the conversation lane, which was given
  no memories by design. So asking about your own vault in Bengali was answered by a
  model that had never seen it, and nothing reported a problem. (Both that lane and the
  gate are gone now; the history is kept because the *shape* of the bug -- a routing rule
  that could not read the message deciding what the model was allowed to see -- is the
  one this codebase keeps re-learning.) Both halves now go
  through `app/core/scripts.py`, which counts **letters outside ASCII** -- an observable
  fact, no model, no table, no network. It is not language identification and does not
  claim to be. Three consequences:
  * **Non-Latin text longer than a greeting is a question**, so it goes to *retrieval*.
    That is the lane that can be wrong safely: with no matching memory it says "I couldn't
    find anything about that in your vault" rather than answering from general knowledge.
    A short piece of general knowledge in another script ("সানি লিওন কে", 6 letters) lands
    there too and gets exactly that answer -- the same outcome the scope gate exists for.
  * **The threshold is measured, not guessed.** Greetings sit at 2-5 letters in every
    script tried (হ্যালো 3, 你好 2, नमस्ते 4, مرحبا 5) and anything with a subject and a
    verb starts at 6. Counted in letters rather than words because Chinese, Japanese and
    Thai have no spaces -- a whitespace word count reads a whole sentence as one word.
    Bengali vowel signs are combining marks, not letters, which is another reason to
    measure rather than eyeball.
  * **The gate no longer treats "nothing left after normalisation" as friendly.** An
    emoji still is; an unreadable *sentence* is declined as `unreadable_script`. The
    router keeps those away from the gate now, but a gate that reads a Bengali sentence
    as a greeting is wrong whether or not anything depends on it today.
- **Nothing detects the reply language, so both prompts are told outright.** `_SYSTEM`
  rule 10 and `_CONVERSE_SYSTEM` rule 8: answer in the language the person wrote in,
  including declining in it -- a refusal in English to a Bengali request is unreadable to
  the person who triggered it. The answer prompt also pins that **titles, names and URLs
  stay exactly as the block spells them**: those identify a saved item, and a translated
  title is one the user cannot search for.
- **The no-match reply is a table, because there is no model call to translate it.** A
  zero-hit search short-circuits with no provider call -- an empty context is the one
  input the answer prompt has no honest response to -- so `_NO_MATCH` in `recall_chat`
  carries the sentence per **script**. That has a consequence worth stating: `devanagari`
  covers Hindi, Marathi and Nepali, so a Marathi speaker gets the Hindi sentence, and no
  amount of character counting improves on that. The table is **deliberately short** and
  anything unlisted falls back to English: a machine-translated sentence that reads as
  broken is worse than plain English at the exact moment someone's search failed. Adding
  a language is one entry, written by someone who speaks it. The subject is echoed back
  verbatim -- it is the user's own words, and translating their search term tells them
  they looked for something they did not.
- **A card's language is `ENRICHMENT_LANGUAGE`, and it defaults to English.** Summary,
  tags and `ai_label` used to follow the content unconditionally -- the reasoning being
  that a Bengali note summarised in English is a memory its author reads in translation.
  That is still true, and it is still available: `ENRICHMENT_LANGUAGE=content` restores
  it exactly. It is no longer the default, because the cost lands on the person who
  *reads* an English vault: a Bengali reel produced a Bengali summary and Bengali tags
  sitting under an English title, and the tag space split with it ("jobs" and "চাকরি"
  never match, so one search finds half a vault). Five things go with the setting:
  * **It is one sentence, written once.** `prompts.language_rule(subject)` is called by
    the combined enrichment, by both providers' summary and tag prompts, and by
    `label_prompt`. The four prompts each carried their own copy before, which is a rule
    that gets corrected in whichever file someone happened to open.
  * **The prompt never contains the configured string.** The value is a key into
    `core/languages.py`, and what is interpolated is the *name* that list maps it to --
    so an environment variable cannot finish a sentence in a system prompt.
  * **An unrecognised value is refused at boot**, in every environment, rather than
    resolved to a default. The failure it would otherwise cause is silent: every card in
    the wrong language, with nothing anywhere naming the setting responsible.
  * **`ai_category` is untouched by it and must stay English**: the reply is checked with
    `in CATEGORIES` and written into the schema as an enum, so a model that helpfully
    translates it drops the item into "Other".
  * **Nothing re-runs old items.** A card enriched before the setting changed keeps the
    language it was written in until something reprocesses it, and `reprocess` refuses a
    `completed` item (voice notes excepted). Changing this setting is not a backfill.
  Highlights need no rule -- they are verbatim quotes and `keep_verbatim` enforces it.
  `tests/chat_engine/test_non_latin_routing.py` pins both modes and the boot guard.
- **The chat lane is gone, and the closed gate that guarded it no longer blocks
  anything.** Both are worth knowing because the reasoning still applies to whatever
  replaces them. `scope.py` began as a blocklist (translate, "what is the capital of"),
  and a live bot asked *"Who is sunny leone?"* matched nothing and replied with a
  biography; the polarity was inverted to an allowlist, and then the allowlist declined
  *"give me the list of my last saved"* — a real question about the vault — with the same
  canned sentence. Both failures have one cause: **a regex decided what the model was
  allowed to do before the model read the message.** What replaced it is the agent
  harness (`app/ai/chat/harness/`): the model reads the message and picks its own steps,
  and the harness bounds what those steps can be. Three things carry the weight the gate
  used to:
  * **The prompt states the scope** (`harness/prompts.AGENT_SYSTEM`) and asks for a
    one-line decline with `declined_out_of_scope` set. That is the model's self-report:
    it is logged on `agent_turn` and **never branched on**.
  * **The guard caps a turn that called no tool** at `CHAT_REPLY_MAX_CHARS`
    (`chat_engine/guard.py`). With no tool call there is no evidence behind a word of the
    reply, so length is the signal, and `agent_long_reply_no_tools` is the number to
    watch. A prompt is a request; a cap is not.
  * **Retrieval is the safe default.** `router.route`'s fallback is RECALL, not CHAT, so
    an unrecognised phrasing is answered from the person's own vault or told "I couldn't
    find anything about that", which is true for general knowledge and is what someone
    asking about their memories wanted. A small social allowlist (`scope.is_social`)
    keeps "hi" out of a vector search.
  `scope.check` still runs and still logs its verdict as `scope_verdict` with
  `blocked=False`, so the two systems can be compared on real traffic for one release.
  **Delete `scope.py`, `_log_scope` and their tests after that comparison** — it was
  added 2026-09-03.
- **The bot types while it works, and the indicator is started in TWO places.**
  `sendChatAction` existed on the client and was never called, so the bot was silent
  from the message to the reply — several seconds for a
  recall (embedding, planner, vector scan, answer) and longer for a file, which reads as
  a bot that never received the message and gets the same thing sent again.
  `services/telegram/typing.py` wraps the dispatch in `typing_action`, which **re-sends
  the action on a timer**: Telegram expires the indicator after ~5s and offers no way to
  cancel one, so a single action at the start goes quiet mid-wait, which is worse than
  never showing it. It is bounded by `MAX_SECONDS`, cancelled when the block exits (the
  reply itself clears the indicator), and swallows its own failures — a cosmetic dot must
  never turn a successful capture into a Celery retry. The chat id is read straight off
  the raw update by `chat_id_of`, before parsing and before the `telegram_accounts`
  lookup, because typing at a chat discloses nothing; group chats are excluded there as
  everywhere else in this surface.
  The **webhook** fires one action too (`send_typing_once`, a FastAPI background task so
  the 202 never waits on a Bot API round trip), because the worker's loop cannot begin
  until something dequeues the update — and that hop through Redis is the part the sender
  reads as "did it even arrive?". One action lasts ~5s, which covers the hop; the worker
  takes over from there. It is fired only on the *queued* path: when the enqueue failed,
  `notify_degraded` apologises instead, and a typing dot is a promise to reply. The
  webhook uses `chat_id_of` rather than its own `_telegram_chat_id` for exactly the group
  reason above — the degraded notice may answer a room because it is an apology for a
  message already sent there; typing must not.
  **Both call sites are pinned by wiring tests** (`tests/integrations/test_telegram_typing.py`,
  `test_telegram_webhook.py`) that run the real `_handle_telegram_update` and the real
  route, because "the method exists and is tested" is precisely what was true while
  nothing called it.
- **Provider tokens are Fernet-encrypted** (`app/core/crypto.py`) in `oauth_accounts`, keyed by
  `TOKEN_ENCRYPTION_KEY`. With no key set (dev) they are dropped, not stored in the clear; the
  boot guard makes the key mandatory outside dev once any provider is configured. The Facebook
  token is deliberately kept: it is what a later Instagram extractor will use.
- **The OAuth callback fails closed.** State is compared with `secrets.compare_digest` against an
  HttpOnly cookie and a *missing* cookie is a rejection, not a skip. The router still honours
  `uses_pkce` (verifier in its own cookie), though no registered provider sets it today.
  The post-login `next` target passes through
  `_safe_next`, which only lets same-origin relative paths through — everything else becomes
  `/vault`.
- **Every user-supplied URL the worker fetches goes through `app/core/net.py` first.** The
  extractors fetch whatever someone pastes, so without `assert_safe_url` that is an SSRF
  primitive: `http://169.254.169.254/latest/meta-data/` would put cloud IAM credentials into the
  pasting user's own vault, and internal services are reachable the same way. The guard resolves
  the hostname and requires every resulting address to be publicly routable (checking the literal
  string is useless — plenty of names resolve inward), and `ArticleExtractor` follows redirects
  manually so each hop is re-validated. Residual risk is DNS rebinding; close it with egress
  rules, not more string checks. `tests/core/test_ssrf_guard.py` pins the cases.
- **Logs are files in dev and stdout everywhere else -- never a database table.** Every
  structlog event from the API, the worker and beat is appended as a JSON line to
  `LOG_DIR/<source>-<date>.jsonl` (`logs/api-2026-08-25.jsonl`, ...) by
  `app/core/log_sink.py`, so three processes share one greppable folder and `request_id`
  correlates a request across all of them:
  `jq -c 'select(.request_id=="abc")' logs/*.jsonl`. `settings.file_logging_enabled` is
  hard-gated on `ENV == "dev"`, not merely defaulted off: a deployed container's
  filesystem is ephemeral and unmonitored, so files there would be a PII spill nobody
  reads. Writes are one `os.write` to an `O_APPEND` fd, which is why the prefork worker's
  children can share a file without a lock between processes. The sink is opened per
  process -- `start_log_sink()` in the API lifespan, `worker_process_init` / `beat_init`
  in `queue/celery_app.py` -- because an fd inherited across fork shares its offset.
- **Credential-looking keys are redacted before the line is built**, not before it is
  displayed (`_SENSITIVE_KEY_PARTS` in `log_sink.py`). A log file gets copied, pasted into
  an issue and archived, so a token that reaches the disk has already leaked. New logging
  helpers must go through `redact` / `build_record`.
- **Log retention is 15 days and the sink enforces it itself.** `prune_expired` runs when
  the sink rolls over to a new day (and on the first write of a process), deleting only
  files matching `<source>-<date>.jsonl` and dating them from the *filename*, not mtime --
  so retention holds with no cron, no worker and no beat running, and a stray note in the
  folder is never touched. A retention below 1 day is refused at boot.
- **There is no log API and no `app_logs` table.** Reading the trail in dev is
  `tail -f logs/*.jsonl`; in staging/prod it is the platform's stdout drain. Do not add an
  HTTP endpoint that serves log files -- it is a path-traversal surface guarding data that
  spans every user.
- **Uploaded files live in a private Backblaze B2 bucket; Postgres stores only the key.**
  `POST /vault/upload` accepts any allowlisted document, `services/documents.py` decides
  what it is **from the bytes** (never the filename or the browser's Content-Type), the
  object goes to B2 *before* the row is inserted (a failed upload then leaves no item
  pointing at a file that is not there), and `GET /vault/{id}/file` mints a presigned GET
  that expires in `DOWNLOAD_LINK_TTL_SECONDS`. There is no public-URL setting and no
  static file route: ownership is re-checked in the repository on every download, so the
  signed URL is the only way in and it is minted per request. `storage_key` is never
  serialized to the browser. `get_storage()` returns None when the bucket is unconfigured
  and uploads degrade to text-only rather than the API failing to boot.
  **The same client speaks AWS S3 too, and the endpoint picks which.** `B2_ENDPOINT_URL`
  empty (the default) means AWS: with no keys, `storage/b2.py` hands boto3 `None` and it
  finds the node's instance role through IMDS. Keep the `or None` -- an empty string is
  sent as a key and rejected with `InvalidAccessKeyId`. Backblaze needs the endpoint *and*
  both keys; `_validate_storage_config` refuses every shape in between at boot. Role
  credentials are short-lived, so a presigned URL can die before its TTL -- harmless for
  a download minted on the click, not for a 6h thumbnail link.
- **A card's picture is copied into our bucket; the scraped URL is not kept as the
  answer.** An `og:image` from Instagram or Facebook is a *signed* fbcdn/cdninstagram
  URL with an expiry measured in days -- measured 2026-09-13, eleven of twelve stored
  stills already answered **403**. Storing it is storing a credential on someone else's
  clock, and the failure is silent in the worst way: the capture is fine, the row is
  fine, and the picture just stops arriving about a week later. It also cost requests --
  a failed `<img>` made the card re-mint a download link for a memory with no stored
  file, so every link card spent a round trip on a guaranteed 404 (`GET /vault/{id}/file`
  404 storms in the log are this). `services/thumbnails.py` mirrors the image once, at
  processing time, into the same private bucket. Five rules:
  * **The fetch goes through `assert_safe_url`, per redirect hop.** The URL was written
    by a scraped page, so this is the same SSRF surface as an extractor.
  * **The type comes from the bytes**, never the header or the extension, and **SVG is
    refused** -- this is the one object in the bucket a page is meant to render.
  * **Failure is silent and total.** Decoration must never fail a capture, retry a task,
    or strand an item; the row keeps the scraped URL, which at least works for a week.
  * **`thumbnail_key` never reaches the browser.** `VaultItemRead.thumbnail_url` is
    swapped for a presigned URL in `api/cards.py`, minted per response with
    `THUMBNAIL_LINK_TTL_SECONDS` (6h -- it has to outlive the *page*, not a click), and
    those responses carry `Cache-Control: private, no-store` because they now embed a
    credential. It is signed in the listing rather than served by a
    `GET /vault/{id}/thumbnail` route because a route per card puts the round trips
    straight back (see "Latency").
  * **Nothing is backfilled by the migration.** `0015_thumbnail_key` adds the column and
    stops; `scripts/backfill_thumbnails.py --apply` is the one-off, and it rescued
    exactly 1 of 12 because the other eleven were already dead.
- **The upload allowlist is closed, and SVG/HTML are refused on purpose.** They are
  executable in a browser context, so storing them makes any future render path a stored
  XSS. Defence in depth on top of that: every presigned URL forces
  `Content-Disposition: attachment` with the name sanitized for the header, so nothing
  from the bucket is ever rendered by an origin. The object key is
  `users/<user>/<item>/<uuid>.<ext>` -- entirely server-generated, so `../` in a filename
  is a character the display name loses, never a path. `tests/vault/test_documents.py` pins it.
- **Deleting an object means deleting every version of it.** The bucket's lifecycle is
  "Keep all versions", so a plain S3 `DELETE` writes a *delete marker* and leaves the
  bytes -- verified against the live bucket: one delete left `live versions: 1`. A user
  who asked for their document to be removed would still have it stored, and still be
  billed for it. `B2Storage.delete` therefore lists the versions for exactly that key
  (prefix listing also returns neighbours like `a.png.bak`, which are skipped) and removes
  each by id, falling back to a plain delete when versioning is off. Do not "simplify"
  this back to one `delete_object`.
- **A voice note's transcript is the memory; the audio is a keepsake beside it.**
  `POST /vault/voice` (`services/transcription.py` + `VaultService.save_voice_note`)
  transcribes with OpenAI Whisper, saves a `ContentType.voice` item whose `content` is
  the transcript, and lets the ordinary pipeline summarise, tag, label and embed it — so
  a spoken note is searchable by its words, not by its filename. Five things go with that:
  * **Transcription is NOT on the `AIProvider` Protocol.** Every provider implements that
    Protocol and Gemini has no Whisper equivalent wired up here; adding `transcribe` would
    oblige a provider to implement what it cannot, and because the Protocol is structural
    the gap would only show at runtime. Speech has its own switch —
    `settings.transcription_enabled`, gated on `OPENAI_API_KEY` alone — so a vault
    summarising with Gemini still records voice notes.
  * **The model is `gpt-4o-transcribe`, not `whisper-1`, and language detection is not
    trusted on its own.** whisper-1 decides the language from the first window of audio,
    and on a short clip in a non-Latin script it goes wrong in a specific and expensive
    way: a Bengali voice note came back as fluent, confident Traditional Chinese. Every
    downstream artefact — title, tags, embedding — was then correct about the wrong text,
    so nothing on the page looked broken. Three things address it, in order of how much
    they can be relied on:
      1. **Pinning.** The recorder offers a language picker and `POST /vault/voice` takes
         `language` (ISO-639-1, re-derived from the closed `LANGUAGES` allowlist — the
         value reaches a provider and a page). Passing it removes the detection step
         entirely, which is the only complete fix.
      2. **The characters.** `script_of` labels a transcript from its Unicode block, and
         `contradicts_script` decides whether the model's answer disagrees. Deliberately
         narrower than "they differ": Hindi *is* written in Devanagari, so demoting
         "hindi" to "devanagari" would lose real information — only a genuine
         contradiction (Han characters reported as Bengali) lets the script win. A
         mismatch is logged as `voice_language_mismatch`; a rise there is the difference
         between one bad clip and a model that cannot hear a language.
      3. **The model's own answer**, kept when it agrees with the script or when the
         script says nothing (Latin).
  * **The duration now comes from the recorder.** Only the `whisper-*` models answer
    `verbose_json`, which is what carried `language` and `duration`; the gpt-4o
    transcribers answer plain `json` and report neither. The player needs a length
    regardless — a MediaRecorder WebM has none in its header and reports `Infinity` until
    fully buffered — so the client sends the `duration` it measured.
  * **A voice note is the one thing re-drivable after it SUCCEEDED.** `reprocess`
    normally refuses `completed`, because re-running a good item spends the whole pipeline
    to reproduce itself. A transcript is the exception: it is the single output that can be
    confidently, fluently wrong, and "finished" is exactly what would make that unfixable.
    So when the item is `voice` **and its audio is still in the bucket**, `reprocess`
    clears `content` — which makes `ProcessingService._transcribe` re-read the object —
    and accepts a `language` to pin the re-run, because repeating a failed auto-detection
    unchanged is the same coin flip. `components/transcript-controls.tsx` is that UI, and
    it renders for a completed item unlike the generic retry.
  * **The container is sniffed from the bytes, never from a name.** A `MediaRecorder` blob
    has no filename; the client invents one. `transcription.inspect` reads the signature
    (WebM/EBML, Ogg, RIFF+WAVE, ISO `ftyp`, ID3 or an MPEG frame sync, FLAC) and the name
    handed to the provider — and to `Content-Disposition` on download — is one the server
    wrote.
  * **A failed *storage* upload does not abort the save, and that is the opposite of
    `save_document` on purpose.** By the time the bucket is reached the clip has been
    transcribed and paid for; dropping the words because the audio could not be filed
    throws away the expensive half to keep the cheap one. A failed *transcription* does
    abort — a row holding only unreadable audio is an empty memory the user has to find
    and delete.
  * **`file_name` is only set once the object is really in the bucket**, because it is
    what the detail page reads to decide whether to offer playback and a Download —
    filling it in for audio that was never stored puts a button on the page whose only
    possible answer is a 404. The clip's size and type stay in `item_metadata` regardless.
    Playback (`components/audio-attachment.tsx`) mints the presigned URL **on the click**,
    never on render: it expires in `DOWNLOAD_LINK_TTL_SECONDS`, so a tab left open
    overnight would otherwise hold a dead link. One silent re-mint per mount on an
    `error` event covers expiry; a second error is reported as an unplayable file, which
    is a real case — a Chrome-recorded WebM/Opus clip does not decode in Safari.
  * **The waveform is measured while recording, never re-derived.** The recorder keeps
    every amplitude sample and downsamples it once on stop to 48 peaks, sent as a `peaks`
    form field and kept in `item_metadata["waveform"]`. Reading peaks back off the stored
    file would mean re-downloading and decoding the audio in the browser, and the
    presigned URL is not fetchable cross-origin. It is client-written data bound for a
    JSONB column and then for an SVG, so `transcription.parse_waveform` re-derives it —
    JSON list, finite numbers only, truncated to 48, clamped to 0-100 ints — and returns
    None for anything else, silently: the picture is decoration beside a transcript and
    must never cost the user the words. A memory with no peaks draws a **flat baseline**;
    `components/voice-hero.tsx` says explicitly not to synthesize a shape from the item
    id, because a plausible waveform unrelated to the audio is a picture of data that does
    not exist and nobody looking at it could tell.
  * **The hero banner IS the player for audio items.** For every other kind
    `MemoryBanner` is decoration over content further down; for a recording the audio is
    the content, so `VoiceHero` replaces it and there is deliberately no second transport
    on the page. The `<audio>` element is hidden and the waveform is the scrubber — bars
    are `aria-hidden`, and the control under them is a real `<input type="range">`, which
    is what supplies arrow-key seeking and "Seek, 7 of 42 seconds" to a screen reader.
    Duration comes from Whisper (`item_metadata.duration_seconds`) rather than the
    element: a MediaRecorder WebM carries no duration in its header and reports
    `Infinity` until fully buffered, so the scrubber would otherwise have no length.
  * **An inaudible clip is refused, not saved.** Whisper answers silence with an empty
    string; that is a real answer, so the retry sits on `_call_provider` rather than on
    `transcribe` and nobody pays twice for it. Two attempts, not three: a retry re-uploads
    the whole clip. Provider faults are re-raised as `TranscriptionFailed` with our own
    wording — theirs can name the account it rejected — and only the exception *type* is
    logged.

- **An uploaded image is read by a vision model, not filed blind.** `services/vision.py`
  describes the picture and transcribes any text in it; the description becomes `content`,
  so the ordinary pipeline summarises, tags, labels and embeds it and a screenshot of a
  receipt is findable by asking about the receipt. Its own capability with its own switch
  (`OPENAI_API_KEY`), for the same reason as transcription — not a fifth method on the
  `AIProvider` Protocol. Four rules go with it:
  * **The bytes go to the provider; the presigned URL never does.** Handing OpenAI a
    signed bucket URL sends a live bearer credential to a third party and makes a private
    object externally fetchable for its whole TTL. The worker downloads
    (`ObjectStorage.download`) and inlines a base64 data URL.
  * **`can_describe` is checked at save time, not in the worker.** A HEIC, an oversized
    file or a missing key means `skipped` immediately, rather than a round trip through
    the queue to be skipped there. HEIC is in the *upload* allowlist and not in the vision
    one on purpose: stored and downloadable, just not readable.
  * **`VisionError` is `skipped`; `VisionFailed` is retried.** "This image cannot be read"
    is an answer — retrying spends another reading to reach the same place. A provider
    fault or an unreachable bucket is not.
  * **The description is marked as machine-written** (`item_metadata["content_source"] =
    "vision"`) and the reader says so. Rendering a model's account of a picture
    identically to words the user typed is the one way this feature can lie.
- **Nothing captured is allowed to sit in limbo, and `processing_error` is scrubbed
  before it is stored.** Celery's retries only cover a task that *ran and raised*; two
  shapes slip past them and both end as a card the user watches forever. The beat task
  `sweep_stranded_items` (every 5 min) owns both: an item stuck in `pending` was never
  queued — usually `vault_enqueue_failed` against an unreachable Redis — so it is
  **re-queued**, bounded by `MAX_SWEEP_REQUEUES` because an item that kills the worker on
  load would otherwise be re-driven forever; an item stuck in `processing` past
  `STUCK_PROCESSING_MINUTES` had its worker killed mid-task, and since there is no safe
  way to know how far the half-finished run got it is marked **failed** with a sentence
  its owner can act on. `list_stranded` excludes items with a *running* extraction run —
  those legitimately sit in `processing` for minutes and belong to `sweep_stale_runs`;
  two sweepers racing to a verdict is how the one with less information wins.
  `POST /vault/{id}/reprocess` is the manual half: allowed only from `failed` and
  `skipped` (`skipped` matters — an image saved before a vision key existed becomes
  readable the moment one is set), refused with 409 when already queued or already
  finished, and 429 inside `REPROCESS_COOLDOWN_SECONDS`. **The retry button renders only
  for those two states** — offering it on a healthy memory invites spending the whole
  pipeline again to replace a result with itself.
  `core/errors.safe_error_text` scrubs the stored reason **on the way in, not at render
  time**: an httpx error carries the whole request URL and Apify's carry a live token in
  the query string, and a redaction that only happens on one render path is one the second
  render path forgets.
- **An upload that carries no readable text is `skipped`, not `failed`.** A PDF's or a
  .txt's text goes through the normal AI pipeline; an image or a .docx is stored and
  downloadable but never sent to the model -- there is no OCR and no OOXML parser, and
  calling the model on an empty string spends tokens to hallucinate about a filename.
- **A Space is a context, not a folder, and it is the one place a user reads someone
  else's rows.** `Space` / `SpaceItem` (`app/models/space.py`) are the renamed
  `Collection` / `CollectionItem`; the old word is gone from Python, SQL and the UI.
  Membership is a plain many-to-many and nothing enforces exclusivity -- a `VaultItem`
  belongs to as many Spaces as it belongs to. Four rules hold the sharing boundary and
  none of them should be relaxed casually:
  * **A member sees cards, not bodies.** `GET /spaces/{id}` serialises every member's
    items as `VaultItemRead` -- title, summary, tags, thumbnail. `GET /vault/{id}`,
    `content`, `ai_highlights`, `item_metadata` and the file route stay owner-only and
    were not widened. Being in a shared Space is not a grant on the memory itself. The
    line lives at one `VaultItemRead.model_validate` call in `app/api/v1/spaces.py`.
  * **You may only add your own memories.** `SpaceService._attach` filters every id
    through `vault_repo.get(item_id, actor_id)`, per item and not per request. Owning
    the container has never granted access to its contents -- that was a real
    cross-tenant IDOR here once, and editors make it worse rather than better: a person
    with write access to someone else's Space must still not be able to pull a third
    party's memory into it, least of all into a *public* one.
  * **Ownership is `spaces.user_id`, never a `space_members` row.** One source of truth,
    so a role row can never contradict the column that gates deletion and publishing.
    `SpaceRepository.get_for_viewer` is the only way into a Space and returns the
    caller's effective role with it, so a new route cannot skip the check by forgetting
    an argument -- the signature has nowhere to put one that would be ignored.
    `_RANK` orders viewer < editor < owner so a gate is a comparison, not a case list,
    and an unrecognised stored role reads as `viewer` rather than as anything more.
  * **Deleting a Space is soft**, unlike `VaultRepository.delete`. It can hold other
    people's contributions and can be someone's only route to a shared page.
- **Adding a memory to a Space is idempotent, and that is load-bearing.**
  `SpaceRepository.add_items` uses `ON CONFLICT DO NOTHING` on the composite PK and
  returns a count. The previous implementation issued a bare INSERT, so re-adding raised
  a `UniqueViolation` and the request 500'd -- and the batch paths ("add all suggestions",
  a selection that overlaps the Space) hit that as the *normal* outcome. The route
  answers `{added, skipped}`; `skipped` counts what was already there **and** what was
  not the caller's to add, so it is reported rather than swallowed.
- **A Space invite is a link, not an email.** There is no mailer in this service, and
  inviting by address would need an oracle for "does this person have a Recall account",
  which every other surface here refuses to provide. So `space_invites` mints a token
  exactly like `telegram_link_tokens` does: 32 random bytes handed over once, only the
  SHA-256 stored, single-use, and **unknown / spent / expired / deleted-Space all answer
  the same 404**. `owner` is not grantable by invite -- transferring a Space is a
  different operation and must not be reachable from a dropdown. The token is spent
  before the membership is written, so a failure between the two cannot leave a live
  credential. `/spaces/invites/{invite_token}/accept` is declared **above** the
  `/{space_id}` routes, and its parameter is `invite_token` rather than `token`, because
  FastAPI resolves path-parameter names across the whole dependency tree and
  `get_current_user` already takes a `token` -- from a cookie, with a default, which a
  path parameter may not have.
- **Every mutating Space route carries `assert_same_site`**, like the auth and
  integrations routes and unlike the vault ones. Worth knowing in development: the guard
  compares `Origin` against `CORS_ORIGINS` + `FRONTEND_URL`, so browsing
  `http://localhost:3000` while those are set to a tunnel gets a **403 on writes** while
  reads and the whole vault keep working. That is the guard doing its job, not a bug --
  browse the configured origin, or add localhost to `CORS_ORIGINS`.
- **`accent` stores a key, never a CSS class.** A `spaces.accent` of `"violet"` is
  resolved to a gradient by the frontend (`lib/space-accent.ts`). A column holding
  `from-violet-200 via-indigo-100` is a column coupled to a Tailwind version, and it
  breaks silently on their next major -- the class stops existing, the card renders with
  no background, and nothing errors. `connection_count` is nullable for a related
  reason: NULL means *never computed*, which the UI renders as nothing rather than as a
  zero, because "no connections" and "not measured" are different claims.
- **`0013_spaces` renames tables, which made two historical migrations wrong.**
  `0001_initial` builds the schema from the *current* models via `create_all`, so on a
  fresh database the table is called `spaces` before `0002_schema_sync` and
  `0003_timestamptz` ever run -- and both of them named `collections` outright, aborting
  the whole upgrade. Both are now guarded with `to_regclass`, and their statements are
  kept rather than deleted because a database created before the rename still has to pass
  through them. **Any future table rename has to sweep the earlier migrations the same
  way.**
- **A connection is ONE row, directional, read from both ends.** `memory_connections`
  (`app/models/connection.py`) stores `source_item_id -> target_item_id` once, and
  `ConnectionRepository.list_for_item` returns each edge with a `direction` computed
  relative to the memory that was asked about. Storing two rows per edge would be two
  sources of truth that can disagree after a retype, with nothing to arbitrate -- the
  reason ownership lives only in `spaces.user_id`. Storing one *undirected* row would make
  `part_of` and `depends_on` unrenderable, because those say different things each way.
  "Bidirectional navigation" is a read concern and it is solved by reading. Five things go
  with it:
  * **The unique key is the unordered pair and excludes the relation.**
    `(user_id, pair_low, pair_high)` over two `GENERATED ALWAYS AS (least/greatest(...))
    STORED` columns, so a pair has at most one edge however it is labelled and whichever
    end it was drawn from. Re-typing is an `UPDATE`. That is also why `POST /connections`
    answers **200 with `created: false`** rather than 409 -- somebody connects A to B from
    A's page and later from B's, and re-adding is a normal outcome, not an error (the
    `SpaceRepository.add_items` lesson). The generated columns are an index, never data:
    they are not in `_PUBLIC_FIELDS` and must not be.
  * **A dismissal is a `status`, not a second table.** The constraint that stops a
    duplicate edge is the same one that has to stop a re-suggestion, so the derivation's
    `ON CONFLICT DO NOTHING` already refuses to re-propose a pair somebody declined -- the
    dismissal *is* the conflicting row. Two tables means two checks and the second is the
    one that gets forgotten. The cost is that a dismissed row owns the pair forever, which
    is paid by a manual connect **reviving** it: a person deliberately connecting two
    memories is overriding their own earlier "don't suggest this", and a 409 there is a
    dead end nobody would guess the way out of.
  * **Symmetric relations are normalised on write.** `related_to` and `contradicts` read
    the same from both ends, so `ConnectionService._orient` points them from the lower id.
    Left to chance, half of them render backwards and somebody eventually "fixes" a row
    that was never wrong.
  * **Every RETURNING statement carries `populate_existing`** (`_FRESH` in
    `repositories/connection.py`). SQLAlchemy's identity map hands back the object it
    already has for a primary key rather than rebuilding it from the row, so an
    `UPDATE ... RETURNING` against a row the session has loaded returns the **stale**
    instance: the database is right and the response is wrong, and nothing errors. Caught
    by the dismiss-then-reconnect test, where a revived edge came back saying `dismissed`.
  * **There is no 403 in this feature, and there must not be.** A connection has one
    owner, so "no such edge", "not yours" and "no such memory" are one answer; anything
    else confirms an id exists to somebody who may not see it. Spaces need both codes only
    because a member can be shown a Space and still be refused inside it.
- **`/connections` with no memory named opens on the CANVAS, and the list is the panel
  beside it.** `GET /connections/graph` (`ConnectionRepository.graph`, one statement)
  answers with every edge plus each memory an edge touches **once** -- nodes derived from
  edges server-side, so the two cannot disagree about what is on screen, and a hub's card
  is not sent six times to be presigned six times. The frontend
  (`app/connections/vault-canvas.tsx`, React Flow + `lib/graph-layout.ts`) draws it, and
  the reason it replaced a list is that a column of suggestions to approve one at a time
  is a chore rather than a place: it shows what the system decided, with no way to see what
  else a memory sits near and no way to connect two things without first opening one. On
  the canvas **both ways an edge comes into existence are the same gesture in the same
  place** -- a model proposes one and it appears as a ghost you accept where it is drawn,
  or you drag one card onto another and it is yours immediately. Five rules:
  * **A suggestion is drawn as a ghost, never as a fact** -- dashed, faint, labelled with
    the model's own sentence. Rendering a proposal identically to an accepted edge is the
    one way the canvas can lie about what its owner has agreed to.
  * **Dismissed edges are never drawn.** The row is kept so the derivation cannot
    re-propose the pair; putting somebody's own "no" back on the canvas makes a decision
    they already took look undone. `include_dismissed` exists for debugging and is off.
  * **The layout is solved once and then owned by the person.** `forceLayout` runs
    d3-force synchronously to completion and stops -- a graph still settling under a cursor
    is a graph that moves out from under a click. It is keyed on the *topology*, so the
    30s poll moves nothing and confirming a suggestion recolours an edge rather than
    rearranging the vault.
  * **Every canvas action is also in the list.** Drag-to-connect has no keyboard
    equivalent worth pretending about, so the review panel is the accessible path -- the
    same actions rendered for a different input, not a fallback view.
  * **A vault with no edges still gets the old picker.** A canvas of nothing is a worse
    empty state than a list of memories somebody could connect.
  `GET /connections/hubs` (`ConnectionRepository.most_connected`, one statement) is what
  that picker lists, and it stays ordered by how connected each memory is rather than by
  date: **the newest capture is usually the least connected**, since everything it links
  to was found from its side and nothing has been saved since. Two rules with it:
  * **Undecided edges come first.** A backfill lands a whole vault's worth of suggestions
    at once, and until `SuggestionInbox` rendered there was no page that listed them --
    they were reachable only by opening a memory that happened to have one. A decision
    nobody can find is a decision nobody makes.
  * **Hubs count confirmed edges only, and only live neighbours.** A count that included
    suggestions or tombstones would promise cards the neighbourhood read will not return.
  Both counting reads share `_ENDPOINTS`, the `UNION ALL` that yields one row per *end* of
  each edge -- an edge is stored one way and read from both, so anything counting per
  memory has to see it twice, and two copies of that is two definitions of what a
  connection count is.
- **A Space's connections panel shows the caller's OWN edges and nobody else's.**
  `GET /spaces/{id}/connections` -> `ConnectionRepository.list_in_space`, which filters on
  three things: the edge is the caller's, both of its memories are in the Space, and both
  are still alive. This sits beside "a member sees cards, not bodies" and narrows the same
  boundary rather than widening it. The reasoning, because it is a decision and not an
  oversight: **a connection is a judgement its author made about their own memories.**
  "A contradicts B" is an opinion, not a fact about the Space, and publishing everyone's
  to everyone would make a shared context a place where your reasoning about your own
  saves is readable by strangers -- including through a public share page. Two members
  therefore see the same Space with different graphs, and the panel copy says so outright
  rather than letting anyone assume it is the Space's graph. It is also the reversible
  direction: widening this later is a decision somebody can take deliberately; un-showing
  people each other's judgements after the fact is not. The membership gate runs **first**
  -- inferring "not a member" from an empty edge list would save a statement and stop
  being an access check.
- **Derivation is RECALL then JUDGEMENT, and the judge's main job is saying no.** The
  first version was arithmetic end to end -- nearest few vectors, one floor, everything
  written as `related_to` -- and it worked exactly as designed while filling the review
  inbox with four copies of the same reel and no reason attached. A cosine distance cannot
  tell "these are the same video" from "these are both in Bengali" from "these are two
  halves of one subject". Three stages replace that number:
  1. **Recall over-offers.** The vector half runs at `CONNECTION_RECALL_FLOOR`, well
     *below* the decision floor, and a second half searches `ai_tags`
     (`VaultRepository.search_by_tags`): two notes about one job application share `jobs`
     and `visa` and can still sit far apart in embedding space, because a summary about a
     form and a summary about an interview genuinely are different text. That tag query is
     an **OR of `@>` containments**, not `?|` -- the GIN index is `jsonb_path_ops`, which
     serves containment and nothing else, so the obvious operator is the one that silently
     falls back to a sequential scan over the vault.
  2. **Signals**, free and deterministic, computed in `connection_derivation.py`: shared
     tags, shared `ai_category`, the similarity, and whether both rows canonicalise to the
     same URL. They reach the prompt as *measured facts about two rows*, not as something
     a model has to infer.
  3. **Judgement** -- `app/ai/connection_judge.py`, ONE call with every candidate in it.
     One call and not one per candidate: the subject card is the expensive half and is
     identical for each pair, and it is the only way the model can see that three
     candidates are the same video, which a pairwise judge cannot -- each pair looks
     connected on its own.
  `CONNECTION_JUDGE_ENABLED` ships **on**, unlike `CONNECTION_TYPING_ENABLED`, and the
  difference is the direction of the failure: typing puts a confident label on an edge a
  person already accepted, where a wrong one is filed somewhere nobody looks again; this
  one rejects, and its failure mode is a suggestion that should not have been offered --
  which is what the review inbox is for. Off, unconfigured, or failed, `_floor_only` is
  what runs: `CONNECTION_MIN_SCORE` over the recall list, everything `related_to`, no
  reason. That is the behaviour the feature shipped with and the path with the most tests
  behind it -- the same ladder `RecallAgentService` climbs down to `RecallChatService`.
  **A tag-only candidate is deliberately not proposed by the fallback**: with no judge
  there is nothing to tell a shared subject from a shared vocabulary, and `jobs` belongs to
  hundreds of items.
- **A derived connection is a SUGGESTION, and that is a security control rather than a
  courtesy.** Judged or not, nothing the derivation writes is ever `confirmed`. A judge
  that is right nine times in ten still writes a wrong edge every tenth capture, and an
  edge is a route from one page's text into another memory's prompt context. Confidence
  (`CONNECTION_JUDGE_MIN_CONFIDENCE`) sorts the inbox and feeds a floor; it never replaces
  the tap. Seven things go with it:
  * **The key a judgement names is re-checked against the keys that were sent.**
    Candidates are `c1..cN`, local to one prompt and deliberately *not* short ids -- a
    short id there is an id the model learned somewhere no citation validator is watching.
    A key that was never sent is discarded, which is the same claim `GetMemory`'s
    surfaced-id check makes: the model may choose among what it was handed and may not
    name anything else.
  * **The threshold has never been measured.** `CONNECTION_MIN_SCORE` defaults to 0.62 and
    that number came from nowhere. Run `scripts/measure_connection_floor.py` before
    trusting it, and re-run it whenever the embedding provider changes -- this is the
    `RECALL_MIN_SCORE` bug waiting to happen again, where Gemini's 0.55 was carried onto
    an OpenAI vault whose true matches score 0.373. It now only decides the *fallback*,
    which makes it less load-bearing and no less wrong.
  * **Dismissals are shown back to the judge as taste** (`recent_declines`,
    `CONNECTION_DECLINED_HINTS`). A hint in a prompt and nothing stronger -- no model is
    trained and no threshold moves -- and what it buys is the difference between a system
    that offers the same wrong kind of connection forever and one that stops after being
    told twice. Fail-soft: a capture must not lose its connections because that read
    failed.
  * **It is a separate Celery task, enqueued after the commit**, beside `_notify_surface`
    and fail-soft like it. Inline in `ProcessingService._enrich` the exception would travel
    up through `process`, which writes `processing_error`, bumps `retry_count` and
    **re-raises**: a bug in the connection scan would mark a perfectly enriched memory
    `failed` and then pay for three more full enrichments to reach the same failure.
    `max_retries=1`, not three -- a `UniqueViolation` from two captures racing is absorbed
    by `ON CONFLICT`, and an item with no embedding is an *answer*.
  * **Both completion paths call it.** `_process_item` for a fast capture and
    `_finalize_run` for a deferred one. Wiring only the first is how the Apify half of a
    vault silently has no connections;
    `tests/connections/test_derivation_wiring.py` reads the source of both and requires it.
  * **`exclude_item_id` on `search_semantic`** keeps an item off its own list. The query
    vector belongs to one of the rows being searched, so without it the strongest candidate
    every single time is the item itself at distance zero.
  * **Older memories are never re-scanned.** An edge is found from the new capture's side
    only, which is what makes this O(1) per capture rather than O(n^2) over the vault. The
    older memory still *shows* the edge -- that is what directional storage buys -- it just
    never goes looking. `scripts/backfill_connections.py` is the one-off for a vault that
    predates the feature, deliberately a script rather than a beat task: a sweep that runs
    forever over a vault that only ever grows is a cost with no ceiling. **It spends no
    money unless you pass `--judge`** -- every vector it compares was already written at
    capture time -- which makes its dry run the one honest way to check
    `CONNECTION_MIN_SCORE` against a real vault without buying fresh embeddings. The
    judge is opt-in there precisely because over a whole vault it is one model call per
    memory, and because an apply decided by a model would no longer be the report the dry
    run just printed. It reports the pair count *deduplicated by unordered
    pair*, because A finds B and B finds A and the database keeps one row: counting
    sightings reports twice the suggestions that could actually land, and that number is
    the one somebody sets a threshold from.
  * **`CONNECTION_MAX_PER_ITEM` counts every state**, dismissed and suggested included. A
    wall of pending suggestions is exactly as much of a wall as a wall of confirmed ones.
  * **`duplicate_of` is mechanical, not semantic.** It is a claim about two *rows* -- the
    same video, article or link kept twice -- which is why the judge is handed a measured
    "same source" signal rather than asked to infer one. `_canonical_url` drops the query
    string (a share link arrives as `?igsh=` from one place and `?fbclid=` from another),
    the fragment, `www.` and a trailing slash, and **refuses a non-http scheme** -- two
    rows sharing `about:blank` are not one page. Short links are deliberately **not**
    resolved: that is a network fetch, per candidate per capture, against a URL from a
    scraped page, which is the SSRF surface `assert_safe_url` exists for. Like everything
    else it is still only a suggestion.
- **A connection is a new way to get attacker-controlled text into a prompt, and the
  confirmation step is what closes it.** The text of a page somebody saves already becomes
  `content`, `summary`, `title` and `ai_tags`; it now also becomes the embedding an edge is
  derived from. So an attacker who guesses a topic the victim keeps memories about can
  write a page stuffed with that vocabulary, get it saved, and have their card appear
  beside the victim's memory -- text next to a memory the question was never about.
  What stops it: a derived edge is `suggested` and is invisible to `GetConnections` until a
  person taps it (**this is the reason not to auto-confirm above a threshold, and no
  threshold replaces it**); the score floor bounds the attack to topics the attacker can
  guess; `CONNECTION_MAX_PER_ITEM` bounds how much of one page it can occupy; `user_id` is
  on the toolbox, on the table and re-applied in the repository, so there is nothing to aim
  cross-tenant; `GetConnections` refuses an id this turn did not surface, so a caption
  naming one gets the refusal; neighbours arrive inside `chain.fence_block` as quoted
  material; and `validate_answer` still strips a citation or a URL that was never
  surfaced. **Residual risk, stated plainly:** somebody who can get a page into a vault can
  get that page *adjacent to a chosen memory* in a prompt. That is a relevance attack, not
  an authorization one, and no string check closes it -- the same shape as
  `assert_safe_url`'s residual DNS-rebinding risk.
- **`GetConnections` is bound on the reader, not on the method** (`harness/tools.py`), the
  way the `propose_*` tools are bound on the store: every toolbox has the method, and a
  turn with nowhere to read edges from must not be offered a tool whose only possible
  answer is an apology. It is **not** an overlap of `QueryMemories` -- the reasoning that
  keeps `SearchMemories` and `ListMemories` off this lane was about two narrower versions
  of the *same* read, and following an edge is a different read. Its results go through
  `MemoryToolbox._render` like everything else, so they land in `SurfacedSet` and stay
  citable and linkable; the relation rides **inside** the fence, one line, capped, worded
  from the side the question was asked from (`part_of` read from the other end is
  `has a part in`, and handing the model the wrong one is a quiet inversion nobody reviews).
  There is deliberately **no write tool**: connecting from chat would be a `propose_connect`
  minting a token through `chat_engine/proposals.py`, with the surfaced-id check on *both*
  ids, and it is not built.
- **A model may propose which relation an edge is *on demand*, and that is off by
  default.** Not to be confused with the capture-time judge above, which also labels: this
  one re-labels an edge that already exists, at a person's request, and is the only
  connection route that spends a model call per *request* rather than per capture. `app/ai/connections.py` + `CONNECTION_TYPING_ENABLED`, reached only
  from `POST /connections/{id}/retype` -- on demand, on an edge that already exists, never
  during capture. The ordering is the point: a cosine distance can only claim two memories
  are close, which is why `related_to` is the honest default, while a *label* can be
  confidently and fluently wrong in the way a Bengali voice note came back as Traditional
  Chinese. A wrong label filed automatically on every capture is one nobody goes back to
  check. Seven things go with it:
  * **It is a module with its own switch, not a fifth `AIProvider` method**, the same
    reasoning `enrichment.py`, `transcription.py` and `vision.py` each give: the Protocol
    is structural, so a provider missing a method fails at runtime inside the pipeline and
    every fake in `tests/` has to grow it. `tests/ai/fakes.py` needed no change.
  * **`tests/conftest.py::_no_provider_calls` gained it in the same commit** -- the switch
    forced off *and* `_call_provider` stubbed. Off is what the connection tests want; the
    stub is what catches a future caller that reaches past the switch. This is the third
    time that rule has been paid for.
  * **The prompt reads cards, never bodies.** `cards.build_card` is "the shortest
    description that still tells two memories apart"; two article bodies in one prompt is
    the cost the combined enrichment exists to avoid, and it buys nothing when the question
    is how two memories relate.
  * **The fence is local, not `chain.fence_block`.** That one labels a block with a
    memory's real short id, and a short id in *this* prompt is an id the model learned
    somewhere no citation validator is watching.
  * **An unrecognised relation becomes `related_to`**, which is both this vocabulary's
    "Other" and its weakest claim -- the fail-closed direction, exactly like
    `enrichment._validate` mapping an unknown category to "Other". `ENRICHMENT_LANGUAGE`
    must never reach the prompt: the relation is checked with `Relation(...)` and stored as
    an enum value, so a translated one drops every typed edge back to the catch-all.
  * **`reason` is one-lined and capped on the way IN**, never at render time -- a
    redaction that happens on one render path is one the second render path forgets
    (`safe_error_text`'s rule). It is written to `ai_reason` and **never** to `note`:
    showing a model's sentence as words its owner typed is the one way this can lie, and
    the frontend marks it with `ConnectChip`.
  * **The only connection route that spends a model call, so the only one with a cap**
    (`CONNECTION_TYPING_PER_HOUR`, keyed by user id in Redis). Off, unconfigured and
    rate-limited all answer **503** with one sentence: they are the same thing from
    outside, and separating them reports which of an operator's settings is off.
- **The agent can OFFER to connect two memories, and that is the only write-shaped thing
  it can do about them.** `ProposeConnect` mints a single-use token through
  `chat_engine/proposals.py`; the write happens in `telegram/confirm.py::_connect` or
  `POST /chat/proposals/{token}/accept`, code with no model in it. There is still no
  `Connect` tool and there must not be. Four rules:
  * **The surfaced-id check is doubled** -- *both* ids must have been shown this turn.
    Sharper than symmetry: a connection is a route from one memory's text to another's in
    a **later** prompt, which is the mechanism by which a scraped page ends up adjacent to
    a memory the question was never about. An id the model read inside a memory is exactly
    the id an attacker would want on one end of that edge.
  * **The card shows both titles and the relation.** There is no text to check against the
    person's own turn the way `propose_note` has, so what somebody reads before tapping is
    *which two* memories and how they are about to be labelled.
  * **The relation is re-derived from the enum on the way out of Redis.** It crosses the
    store as a string; an unrecognised one becomes `related_to`, the weakest claim in the
    vocabulary and the fail-closed direction.
  * **Both ends are re-checked against the tapping account, per item.** The ids in a
    proposal are not trusted because a proposal carried them --
    `ConnectionService._assert_owns_both` runs exactly as it does for a typed request.
  `confirm.py` takes a `ConnectionWriter` **Protocol** rather than importing
  `ConnectionService`, whose own imports reach `app.ai.connections`. The boundary test in
  `tests/integrations/test_telegram_callbacks.py` checks *direct* imports only, and
  deliberately: a transitive walk was tried and flags `VaultService -> app.ai.spans`,
  which is legitimate -- so the narrow property is the one that is true and worth keeping.
- **`connections` is the one `QUERY_FIELDS` entry that costs a second statement**, which is
  why it is opt-in like `excerpt` -- and why it exists at all: without it the model cannot
  see that a result *has* neighbours, so it has no reason to ever follow them. That is the
  same shape as the missing-`link` bug, where "never say you cannot without looking" only
  works if there is something visible to look at. A denormalized count on `vault_items` was
  rejected: it is wrong the moment anything is dismissed, and the agent reads this number
  and then follows it **inside the same turn**.
- **You may only connect memories you own, and it is checked per item.**
  `ConnectionService._assert_owns_both` reads both endpoints through
  `VaultRepository.owned_items` and requires *two* rows back. A request carrying two ids is
  exactly where a stranger's is easiest to slip in, and "does this user own something here"
  says yes to the half-owned case -- which is the shape of the cross-tenant IDOR
  `SpaceService._attach` was fixed for. `tests/connections/test_connection_authz.py` comes
  at it from every direction a caller has, including the ids swapped.
- **Deleting a memory hard-deletes its edges, beside its chunks.** `VaultRepository.delete`
  is a soft delete that scrubs; the connections are removed outright, for the reason given
  there about the chunks: an edge asserts "this memory is about the same thing as that
  one", which is a statement about content somebody asked to be rid of. It also frees the
  pair, so a deletion cannot permanently block a connection drawn later between what
  remains -- unlike a dismissal, which is meant to. Every read *also* filters `deleted_at`
  on the neighbour, which is defence in depth rather than the only thing standing between
  a tombstone and a live memory's page.
- **Every mutating connection route carries `assert_same_site`**, like the Spaces and auth
  routes and unlike the older vault ones. Same development consequence, worth knowing
  before it reads as a bug: browsing `http://localhost:3000` while `CORS_ORIGINS` points at
  a tunnel gets a **403 on every connection write** while reads and the whole vault keep
  working.
- **Deleting is two steps now, and only the second one destroys anything.**
  `DELETE /vault/{id}` writes `deleted_at` and stops (`VaultRepository.trash`). Because
  every read in that file already filters the column, that single statement is the whole
  removal as far as the product is concerned -- the memory leaves listings, search, chat
  retrieval, connections, the sweepers and the worker at once -- while the row, its
  chunks and its bucket objects all survive. `VaultRepository.purge` is the old delete
  (scrub, chunks, edges, every version of every object) and runs either on request --
  `DELETE /vault/{id}/permanent`, or `DELETE /vault/trash` for the lot -- or from the beat
  task `purge_expired_trash`, `TRASH_RETENTION_DAYS` (15) later. Six things go with it:
  * **`purged_at` is what separates the two**, and it is not optional: a purged row still
    carries `deleted_at`, so without it the trash page would list empty shells and offer
    a restore that returns a memory with no title, no body and no file. `0017` backfills
    it onto every row deleted under the old behaviour, for exactly that reason.
  * **The chunks survive the trash on purpose.** They carry the vector a restore has to
    put back, and re-deriving them would mean re-embedding -- a restore that costs money
    and can fail. They are already invisible: `search_semantic` joins the item and filters
    `deleted_at`.
  * **`purge` is reachable only for a row already in the trash.** "Delete forever" is a
    second, deliberate action on something the person has already thrown away; a live item
    answers 404 there, which is what stops the route being a one-call destroy.
  * **The two permanent routes carry `assert_same_site`, unlike the rest of this router.**
    The ordinary delete is recoverable now, so the Origin check went to the half that is
    not. Same development consequence as Spaces: browsing `http://localhost:3000` while
    `CORS_ORIGINS` points at a tunnel gets a 403 on those two and nothing else.
  * **The batch is a ceiling, not a page.** Each purge deletes bucket objects one at a
    time, so both the sweep and "empty trash" do `TRASH_PURGE_BATCH` rows per call rather
    than holding one transaction open across hundreds of network round trips. The cutoff
    only moves forward, so a backlog drains over ticks.
  * **Nothing may still call a delete permanent.** The Telegram reply, the agent's
    `ProposeDelete` schema, `AGENT_SYSTEM` and the toolbox's post-mint sentence all say
    trash now: somebody told "gone for good" does not go looking for the restore that
    exists, which is the more expensive half of getting this wrong.
- **`item_metadata` maps to a DB column literally named `metadata`** (renamed because `metadata` is
  reserved on SQLModel classes). Raw SQL must use `metadata`; Python must use `item_metadata`.
- **`EMBEDDING_DIM = 1536` is a padded lie under Gemini, exact under OpenAI.** `text-embedding-004`
  emits 768 dims and `GeminiProvider._fit_dim` zero-pads to fit the `Vector(1536)` column;
  OpenAI's `text-embedding-3-small` is 1536 natively, so nothing is padded. Both keep `_fit_dim`
  so swapping the model cannot silently write a vector the column rejects. **Vectors from the two
  providers are not comparable** — switching provider means a full re-embed, not just a config
  change. Changing the setting itself also needs a migration, since the HNSW index is built on
  the column width.
- Gemini returns prose, not guaranteed JSON — `_parse_tags` strips ``` fences and falls back to
  comma-splitting. Keep new AI outputs equally defensive; `tests/ai/test_gemini_parsing.py` covers this.
- **`ai_highlights` are quotes, and the pipeline enforces that.** The model is asked for exact
  sentences from `content`, and `ai/spans.py::keep_verbatim` then *discards* anything that is not
  actually in the text (whitespace and case are normalised, nothing looser — fuzzy matching starts
  approving paraphrases again). The frontend marks these spans inside the content it already
  renders, so a stored paraphrase would either vanish there or be displayed as words the author
  never wrote. Spans are also length-bounded and de-overlapped, and returned in document order.
  Highlights are only requested when `item.content` exists — an item enriched from its title alone
  has nothing to index into. `tests/ai/test_spans_and_labels.py` pins each rule.
- **`ai_label` is not another tag.** Tags are topical and collide by design (`jobs` belongs to
  hundreds of items); the label is the one line that tells two memories apart in a list, so the
  prompt (`ai/prompts.py`) explicitly rejects generic subject areas. New prompts live in
  `ai/prompts.py` and new parsing in `ai/parsing.py`, shared by both providers — the older
  summary/tags/category prompts stay duplicated inside each provider only because their wording is
  pinned by that provider's tests. **A new provider method must be added to the `AIProvider`
  Protocol *and* to every fake in `tests/`**: the Protocol is structural, so a stub missing the
  method fails at runtime inside `_enrich`, not at type-check time.

- **A hand-edited body is stored TWICE, and the two answer different questions.**
  `PATCH /vault/{id}/content` takes EditorJS *blocks* and nothing else;
  `services/editor_doc.py` derives both halves from one pass, so the browser cannot post a
  `content` that disagrees with the document beside it, and no other column is reachable
  by adding it to the body. `item_metadata["editor_doc"]` keeps the block structure and a
  **small allowlist of inline markup** (`b i u mark code a[href] br`) — that is what the
  reader renders, and rendering the flat text instead is what made an applied heading come
  back looking like an ordinary paragraph. `VaultItem.content` stays the flat projection
  that highlights index into, search matches and the embedding is drawn from; it never
  contains tags.
  The markup is produced by **re-serializing from the allowlist**, never by stripping
  patterns out of the input: `_InlineSanitizer` reads whatever the contenteditable sent
  and writes fresh tags from what it recognises, escaping all text and dropping every
  attribute except a re-validated `href` (`http(s)`/`mailto` only, checked after
  whitespace and control characters are removed). A tag outside the list cannot appear in
  the output because nothing in the output is copied from the input. A `code` block is the
  one exception and is kept verbatim — it comes from a textarea's `.value`, which never
  parses markup, and sanitizing it would delete the user's own examples of the very tags
  this refuses. **The frontend does not treat that as sufficient**: `editor_doc` is a
  JSONB column, so `lib/editor-doc.ts` re-applies the same allowlist before anything
  reaches the editor, and the reader builds React elements rather than setting innerHTML.
  Saving an empty document is refused rather than treated as "clear the body". Two
  consequences to know: `ai_highlights` are re-run through `keep_verbatim` against the new
  text (a quote of a deleted paragraph would otherwise be marked somewhere the model never
  pointed), and the **embedding is deliberately not recomputed** — reprocessing would
  re-fetch `source_url` and overwrite what the user just wrote, so semantic search keeps
  ranking the item by its pre-edit body until something else reprocesses it.

## Latency: one statement is one network round trip

**The database is not local and never will be.** `DATABASE_URL` points at Neon in
`ap-southeast-1` through its **pooler** endpoint, and a round trip from a development
machine measures **~290ms** — a cold connect, TLS and auth included, measures **~1.85s**.
That single number explains every slow endpoint this API has ever had: `/health` answered
in 2ms while `/vault` took 1178ms, and the difference was not code, it was four
statements. Read the request log before optimising anything here —
`jq -c 'select(.event=="request")' logs/api-*.jsonl` carries `duration_ms` per request,
and dividing it by ~290 gives the statement count without a profiler.

So the unit of optimisation on a read path is **statements, not milliseconds**, and the
budget is what the endpoint actually needs:

| Endpoint | Statements |
|---|---|
| `GET /vault`, `GET /search` | 1 |
| `GET /auth/me` | 1 (the `oauth_accounts` join for `linked_providers`) |
| `GET /vault/uploads/limits` | 0 |

Five things hold that budget, and each of them looks like a safe thing to undo:

- **`pool_pre_ping` is OFF** (`DB_POOL_PRE_PING`). It sends `SELECT 1` on every checkout
  to prove a connection is alive, which against a database across a region costs the same
  as the query the request came to run — it was the whole 637ms of an endpoint whose only
  work was the session lookup. `DB_POOL_RECYCLE` (240s) covers the case it existed for by
  discarding a connection idle longer than the shortest timeout in front of Postgres
  (Neon suspends an idle compute at 300s), rather than testing every connection to catch
  the rare stale one. Turn it back on only if stale-connection errors actually appear.
- **The pool is warmed at startup** (`warm_pool`, `DB_POOL_WARMUP`). The ~1.85s handshake
  otherwise lands on whoever arrives first after a deploy or an idle period — precisely
  the page load that gets reported as "the app is broken". It fails soft: the database
  being unreachable at boot must degrade to a slow first request, never to an API that
  will not start.
- **A listing and its `total` are ONE statement.** `VaultRepository._page` uses
  `count(*) OVER ()`, computed inside the scan it is already doing. The `SELECT count(*)`
  + `SELECT … LIMIT` pair reads as obviously correct and silently doubles the cost of
  every page. Its one behavioural difference: a page past the end has no rows and
  therefore no window total, reported as 0.
- **Listings load card columns only** (`_CARD_COLUMNS`, `cards_only=True`), with
  `raiseload=True`. A `SELECT *` listing drags `content`, `item_metadata` and
  `ai_highlights` for every row — an article body is kilobytes, a page is twenty of them,
  and none of it is rendered. `raiseload` is what makes a field added to `VaultItemRead`
  but not to `_CARD_COLUMNS` fail loudly instead of emitting a silent per-row query or a
  `DetachedInstanceError` after the session closes; `tests/core/test_query_shape.py`
  turns it into a test failure instead. **`list_filtered` is deliberately NOT narrowed** —
  it feeds chat retrieval, which needs the bodies.
- **A write does not read itself back.** `UserSessionRepository.add` flushes without
  `refresh()`: every value the caller uses comes from a Python `default_factory`, so the
  refresh was a second round trip for nothing — twice per rotation, on what was already
  the slowest endpoint in the log. Only add `refresh()` back for a value the *database*
  generates and the caller actually reads.

**`get_current_user` caches the `users` row for `AUTH_USER_CACHE_SECONDS` (30s).** Every
authenticated request re-read the same immutable row; nothing under this dependency
writes to the user, and the one place that does mutate one (the OAuth callback) does not
go through it. Four properties keep this out of the security envelope, and a boot guard
enforces the fourth:

- Keyed by the **digest of the access token**, never by user id — a different token
  cannot read another token's entry, and the raw token is never a key in a dict that a
  heap dump or a traceback can reach.
- Only a **live, verified** user is ever inserted, so a hit cannot skip the `deleted_at`
  check; the token's signature is still verified on every single request, and the cached
  row's id is re-checked against the token's `sub` on the way out.
- `forget_cached_user(user_id)` is the hook for account deletion, so it takes effect on
  the next request rather than after the TTL.
- **It does not widen the revocation window; it sits inside one that already exists.**
  Nothing consults the database while an access token is valid, so `logout-all` and
  deletion already take up to `ACCESS_TOKEN_EXPIRE_MINUTES` (capped at 60 outside dev).
  `validate_deployment_config` refuses to start if the TTL exceeds a quarter of that.

**Responses are gzipped** above `GZIP_MIN_BYTES` — vault listings are text and compress
roughly 4:1, and on a phone the transfer is a real share of the wait.

**The single biggest remaining win is not in this repository: colocate the API with the
database.** Every number above is 290ms of physics that no amount of code removes; an API
deployed in `ap-southeast-1` sees ~1-5ms instead, which is roughly a 50x cut to the part
of each request that is not compute. Until then, the frontend should fetch `/auth/me`,
`/vault` and `/vault/uploads/limits` **in parallel** — issued serially they add up, issued
together they cost one round trip. Two smaller levers, both deliberately not taken:
`prepared_statement_cache_size=0` in the URL is required by the pooler and costs a few
extra round trips on the *first* execution of each statement shape per process (the
compiled cache makes every later one 1); and Neon's direct (non-pooled) endpoint would
allow prepared statements at the cost of a much smaller connection ceiling.

## The agent harness

Everything that is not a capture, a command or a status question is answered by **one**
lane: a bounded agent loop that reads the message and picks its own steps. The design is
in `RecallAI — Chat Agent Harness (Design).md` and the build order in
`Build Instruction: RecallAI Chat Agent Harness.md`; what follows is what a person editing
it needs to know.

```
app/ai/chat/harness/{prompts,schemas,tools,graph}.py   the model side
app/services/chat_engine/{context,budget,guard,trace,proposals}.py   the harness side
app/services/recall_agent.py                          the lane, a subclass of RecallChatService
app/services/telegram/confirm.py                      what a tapped button does
```

- **It is `harness/`, not `agent/`.** `app/ai/chat/agent.py` is the older streamed tool
  driver and is still the rung below this one; a package cannot share a module's name.
- **The degradation ladder is literally `super()`.** `RecallAgentService` subclasses
  `RecallChatService`, so "fall back to the older, better-tested path" needs no wiring
  and cannot drift. The fall happens **only before a word has been shown** -- after that
  the turn is final, because repeating sentences a reader has already seen is worse than
  the shorter answer they have. `GraphRecursionError` and the wall-clock timeout take the
  same rule; from the reader's side they are the same event.
- **Every prompt carries a vault snapshot** (`chat_engine/context.py`): the three newest
  rows, one `cards_only` statement, fenced `<vault_snapshot trust="untrusted">` because
  titles come from scraped pages. It is what makes "it", "that" and "my last one" resolve
  to a real row, and it is why "what were my last three?" costs no tool call. `total`
  rides alongside so three rows are never reported as a whole vault.
  **Each row carries both of its links and falls back to the URL for a title**, and both
  were bugs found on a live bot rather than design. Asked for the links to two memories it
  had just listed, it replied *"I can't provide links directly"* -- true of its context,
  false of the vault, and the data was one tool call away. And a fresh capture has no
  title until the pipeline finishes, so the newest row -- the one the next message is
  about -- rendered as an empty `<item></item>` describing nothing. Every other renderer
  already fell back to `source_url`; this one did not.
- **A memory has TWO links and they answer different questions.** `url` is where it came
  from; `link` (`cards.memory_link`) is its page in the vault, and it is the only one a
  note, a recording or an upload has. Handing the second over discloses nothing -- the
  route takes the full UUID and the API re-scopes the row to the session.
  **Both must be in `SurfacedSet.urls`.** `validate_answer` replaces any URL outside that
  set with `[link omitted]`, so a link in the prompt but not in the allowlist is a correct
  answer the guard silently deletes: no error, a hole in the reply, and it reads as a
  model problem rather than a guard one. `tests/chat_engine/test_toolbox.py` pins it.
- **`found_nothing` means "searched and found nothing", not "surfaced nothing".** The
  distinction did not exist before the snapshot and its absence would have been a
  regression: a question the snapshot answers is answered with **no tool call**, and the
  old reading would have thrown that answer away and replaced it with "I couldn't find
  anything in your vault" about memories the person was looking straight at.
- **Budgets do not refuse calls, they answer them differently** (`chat_engine/budget.py`).
  Past the ceiling every tool returns the "budget spent" sentence instead of running, so
  every call still gets its `ToolMessage` -- a call left unanswered is a malformed
  conversation and providers reject the *next* request outright. Then one tool-free round,
  and the turn ends with words.
- **`SurfacedSet` is the answer to two questions and must stay one structure**: which ids
  a reply may cite, and which ids `GetMemory` may open. Both are the same claim -- the
  model saw this. The snapshot seeds it, tool results add to it, nothing else may write
  to it.
- **The agent reads through one composable tool, not a menu of fixed ones.**
  `QueryMemories` lets the model choose the filters *and* the projection (`fields`), which
  is the answerable version of "let it build its own tool" -- it composes a query, and
  nothing anywhere executes text a model wrote. `SearchMemories` / `ListMemories` still
  exist and the older `recall_chat` lane still binds them; the agent lane does not, because
  two narrower tools overlapping this one are two more ways to pick the worse answer.
  Two things are deliberately not the model's to choose: **the links are always returned**
  (a projection that could omit them can reproduce the bug above), and the tenant.
- **A model may propose a write; it may never perform one.** Tool results are scraped
  captions -- exactly the text an attacker gets to write -- so `propose_note` mints a
  single-use token (`chat_engine/proposals.py`) and the write happens in
  `services/telegram/confirm.py` or `POST /chat/proposals/{proposal_token}/accept`, code
  that imports no model. Four rules hold it:
  * **`propose_note` is refused at mint unless the text appears in the person's own
    message this turn** (`from_user_turn`). A model reading an injected caption can be
    talked into proposing anything; it cannot be talked into having been *asked*.
    `proposal_text_not_from_user` is the clearest injection signal this system has.
  * **The token is spent before the write.** A failure between the two leaves nothing to
    retry with, which is the safe direction.
  * **Unknown, expired, spent and wrong-owner all answer identically.** Distinguishing
    them tells whoever found a token in a screenshot what they are holding.
  * **The card shows the exact text that will be written**, never a summary -- that is
    where a person reads what a caption talked the model into and taps No.
  `propose_retry` and `propose_delete` take no text, so their check is the surfaced-id
  one: an id a *memory* mentioned did not come from the vault. Delete waited for the soft
  delete to be real -- offering a tap that runs a half-implemented delete is worse than
  not offering it -- and it says plainly that it is permanent, because it is.
- **`callback_data` is a prefix and a token, never text**: `p:` accept, `p:no:` decline,
  `q:` a tapped answer option. Telegram caps it at 64 bytes. A tapped option is replayed
  through the ordinary inbound path as if typed, so there is no second routing path -- and
  the text is then genuinely the person's own words, which is what lets it stand as
  provenance for a later proposal.
- **`allowed_updates` must list `callback_query`.** Leave it out and taps never arrive at
  all: the button spins on the person's screen and no handler below it ever runs.
- **One `agent_turn` log line per turn** (`chat_engine/trace.py`), carrying
  `prompt_version`, the tool *names* (never their arguments -- those are the model's guess
  at the subject, derived from the person's message), the guard's removals, and the two
  self-reported booleans. `surface`, `shadow` and `router_lane` ride as structlog
  contextvars rather than as fields, the same way `surface` and `intent` already do.
- **Which model drives the loop is a measured choice, not a reputation.**
  `AGENT_CHAT_MODEL` is `gpt-4.1-mini`, and `scripts/bench_agent_model.py` is why: it
  scores a candidate on the four shapes of turn that matter -- the snapshot holds the
  answer, the snapshot does not, out of scope, small talk -- and fails it for markdown, a
  needless tool call, a refusal to hand over a link it was shown. Three trials each, 4/4
  every time, at the lowest output of anything that passed. Re-run it before changing the
  setting; the failures are not marginal. `gpt-4o-mini`, which the loop inherited by
  default for its first release, returns markdown the prompt forbids, and the gpt-5-mini
  family spends the entire output budget on reasoning tokens and returns nothing.
  The script spends real money and lives in `scripts/` for that reason -- `_no_provider_calls`
  would refuse it, and rightly.
- **No test may reach a provider, embeddings included.** `_no_provider_calls` patches the
  chat factory, the agent model, the enrichment client **and**
  `chat_engine.retrieval.get_ai_provider` -- the last was only ever kept off the network
  by every test happening to stub `MemoryRetriever.recall` one level above.

## Known rough edges (real, in the current code)

- ~~`enqueue_process_item` fires before the request session commits.~~ **Fixed.** Every
  save route in `api/v1/vault.py` now takes `enqueue=False`, commits, and then calls
  `VaultService.enqueue` (`_commit_then_enqueue`), which is the order the bot has always
  used. Enqueuing first was a real race: the worker runs in its own transaction, so a
  fast one could dequeue before the row was visible, log `process_missing_item` and leave
  the item at `pending` forever. Any new save path must keep that order —
  `VaultService.enqueue` is public precisely so a caller can. The hand-off is also
  fail-soft: an unreachable Redis logs `vault_enqueue_failed` and leaves the item
  `pending` rather than 500ing the save, and `sweep_stranded_items` re-queues it.
- ~~`VaultItem.deleted_at` exists and every read filters on it, but
  `VaultRepository.delete()` does a hard `session.delete()`.~~ **Fixed.** `delete` now
  writes the tombstone the reads were always looking for, and `get` / `get_unscoped`
  honour it -- neither did, so a soft-deleted row would have stayed fully readable by id
  and would have kept being processed by the worker. Three properties to keep if you
  touch it:
  * **The tombstone is scrubbed.** Title, summary, content, URL, tags, highlights, label,
    category, metadata and the file fields are all cleared. It keeps the id, the owner,
    the kind and the timestamps -- enough to explain a gap, not enough to reconstruct
    anything. Keeping a memory somebody asked to be rid of, so that a hypothetical undo
    could exist, is the opposite of what they asked for.
  * **The chunks go with it**, because they carry the same words *and* the vector drawn
    from them. `search_semantic`'s join also hides them, which is now defence in depth
    rather than the only thing standing between a deleted memory and the index built to
    find it.
  * **The object is deleted for real**, every version of it -- the purge reads
    `storage_key` before the scrub clears it. See the bucket-versioning note above.
  All three now describe the **purge**, not the delete -- see the trash below.
  `tests/vault/test_trash.py` pins each one.
- ~~Rate limiting (`core/middleware.py`) is per-process in-memory.~~ **Fixed.** It counts
  in Redis through `core/rate_limit.consume`, the same function the per-user caps use, so
  there is one implementation of "count things in a window" and one set of fail-open
  semantics. It was wrong in two ways and only one was written down: a count per process
  is a count per replica *and* per uvicorn worker, so the real limit was the configured
  one times the concurrency -- and the dict was never pruned, so every distinct client
  address became a permanent entry and the limiter was a slow memory leak reachable from
  the internet by rotating IPs. Still keyed by client IP, still exempting
  `/api/v1/webhooks/`, still failing open.
  **Consequence for tests:** a live developer Redis now makes this count across tests, so
  `tests/conftest.py`'s autouse `_no_shared_redis_state` stubs it -- without that, the
  sixty-first request in a suite run starts answering 429 to assertions about 401s.
- **Spaces are built; the AI half of them is not.** `/api/v1/spaces` does CRUD,
  membership, invites and the public page. Not built yet, and named here so nobody
  assumes otherwise: the AI *proposal* for a Space (`app/ai/chat/curator.py`), the
  "memories that may belong here" suggestions, and Space-scoped Ask AI. **Connections
  are built**, both per-memory (`/api/v1/connections`) and per-Space. The
  settings for all four are in `app/core/config.py` with their reasoning; the UI shows
  honest empty states rather than placeholders.
- `GET /search` is still `ILIKE` over title/summary/content. Vector search now exists —
  `VaultRepository.search_semantic` orders by `embedding <=> $1` over the HNSW index — but
  only the Telegram bot calls it; the HTTP search endpoint has not been switched over, so
  the two surfaces answer the same question differently. `search_semantic` keeps the
  ordering a bare `ORDER BY … LIMIT n` and dedupes by item in Python on purpose: a
  `DISTINCT ON` makes the planner drop the index.
