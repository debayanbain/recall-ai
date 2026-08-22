# RecallAI — Product & Engineering Instructions

## 1. Product Definition

RecallAI is an AI-powered personal memory system. It lets users capture anything they do not want to forget, automatically understands and organizes it, connects related memories, and makes the information easy to retrieve and share later.

Core promise:

> Never lose anything important you found, thought, learned, or received again.

Core experience:

```text
Capture → Extract → Understand → Create Memory → Index → Connect → Retrieve → Resurface / Share
```

The product should feel like a second brain, not a bookmark manager, file browser, or chatbot-only product.

## 2. Product Principles

1. Capture must be extremely low friction. A simple save should take about 1–3 seconds.
2. Users should not have to manually categorize every item.
3. AI work happens asynchronously after capture.
4. Retrieval is more important than storage.
5. Every saved item becomes a reusable Memory Card.
6. Related memories should be visible without forcing users into a complicated graph UI.
7. Chat is a capture/retrieval interface, while the vault is the visual consumption interface.
8. Shared Spaces should work for people who do not have a RecallAI account.
9. Provider-specific extraction must be isolated behind adapters.
10. Build a modular monolith first; do not create microservices prematurely.
11. User ownership and tenant isolation are mandatory for every user-owned object.
12. AI suggestions must always be editable or dismissible.

## 3. Recommended Technical Architecture

### Frontend

- Next.js
- TypeScript
- Tailwind CSS
- shadcn/ui
- TanStack Query

### Backend

- FastAPI
- Python 3.12+
- Pydantic v2
- SQLModel / SQLAlchemy-compatible models
- Alembic migrations

### Data

- PostgreSQL
- pgvector
- pg_trgm
- JSONB

### Background Processing

- Redis
- ARQ or an equivalent Redis-backed job system

### Storage

- Cloudflare R2 or S3-compatible object storage

### AI

- A provider abstraction supporting Gemini initially and other providers later

### Extraction

- First-party/public APIs where stable
- Apify for difficult user-submitted URLs such as Instagram/TikTok
- Never couple business logic directly to a single scraper provider

## 4. Canonical Domain Model

Everything important becomes a `VaultItem` / Memory.

Do not create separate business tables such as `youtube_videos`, `instagram_reels`, and `articles` unless a future platform-specific capability genuinely requires it.

Current content types:

- youtube
- article
- pdf
- note
- instagram
- tiktok
- linkedin
- voice
- image

Primary entities:

- User
- VaultItem
- VaultChunk
- Collection/Space
- CollectionItem
- Subscription
- AuditLog

Embeddings should normally live at chunk level for RAG-quality retrieval of long documents.

---

# 5. The 30 Core Features

## Feature 1 — Universal Capture

### Purpose
Create one capture concept for every kind of memory.

### Inputs
- URL
- Text note
- PDF/document
- Image
- Voice
- Future attachments

### UX
Provide a single `Capture` entry point. Users should not have to choose a complicated form before saving.

Examples:

- Share URL → RecallAI → saved
- Type thought → save
- Upload PDF → saved
- Record voice → saved

### Backend
Create a single capture service that normalizes inputs into a VaultItem and optionally creates a processing job.

### Acceptance criteria
- A user can create a memory from every supported input type.
- Every input produces a stable VaultItem ID.
- Capture returns quickly without waiting for AI processing.

---

## Feature 2 — URL Detection & Routing

### Purpose
Identify the source platform and route it to the correct extractor.

### Detection examples
- YouTube
- Instagram
- TikTok
- LinkedIn
- Reddit
- Generic web page
- GitHub

### Design
Use a routing layer:

```python
extractor = extractor_registry.resolve(url)
```

Never put provider-specific extraction logic inside FastAPI routes.

### Acceptance criteria
- Supported domains route deterministically.
- Unsupported URLs fall back to generic article extraction.
- Invalid URLs are rejected clearly.

---

## Feature 3 — Content Extraction

### Purpose
Turn a source URL into structured content that AI and search can use.

### Extract
- Canonical URL
- Title
- Description
- Thumbnail
- Author/creator
- Publish date when available
- Main text
- Transcript where available
- Platform metadata

### Failure behavior
Extraction failures must not destroy the saved memory. Keep the original URL and partial metadata, mark processing as failed, and permit retry.

### Acceptance criteria
- Extraction is provider-independent.
- Partial extraction is allowed.
- Failures are observable and retryable.

---

