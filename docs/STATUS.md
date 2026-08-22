# RecallAI — Build Status & Next Step

Audit of the codebase at commit `e0a1ea9` against `planner.md` / `instruction.md`.
Verified by reading the code, not by trusting the planner checkboxes.

## Where the build actually stands

| Phase | State | Evidence |
|---|---|---|
| 0 — Foundation | **Done except CI** | FastAPI, SQLModel, Alembic, pgvector + pg_trgm + pgcrypto + citext extensions, ARQ, structlog, request IDs, `/health` + `/ready`, docker-compose (db/redis/migrate/api/worker), pytest. `.github/workflows/` is **empty** — the CI task is unticked in reality. |
| 1 — Identity & core data | **Mostly done** | All 7 entities exist (`User`, `VaultItem`, `VaultChunk`, `Collection`, `CollectionItem`, `Subscription`, `AuditLog`). Google OAuth with CSRF state cookie + JWT session. Tenant scoping enforced in repositories. Pagination returns `total`. **Gaps:** no common API error format (no `exception_handler` anywhere), soft delete only half-built, zero authorization tests. |
| 2 — Universal Capture | **Partial** | `POST /vault/save` and `/vault/note` persist immediately and return `pending`. **Gaps:** no idempotency key, URL validated only by `HttpUrl` (no SSRF/private-IP guard), no PDF/image/voice capture path, no tests. |
| 3 — Extraction | **Partial** | `Extractor` Protocol + registry + YouTube (oEmbed) + Article (stdlib HTML parser) with timeouts and fallback ordering. **Gaps:** Apify adapter, Instagram, TikTok, provider usage/cost logging. |
| 4 — AI Enrichment | **Partial** | `AIProvider` Protocol, `GeminiProvider` with `tenacity` retry, defensive tag parsing. **Gaps:** prompts are inline strings (no templates or versioning), category vocabulary is uncontrolled free text, no user-override endpoint. |
| 5 — Background pipeline | **Mostly done** | ARQ worker, `max_tries=4`, `job_timeout=120`, status persisted at every stage, failures recorded and re-raised for retry. **Gaps:** one monolithic job instead of extract/enrich/embed stages, no job table, no dead-letter queue, **and a capture race (see below)**. |
| 6 — Memory Cards & Vault | **Not started** | No frontend exists in this repo. `R2Storage` is written but **never imported** — no PDF, no voice, no chunking, no editor. |
| 7 — Search foundation | **Partial** | Embeddings generated and stored; HNSW index on `vault_chunks.embedding`; trgm GIN indexes on `vault_items.title` and `.summary` already migrated. **Gap: none of it is queried.** `/search` is plain `ILIKE`. No vector search, no fuzzy search, no ranking, no filters. |
| 8–15 | **Not started** | Ask Recall, connections, Spaces UI, sharing, Telegram, digests, billing, hardening. |

`Subscription`, `AuditLog`, and `R2Storage` are scaffolding — defined, migrated, never referenced by any service.

## Fixed on 2026-08-21 while building the test harness

- **`alembic upgrade head` failed on any fresh database.** `User.auth_provider` and
  `Collection.slug` passed `sa_column=Column(...)` with no type, so SQLModel stored `NullType`
  and `0001_initial`'s `create_all` raised `CompileError`. Nobody could provision the project;
  `docker compose up` died at the `migrate` service. Fixed with explicit `Text` / `String(300)`.
- **No row could be inserted through the ORM.** `utcnow()` returns aware datetimes but only
  `updated_at` declared `DateTime(timezone=True)`; the other 13 timestamp columns compiled to
  `TIMESTAMP WITHOUT TIME ZONE` and asyncpg rejected every insert. Models fixed and migration
  `0003_timestamptz` converts existing databases with `AT TIME ZONE 'UTC'`.
- **Cross-tenant IDOR in `POST /collections/{id}/items`.** `CollectionService.add_item` checked
  collection ownership but never the item's, so any user could attach a stranger's vault item to
  their own collection and read its title, summary and content back -- and, if the collection was
  public, expose it to unauthenticated visitors. Confirmed end-to-end by test, then fixed by
  requiring `vault_repo.get(vault_item_id, user_id)`.

## Correctness bugs to fix before adding features

1. **Capture race (highest priority — it breaks the core loop).**
   `VaultService.save_url` calls `enqueue_process_item` *before* the request session commits
   (`get_session` commits at the request boundary). A fast worker dequeues, calls
   `get_unscoped`, finds no row, logs `process_missing_item`, and returns. The item is then
   stuck at `pending` forever with no retry. Fix: enqueue after commit (background task,
   `after_commit` hook, or an outbox row).
