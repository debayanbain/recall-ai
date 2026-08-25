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
uv run pytest tests/test_extractors.py::test_generic_url_falls_back_to_article   # single test
```

Baseline as of this file: `mypy` is clean (53 files) and all 9 tests pass, but `ruff check` reports
**38 pre-existing errors** — 28 `UP045` (`Optional[X]` instead of `X | None`, mostly in
`app/models/vault.py`), 9 `E501`, 1 `I001`. 29 are `--fix`-able. Do not read a red ruff run as
damage you caused; check whether your files are among the offenders.

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

`pytest` runs with `asyncio_mode = "auto"` — do not add `@pytest.mark.asyncio`. There is no
`conftest.py` and no DB/Redis fixtures: the existing tests are pure and offline (extractor
routing, Gemini response parsing). Anything needing a database has no harness yet.

**Every new migration must be idempotent.** `0001_initial` calls
`SQLModel.metadata.create_all()`, so it builds the schema from the *current* models rather than a
frozen snapshot: on a fresh database it already creates whatever the latest models declare, and an
unguarded `ADD COLUMN` / `CREATE TABLE` in a later revision then aborts the entire upgrade (Alembic
wraps it in one transaction, so the DB rolls back to empty). Guard with
`sa.inspect(op.get_bind())` — see `0004_oauth_accounts` and `0005_instagram_accounts`.

New migration: `alembic revision --autogenerate -m "..."`. `alembic.ini` leaves `sqlalchemy.url`
empty on purpose — `migrations/env.py` injects it from `settings.database_url_str` and imports
`app.models` so `SQLModel.metadata` is populated. A model that is not re-exported from
`app/models/__init__.py` is invisible to autogenerate.

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
  no cookie, no state, wrong nonce, different user — fails closed; `tests/test_integrations_instagram.py`
  pins each one.
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
  rules, not more string checks. `tests/test_ssrf_guard.py` pins the cases.
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
  comma-splitting. Keep new AI outputs equally defensive; `tests/test_gemini_parsing.py` covers this.

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
- Search is `ILIKE` over title/summary/content. The embeddings and the HNSW index are written but
  nothing queries them yet; vector search is unimplemented.