## Feature 4 — Apify Integration

### Purpose
Support difficult sources without maintaining custom scraping infrastructure.

### Initial use
User-submitted Instagram/TikTok URLs.

### Architecture
Create a provider adapter such as:

```python
class ApifyExtractor:
    async def extract(self, url: str) -> ExtractedContent: ...
```

Do not let Apify response structures leak throughout the application.

### Requirements
- Provider timeout
- Retry policy
- Cost/usage tracking
- Raw response normalization
- Graceful degradation
- Provider replacement capability

### Product rule
Only process URLs explicitly submitted by users. Do not proactively crawl social networks.

---

## Feature 5 — AI Summarization

### Purpose
Convert large content into a concise, useful summary.

### Output
- Short summary
- Optional key takeaways
- Optional action items

### Requirements
- Structured model output
- Stable prompt versioning
- Token limits
- Truncation/chunk strategy
- Retry handling
- Model/provider abstraction

### Acceptance criteria
- Summary is persisted on the VaultItem.
- Original content remains available.
- AI failure does not lose the item.

---

## Feature 6 — AI Categorization

### Purpose
Assign an understandable semantic category automatically.

### Example categories
- Idea
- Learning
- Research
- Business
- Travel
- Recipe
- Fitness
- Work
- Finance
- Reference

### Requirements
- Controlled category vocabulary
- User override
- Confidence score when useful
- No requirement to accept AI classification

---

## Feature 7 — AI Tag Generation

### Purpose
Create useful keywords for browsing and retrieval.

### Requirements
- Generate a small number of high-signal tags
- Deduplicate semantically similar tags
- Allow manual editing
- Store as structured data

Example:

```json
["saas", "customer discovery", "mvp", "validation"]
```

---

## Feature 8 — Memory Card Generation

### Purpose
Turn each saved item into a visually understandable Memory Card.

### Card content
- Thumbnail / visual
- Title
- AI summary
- Source
- Type
- Category
- Tags
- Date
- Connection count

### UX
Use a Pinterest-inspired masonry layout in the vault.

Cards should support quick actions:

- Open
- Share
- Connect
- Favorite
- Archive

### Acceptance criteria
Every successfully processed item has a usable card representation.

---

## Feature 9 — PDF / Document Processing

### Purpose
Treat documents as first-class memories.

### Pipeline
Upload → object storage → text extraction → chunking → AI enrichment → embeddings → searchable memory.

### Requirements
- Preserve file metadata
- Store file in object storage, not PostgreSQL
- Generate thumbnail/preview where possible
- Track page count where possible
- Chunk large content

### Failure behavior
If extraction fails, keep the file and show a retryable processing state.

---

## Feature 10 — Voice-to-Memory

### Purpose
Capture thoughts when typing is inconvenient.

### Pipeline
Audio → transcription → cleanup → category/tagging → embedding → Memory Card.

### UX
A voice capture should feel as simple as sending a voice message.

### Requirements
- Audio upload
- Transcription
- Optional title generation
- Source audio retention according to plan/policy
- Searchable transcription

---

## Feature 11 — Smart Notes

### Purpose
Allow a user to record a thought immediately without configuring a note object.

Example:

> “Compare Supabase vs Neon before choosing the database.”

AI can classify this as research/task/reference without forcing a form.

### Requirements
- One-field capture
- Autosave where appropriate
- AI title generation
- AI category/tag suggestion
- User edit capability

---

## Feature 12 — Smart Editor

### Purpose
Provide a focused browser experience for long-form writing.

Use for:

- Meeting notes
- Research
- Business plans
- Brain dumps
- Long-form ideas

### UI
- Minimal title
- Distraction-free body
- Rich text controls
- Attachments
- Voice insert
- AI Enhance
- Save

Do not make the editor visually complex.

---

## Feature 13 — Background Processing Pipeline

### Purpose
Keep capture fast while heavy processing happens asynchronously.

### Pipeline

```text
capture
→ persist
→ enqueue job
→ extract
→ enrich
→ chunk
→ embed
→ index
→ complete
```

### Job requirements
- Stable job IDs
- Retries
- Backoff
- Idempotency
- Dead-letter/failure state
- Processing status visible to the UI

### Status values
- pending
- processing
- completed
- failed
- skipped

---

## Feature 14 — Duplicate Detection

### Purpose
Prevent the vault from becoming cluttered.

### Detect
- Exact URL duplicate
- Canonical URL duplicate
- Near-duplicate title/content
- Similar semantic content

