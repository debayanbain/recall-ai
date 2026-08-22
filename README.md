# RecallAI

AI-powered memory vault. Save YouTube videos, articles, PDFs and notes; AI
summarizes, tags, categorizes and embeds them; search and share collections.

> Built for 10 users, designed to grow to 100k. FastAPI + Postgres(pgvector) +
> Redis/ARQ workers + Gemini Flash.

## Architecture

Everything is a **VaultItem** distinguished by a `type` field — no per-platform
tables. Saving a URL returns instantly and enqueues an async job; the worker
runs the enrichment pipeline.

```
Save URL ──▶ store VaultItem (pending) ──▶ enqueue job ──▶ return 201
                                              │
        worker: detect → extract → summary → tags → category → embedding → store
```

Layers (feature-first, dependency-inverted):

```
api/          FastAPI routers (thin) + deps
services/     business use-cases
repositories/ async data access (SQLModel)
models/       SQLModel tables
extractors/   Strategy pattern per source (YouTube, Article, …)
ai/           AIProvider protocol + GeminiProvider
queue/        ARQ producer + worker
storage/      Cloudflare R2 (S3-compatible)
core/         config, logging, security, middleware
```

Swap points: `ai/factory.py` (AI provider), `extractors/registry.py` (new
sources), all behind protocols so business logic never depends on a vendor.

## Quick start (Docker — recommended)

```bash
touch .env                    # see the Configuration section; not templated, fill by hand
docker compose up --build     # db, redis, migrate, api, worker
```

API: http://localhost:8000  ·  Docs: http://localhost:8000/docs

## Local dev (without Docker)

```bash
uv sync --extra dev                       # install deps
# start Postgres (pgvector) + Redis yourself, then:
alembic upgrade head                      # migrate
uvicorn app.main:app --reload             # API
arq app.queue.worker.WorkerSettings       # worker (separate terminal)
```

## Quality gates

```bash
ruff check app tests      # lint
mypy app                  # strict types
pytest -q                 # tests
```

## API (v1, prefix `/api/v1`)

| Method | Path | Purpose |
|--------|------|---------|
| GET  | `/auth/google/login` | Start Google OAuth |
| GET  | `/auth/google/callback` | OAuth callback → session cookie |
| GET  | `/auth/me` | Current user |
| POST | `/vault/save` | Save a URL (async AI) |
| POST | `/vault/note` | Save a note |
| GET  | `/vault` | List vault (paginated) |
| GET  | `/vault/{id}` | Item detail |
| DELETE | `/vault/{id}` | Delete item |
| GET  | `/search?q=` | Search (ILIKE phase 1) |
| POST | `/collections` | Create collection |
| GET  | `/collections` | List collections |
| POST | `/collections/{id}/items` | Add item |
| GET  | `/public/{slug}` | Public shared collection |

## Notes

- `EMBEDDING_DIM=1536` per spec; Gemini `text-embedding-004` output is padded
  to fit. Set to 768 + reindex for native dimensionality.
- Rate limiting is in-memory (fine for 10 users). Move to Redis token-bucket
  before scaling to multiple API replicas.
