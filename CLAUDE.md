# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync --extra dev                          # install deps (Python >=3.11)
```

There is no docker-compose — the database is hosted (Neon), set via `DATABASE_URL` in `.env`.
The API and worker are two separate processes; the worker is only needed for AI processing:

```bash
uv run alembic upgrade head                         # migrate (needs Postgres w/ pgvector)
uv run uvicorn app.main:app --reload                # API   -> :8000, docs at /docs
uv run celery -A app.queue.celery_app.celery_app worker   # worker -- needs Redis
uv run celery -A app.queue.celery_app.celery_app beat     # sweeper for lost callbacks
```

Quality gates (CI is not wired up — `.github/workflows/` is empty, so run these by hand):

```bash
uv run ruff check app tests                         # line-length 100, rules E,F,I,UP,B,ASYNC
uv run mypy app                                     # strict = true
uv run pytest -q
uv run pytest tests/extractors/test_extractors.py::test_generic_url_falls_back_to_article   # single test
```

Baseline as of this file: `mypy` is clean (85 files) and `pytest -q` is 190 passed / 54 skipped
(the skips are every DB-backed test — see below), but `ruff check` reports **30 pre-existing
errors** — 28 `UP045` (`Optional[X]` instead of `X | None`, mostly in `app/models/vault.py`) and
2 `E501`, all `--fix`-able. Do not read a red ruff run as damage you caused; check whether your
files are among the offenders.

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
  recoverable. `RecallChatService.respond` then splits chat from retrieval on
  `planner.looks_like_question`, which costs no tokens -- and `ai/chat/chain.converse` is
  a **separate chain** from `answer` on purpose: `answer`'s whole rule is "speak only
  from the MEMORY blocks", which holds precisely because it is never handed an empty
  context. `first_url` beats phrasing, so "what is this? <link>" saves rather than asks.
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
  type; the magic-byte check still decides what the bytes actually are. Voice notes are
  refused out loud — there is no ASR and no audio MIME in the allowlist, and a silent drop
  reads as a bug.
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
- **An upload that carries no readable text is `skipped`, not `failed`.** A PDF's or a
  .txt's text goes through the normal AI pipeline; an image or a .docx is stored and
  downloadable but never sent to the model -- there is no OCR and no OOXML parser, and
  calling the model on an empty string spends tokens to hallucinate about a filename.
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

## Known rough edges (real, in the current code)

- `enqueue_process_item` fires inside `VaultService.save_url` *before* the request session commits.
  A fast worker can dequeue the job before the row is visible, log `process_missing_item`, and leave
  the item stuck at `pending`. Enqueue after commit if you touch this path. The hand-off itself is
  now fail-soft (`VaultService._enqueue`): the row is already persisted, so an unreachable Redis
  logs `vault_enqueue_failed` and leaves the item `pending` rather than 500ing the save. That does
  not fix the commit race — it only stops a missing queue from breaking capture.
- `VaultItem.deleted_at` exists and every read filters on it, but `VaultRepository.delete()` does a
  hard `session.delete()`. Reads assume soft delete; writes do not implement it.
- Rate limiting (`core/middleware.py`) is per-process in-memory — correct only for a single API
  replica.
- `GET /search` is still `ILIKE` over title/summary/content. Vector search now exists —
  `VaultRepository.search_semantic` orders by `embedding <=> $1` over the HNSW index — but
  only the Telegram bot calls it; the HTTP search endpoint has not been switched over, so
  the two surfaces answer the same question differently. `search_semantic` keeps the
  ordering a bare `ORDER BY … LIMIT n` and dedupes by item in Python on purpose: a
  `DISTINCT ON` makes the planner drop the index.