### UX
When a duplicate is found:

> “You already saved something similar.”

Offer:
- Open existing
- Save anyway
- Merge later

Do not silently delete user content.

---

## Feature 15 — Semantic Embeddings

### Purpose
Support conceptual retrieval beyond keyword matching.

### Storage
Use pgvector.

For long documents, embed `VaultChunk` records instead of only the parent item.

### Requirements
- Version embedding models
- Track dimension/model metadata
- Batch embedding where sensible
- Re-embedding capability

### Important
Do not assume a single vector can represent every long document well.

---

## Feature 16 — Hybrid Search

### Purpose
Combine different search strategies to maximize retrieval quality.

### Layers
1. Exact/ILIKE matching
2. pg_trgm fuzzy matching
3. Metadata filtering
4. Semantic vector search

### Ranking
Merge results into a single relevance ranking.

### Example query

> “that reel about protein breakfast I saved last month”

Search can use:
- text terms
- time filter
- type/platform
- semantic similarity

### Acceptance criteria
The same search endpoint can support both ordinary queries and natural-language queries.

---

## Feature 17 — Ask Recall AI

### Purpose
Let users converse with their own memory vault.

### Example queries
- “What did I save about SaaS pricing?”
- “Where is that reel about intermittent fasting?”
- “Show me everything I learned about pgvector.”

### Response requirements
- Answer
- Referenced memory cards
- Links to source memories
- Connected memories where relevant
- Honest uncertainty when retrieval is weak

Never fabricate that a memory exists.

---

## Feature 18 — Contextual Retrieval

### Purpose
Return the direct match plus useful surrounding context.

Example:

User asks:
> “How did I plan the Japan trip?”

Return:
- Main matching memories
- Related itinerary notes
- Flight research
- Hotel cards
- Restaurant cards
- Connected travel ideas

### Rule
Do not flood users with unrelated content. Use relevance thresholds and explain why related items appear when useful.

---

## Feature 19 — Memory Connections

### Purpose
Let users explicitly connect memories.

Example:

```text
SaaS Validation
       ↓
Customer Interviews
       ↓
Mom Test
```

### UX
A Memory Card action:

`+ Connect`

User searches and selects another memory.

### Requirements
- Bidirectional navigation
- User-owned relationships
- Delete connection
- Optional note

---

## Feature 20 — AI Connection Suggestions

### Purpose
Automatically discover useful relationships.

When a memory is saved, suggest high-confidence related memories.

Example:

> “This looks strongly related to 3 memories you saved earlier.”

### User control
Actions:
- Connect
- Dismiss
- Never suggest this relationship

AI suggestions must not silently modify the user's knowledge structure.

---

## Feature 21 — Typed Relationships

### Purpose
Add semantic meaning to connections.

Supported relationship types can include:

- Inspired by
- Related to
- Expands
- Supports
- Contradicts
- Depends on
- Example of
- Part of

### UI
Keep the relationship label contextual. Avoid showing a giant technical graph by default.

---

## Feature 22 — Spaces

### Purpose
Create curated knowledge areas from memories.

Examples:

- Building RecallAI
- Japan Trip
- Startup Ideas
- Learning AI
- Recipes

A Space is more than a folder. It contains:
- Memories
- Connections
- AI overview
- Timeline
- Search
- Ask AI
- Sharing

### UX
Spaces should feel like mini knowledge products.

---

## Feature 23 — AI Space Summary

### Purpose
Automatically explain what a Space contains.

Output:
- Overview
- Key topics
- Major themes
- Important memories
- Optional chronology

Update summaries when material changes significantly.

---

## Feature 24 — Timeline View

### Purpose
Show how a user's thinking or research evolved over time.

### Display
- Month/day clusters
- Memory cards
- Important events
- Connections

### Useful for
- Projects
- Research
- Learning
- Trips
- Long-running ideas

Timeline should not require manual date organization beyond actual memory timestamps and source dates.

---

## Feature 25 — Public Space Sharing

### Purpose
Turn a Space into a shareable public knowledge page.

### Behavior
User clicks Share → Read Only → public URL.

Example:

```text
recallai.app/s/user/space-slug
```

### Public page
- Space title
- Author
- AI overview
- Topics
- Memories
- Timeline
- Connections
- Ask AI

Visitors do not need an account just to read the page.

### Privacy
Private is the default. Public sharing must be explicit.

---