2. **Soft delete is half-implemented.** Every read filters `deleted_at IS NULL`, but
   `VaultRepository.delete()` issues a hard `session.delete()`. Planner Phase 1 lists soft
   deletion as a requirement, and instruction §14 says never silently destroy user content.
3. **No common error format.** Errors surface as raw FastAPI `{"detail": ...}`. Instruction §6
   requires a consistent error envelope.
4. ~~No deployment config guards~~ — **fixed**: `validate_deployment_config` refuses non-dev boot
   on a weak `SECRET_KEY`, `COOKIE_SECURE=false`, or wildcard/plaintext/local `CORS_ORIGINS`;
   docs routes are withheld when `ENV=prod`. 25 tests in `tests/test_config_guards.py`.
5. **`ruff check` fails** — 38 pre-existing errors (28 `UP045`, 9 `E501`, 1 `I001`); 29 auto-fixable.
   `mypy` is clean and all 9 tests pass.

## Recommended next step

### Step A — close the Phase 0/1/5 gaps (small, unblocks everything)

- Fix the enqueue-before-commit race.
- Implement real soft delete (`deleted_at` set on delete; hard delete only via purge).
- Add a global exception handler with one error envelope.
- Add the CI workflow the planner already calls for: ruff + mypy + pytest on push.
- ~~Boot guards for `SECRET_KEY`, `COOKIE_SECURE`, `CORS_ORIGINS`, prod docs exposure~~ — done.
- Add the Phase 1 tests that are explicitly listed and entirely missing: authenticated request,
  unauthenticated request, **cross-user access rejection**, soft-delete behavior. This needs the
  first DB test fixture — none exists today (no `conftest.py`).
- `ruff check --fix` the 29 mechanical errors.

### Step B — Phase 7, Hybrid Search (the real next feature phase)

Chosen over Phase 6 deliberately. Every expensive prerequisite is already built and sitting
unused: embeddings are written on every save, the HNSW index exists, the trgm indexes exist.
Phase 7 is pure backend, needs no frontend decision, and it is what unblocks Phase 8 (Ask Recall) —
the product's actual differentiator. Instruction principle 4 states retrieval matters more than
storage; right now the system stores well and retrieves badly.

- Chunking strategy — replace the single "chunk 0" with real splitting for long content.
- Vector search: cosine KNN over `vault_chunks`, scoped by `user_id`.
- Fuzzy search using the existing trgm indexes.
- Merge exact + fuzzy + semantic into one ranked result set behind the existing `/search`.
- Filters: type, category, date range, platform.
- Duplicate detection (Feature 14) — exact and canonical URL match, cheap here.
- Search evaluation fixture, per the planner's testing strategy.

### Deferred pending a decision — Phase 6 frontend

Phase 6 is mostly UI (masonry vault, card detail, smart editor) and there is no Next.js app in
this repo. That needs an answer to: **separate repo, or a `web/` directory here?** The
backend-only slice of Phase 6 — PDF upload to R2, document chunking — can proceed without it and
would reuse the chunking work from Step B.

## API surface today

17 operations across 15 paths, exported to `docs/openapi.json` / `docs/openapi.yaml`.
Regenerate after any route change:

```bash
uv run python scripts/export_openapi.py
```

Browse: `docs/index.html` (Swagger UI, needs internet for the CDN assets), or run the API and
open <http://localhost:8000/docs>.

| Method | Path | Tag |
|---|---|---|
| GET | `/health` | health |
| GET | `/ready` | health |
| GET | `/api/v1/auth/google/login` | auth |
| GET | `/api/v1/auth/google/callback` | auth |
| POST | `/api/v1/auth/logout` | auth |
| GET | `/api/v1/auth/me` | auth |
| POST | `/api/v1/vault/save` | vault |
| POST | `/api/v1/vault/note` | vault |
| GET | `/api/v1/vault` | vault |
| GET | `/api/v1/vault/{item_id}` | vault |
| DELETE | `/api/v1/vault/{item_id}` | vault |
| GET | `/api/v1/search` | search |
| POST | `/api/v1/collections` | collections |
| GET | `/api/v1/collections` | collections |
| GET | `/api/v1/collections/{collection_id}` | collections |
| POST | `/api/v1/collections/{collection_id}/items` | collections |
| GET | `/api/v1/public/{slug}` | public |

Not yet in the spec, and named in the planner: capture for PDF/image/voice, vault item update
(AI overrides), connections, Spaces beyond basic collections, Ask Recall, digests, billing.