## Feature 26 — Interactive Shared Space

### Purpose
Let recipients ask questions about the shared Space.

Example:

> “What are the three most important things in this research?”

The AI must ground responses only in the allowed shared content.

### Permission model
The owner controls whether AI interaction is enabled.

---

## Feature 27 — Collaborative Spaces

### Purpose
Allow multiple users to contribute to the same Space.

### Permissions
At minimum:
- Owner
- Editor
- Viewer

### Requirements
- Invite members
- Remove members
- Role management
- Ownership checks
- Auditability for sensitive actions

---

## Feature 28 — Duplicate / Clone Space

### Purpose
Turn shared knowledge into a reusable starting point.

Public Space should expose:

`Duplicate Space`

The recipient gets a private copy they can modify.

### Requirements
- Copy Space metadata
- Copy allowed memories/references
- Copy structure and relationship data where permitted
- Do not copy private source data that was not shared

---

## Feature 29 — Telegram / Chat Capture Bot

### Purpose
Provide frictionless capture and basic retrieval in a chat surface.

### Behavior
Natural language first.

Examples:

User:
> “I just had an idea.”

Bot:
> “Tell me.”

User sends text or voice.

Bot:
> “Saved.”

For long-form writing or advanced uploads, send the user to a focused RecallAI web editor.

### Important UX rule
Do not force users to memorize commands such as `/notes`, `/voice`, `/pdf` for normal usage. Commands can exist as secondary shortcuts, but the primary path is natural language and URL sharing.

### Retrieval
Support queries such as:
- “Find the reel I saved about fasting.”
- “Show my latest SaaS ideas.”
- “What did I save about FastAPI?”

---

## Feature 30 — Resurfacing / Memory Digest

### Purpose
Stop the vault from becoming a graveyard.

### Experiences
- Weekly digest
- Daily optional recap
- Random rediscovery
- Relevant old memory resurfacing
- Things-to-try/action extraction where available

### Example

> “This week you saved 12 items about AI agents. Three of them connect to research you saved in March.”

### Rules
Resurfacing must be useful, not spammy. Give users frequency controls.

---

# 6. Cross-Cutting Engineering Requirements

## Authentication

- Google OAuth initially
- Secure session/token handling
- Tenant isolation
- Account deletion

## API

Version APIs under `/api/v1`.

Use:
- Pydantic request/response schemas
- Dependency injection for auth
- Consistent error format
- Pagination
- Idempotency for capture endpoints where needed

## Security

- Validate all inputs
- Validate URLs
- Rate limit public and expensive endpoints
- Never trust client-provided `user_id`
- Enforce ownership at repository/service level
- Signed/private object-storage URLs where appropriate
- Protect public sharing routes from accidental private-data exposure

## Observability

Track:
- Request ID
- Processing job ID
- Extraction latency
- AI latency
- Search latency
- Processing failures
- Retry counts
- Provider failures

## Testing

Every feature should have:

1. Unit tests
2. API/integration tests
3. Background-job tests where applicable
4. Authorization/tenant-isolation tests
5. Failure-path tests

For retrieval features, create a small evaluation dataset and test relevance, not just code coverage.

## Cost Controls

- Do not call an expensive model when a cheaper deterministic operation works.
- Cache immutable extraction results where appropriate.
- Deduplicate repeated URLs.
- Avoid embedding unchanged content twice.
- Limit AI context to relevant chunks.
- Track AI cost per operation and per user.

## Performance

Targets:
- Capture acknowledgement: fast and non-blocking
- Normal search: sub-second target at MVP scale
- AI enrichment: asynchronous
- Public Space pages: cache-friendly

## Product Analytics

Instrument product events such as:

- memory_captured
- processing_completed
- search_performed
- ai_question_asked
- connection_created
- space_created
- space_shared
- public_space_viewed
- space_cloned
- bot_capture_used
- digest_opened

Do not allow analytics to become a dependency of core product behavior.

---

# 7. Definition of Done

A feature is not considered complete when the happy path works once.

It is complete when:

- API is implemented
- Database migration exists
- Authorization is enforced
- Error states are handled
- Tests exist
- UI state exists for loading/success/failure where relevant
- Background jobs are retryable where applicable
- Logs/metrics exist for production failures
- Documentation is updated

# 8. Development Rule

Always build the smallest production-quality implementation that satisfies the behavior.

Do not add speculative complexity.

Prefer a modular monolith until scale or organizational boundaries actually require extraction into services.
