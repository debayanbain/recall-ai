# RecallAI — Implementation Planner

## Objective

Build the complete RecallAI product around the 30 defined features while preserving fast development, low cost, strong UX, and future scalability.

The planner is organized by dependency order, not by arbitrary product-version labels.

---

# Phase 0 — Foundation

## Goal
Create a stable development foundation before product feature work.

### Tasks

- [ ] Create FastAPI project
- [ ] Configure Python 3.12+
- [ ] Configure Pydantic v2
- [ ] Configure SQLModel / SQLAlchemy
- [ ] Configure PostgreSQL
- [ ] Configure Alembic
- [ ] Enable `pgvector`
- [ ] Enable `pg_trgm`
- [ ] Configure Redis
- [ ] Configure background worker framework
- [ ] Configure environment variables
- [ ] Create structured logging
- [ ] Create request IDs
- [ ] Create health endpoint
- [ ] Create Docker setup
- [ ] Create test setup with pytest
- [ ] Create CI pipeline

### Deliverable
A FastAPI application that boots, connects to PostgreSQL/Redis, runs migrations, and passes an initial health test.

---

# Phase 1 — Identity, Tenant Isolation & Core Data

## Goal
Establish user ownership and the canonical memory model.

### Features covered
- User foundation
- Feature 1 Universal Capture foundation
- Feature 8 Memory Card foundation
- Feature 22 Spaces foundation

### Tasks

- [ ] Implement User model
- [ ] Implement Google OAuth
- [ ] Implement secure session/token flow
- [ ] Implement VaultItem model
- [ ] Implement VaultChunk model
- [ ] Implement Collection/Space model
- [ ] Implement CollectionItem model
- [ ] Implement subscription model structure
- [ ] Implement soft deletion
- [ ] Implement tenant ownership checks
- [ ] Implement common pagination
- [ ] Implement common API error format

### Tests

- [ ] User creation
- [ ] OAuth callback
- [ ] Authenticated request
- [ ] Unauthorized request
- [ ] Cross-user data access rejection
- [ ] Soft-delete behavior

### Deliverable
A user can sign in and securely own an empty vault.

---

# Phase 2 — Universal Capture

## Goal
Make saving something extremely fast.

### Features covered
1. Universal Capture
11. Smart Notes
29. Telegram/Chat Capture foundation

### Tasks

- [ ] Create unified capture command/service
- [ ] Create `POST /api/v1/vault/save`
- [ ] Create note capture endpoint
- [ ] Normalize capture input
- [ ] Persist VaultItem immediately
- [ ] Return item ID immediately
- [ ] Add processing status
- [ ] Add idempotency handling where appropriate
- [ ] Add URL validation

### UI / prototype validation

- [ ] Minimal web capture UI
- [ ] Quick note UI
- [ ] Mobile capture concept

### Tests

- [ ] URL capture
- [ ] Note capture
- [ ] Invalid URL
- [ ] Duplicate request
- [ ] Ownership

### Deliverable
A user can save a URL or thought immediately and see `pending` processing state.

---

# Phase 3 — Extraction Engine

## Goal
Turn raw captured URLs into structured content.

### Features covered
2. URL Detection & Routing
3. Content Extraction
4. Apify Integration

### Tasks

- [ ] Create extractor protocol/interface
- [ ] Build extractor registry
- [ ] Build generic article extractor
- [ ] Build YouTube extractor
- [ ] Add metadata normalization
- [ ] Add provider timeout handling
- [ ] Add provider retry handling
- [ ] Add partial-result handling
- [ ] Create Apify adapter
- [ ] Add Instagram URL extraction
- [ ] Add TikTok URL extraction
- [ ] Add provider usage logging

### Tests

- [ ] Domain detection
- [ ] YouTube extraction fixture
- [ ] Article extraction fixture
- [ ] Unsupported platform fallback
- [ ] Apify failure path
- [ ] Timeout path
- [ ] Retry path

### Deliverable
A URL can be converted to normalized `ExtractedContent` independent of the provider used.

---

# Phase 4 — AI Enrichment

## Goal
Make every memory understandable.

### Features covered
5. AI Summarization
6. AI Categorization
7. AI Tag Generation

### Tasks

- [ ] Create AI provider interface
- [ ] Implement Gemini provider
- [ ] Add prompt templates
- [ ] Add structured output validation
- [ ] Implement summary generation
- [ ] Implement category generation
- [ ] Implement tag generation
- [ ] Add AI failure handling
- [ ] Persist AI metadata
- [ ] Allow user overrides

### Tests

- [ ] Structured AI response validation
- [ ] Missing field handling
- [ ] Provider failure
- [ ] Retry
- [ ] User override persistence

### Deliverable
Processed memories contain a useful title, summary, category, and tags.

---

# Phase 5 — Background Processing Pipeline

## Goal
Make all enrichment asynchronous and production-safe.

### Features covered
13. Background Processing Pipeline

### Tasks

- [ ] Create Redis queue
- [ ] Create processing job model/status
- [ ] Create worker
- [ ] Create extraction job
- [ ] Create enrichment job
- [ ] Create embedding job
- [ ] Add retry/backoff
- [ ] Add idempotency
- [ ] Add dead-letter/failure state
- [ ] Add progress/status events if useful
- [ ] Add worker monitoring logs

### Deliverable
Capture returns immediately while processing continues safely in the background.

---

# Phase 6 — Memory Cards & Vault

## Goal
Create the visual product experience.

### Features covered
8. Memory Card Generation
9. PDF/Document Processing
10. Voice-to-Memory
12. Smart Editor

### Tasks

- [ ] Build Memory Card API response
- [ ] Build masonry vault UI
- [ ] Add card filters
- [ ] Add card detail page
- [ ] Implement PDF upload
- [ ] Implement object storage upload
- [ ] Implement PDF extraction
- [ ] Implement document chunking
- [ ] Implement audio upload
- [ ] Integrate transcription provider
- [ ] Build smart editor
- [ ] Add AI Enhance

### Tests

- [ ] Card rendering state
- [ ] PDF upload
- [ ] PDF processing failure
- [ ] Audio transcription failure
- [ ] Editor autosave if enabled

### Deliverable
The user can see a beautiful vault containing links, notes, documents, and voice memories.

---

# Phase 7 — Search Foundation

## Goal
Make stored memories findable before implementing advanced AI retrieval.

### Features covered
15. Semantic Embeddings foundation
16. Hybrid Search foundation

### Tasks

- [ ] Create chunking strategy
- [ ] Persist VaultChunk
- [ ] Generate embeddings
- [ ] Store embeddings in pgvector
- [ ] Add HNSW index
- [ ] Add pg_trgm indexes
- [ ] Implement exact search
- [ ] Implement fuzzy search
- [ ] Implement vector search
- [ ] Build result ranking layer
- [ ] Add filters for type/category/date/platform

### Tests

- [ ] Exact match
- [ ] Fuzzy typo
- [ ] Semantic match
- [ ] User-scoped vector search
- [ ] Empty search
- [ ] Ranking quality fixture

### Deliverable
Users can find memories through normal keywords and conceptual similarity.

---

# Phase 8 — Ask Recall

## Goal
Make retrieval conversational.

### Features covered
17. Ask Recall AI
18. Contextual Retrieval

### Tasks

- [ ] Build query understanding service
- [ ] Extract filters from natural-language queries
- [ ] Run hybrid search
- [ ] Retrieve relevant chunks
- [ ] Build context window
- [ ] Generate grounded answer
- [ ] Attach source memory cards
- [ ] Attach related memories
- [ ] Handle weak/no-result cases
- [ ] Build chat UI

### Example evaluations

- [ ] "What did I save about SaaS pricing?"
- [ ] "Find the reel about protein breakfast."
- [ ] "What did I learn about pgvector?"
- [ ] "Show everything related to my Japan trip."

### Deliverable
The user can ask their vault questions and receive grounded answers with references.

---

# Phase 9 — Memory Relationships

## Goal
Transform isolated memories into contextual knowledge.

### Features covered
19. Memory Connections
20. AI Connection Suggestions
21. Typed Relationships

### Tasks

- [ ] Add memory relationship model
- [ ] Implement manual connect action
- [ ] Implement disconnect
- [ ] Implement relationship types
- [ ] Build related-memory API
- [ ] Create AI similarity candidate generator
- [ ] Create AI suggestion UI
- [ ] Add accept/dismiss actions
- [ ] Prevent duplicate relationships
- [ ] Add connection counts

### Relationship vocabulary

- inspired_by
- related_to
- expands
- supports
- contradicts
- depends_on
- example_of
- part_of

### Deliverable
Each memory can have meaningful related memories without requiring a giant graph UI.

---

# Phase 10 — Spaces

## Goal
Turn memories into curated bodies of knowledge.

### Features covered
22. Spaces
23. AI Space Summary
24. Timeline View

### Tasks

- [ ] Build Space creation
- [ ] Add/remove memories
- [ ] Build Space detail UI
- [ ] Generate AI Space overview
- [ ] Generate topic summary
- [ ] Build timeline API
- [ ] Build timeline UI
- [ ] Show related connections inside Spaces
- [ ] Add Space search
- [ ] Add Ask AI for Space

### Example Spaces

- Building RecallAI
- Startup Ideas
- Japan Trip
- Learning AI
- Recipes

### Deliverable
A user can turn a group of memories into a coherent knowledge Space.

---

# Phase 11 — Sharing

## Goal
Make Spaces useful outside the user's private account.

### Features covered
25. Public Space Sharing
26. Interactive Shared Space
27. Collaborative Spaces
28. Duplicate/Clone Space

### Tasks

- [ ] Public Space routes
- [ ] Public SEO metadata
- [ ] Privacy controls
- [ ] Share modal
- [ ] Read-only mode
- [ ] Interactive Ask AI mode
- [ ] Collaboration membership model
- [ ] Owner/editor/viewer permissions
- [ ] Invite flow
- [ ] Clone Space flow
- [ ] Ensure private data never leaks

### Public page requirements

- No login required for reading
- AI overview
- Topics
- Memories
- Timeline
- Connections
- Optional Ask AI
- Duplicate Space action

### Deliverable
A user can send a link to a friend who can explore the Space without first creating a RecallAI account.

---

# Phase 12 — Chat Capture

## Goal
Make RecallAI available where users already communicate.

### Features covered
29. Telegram / Chat Capture Bot

### Tasks

- [ ] Telegram bot integration
- [ ] Natural-language capture
- [ ] URL forwarding
- [ ] Text note capture
- [ ] Voice-note capture
- [ ] Retrieval commands through natural language
- [ ] Deep-link to focused web editor for long-form writing
- [ ] Deep-link to vault/search results
- [ ] Handle authentication/linking between Telegram and RecallAI

### UX rule
Natural language is primary. Slash commands can exist as shortcuts but should not be required for ordinary capture.

### Deliverable
A user can send a thought, URL, or voice note to RecallAI through Telegram and retrieve saved memories later.

---

# Phase 13 — Resurfacing & Retention

## Goal
Prevent memory graveyard behavior.

### Features covered
30. Resurfacing / Memory Digest

### Tasks

- [ ] Weekly digest generation
- [ ] Optional daily digest
- [ ] Random memory resurfacing
- [ ] Related historical memory resurfacing
- [ ] Action/Things-to-Try extraction where useful
- [ ] Notification preferences
- [ ] Digest rendering
- [ ] Open/click tracking

### Rules

- Do not spam users.
- Allow frequency controls.
- Every resurfaced memory should have a relevance reason when useful.

### Deliverable
The system gives users a reason to return even when they are not actively saving something.

---

# Phase 14 — Billing & SaaS Operations

## Goal
Turn the product into a sustainable SaaS.

### Tasks

- [ ] Define Free/Pro plan limits
- [ ] Add subscription integration
- [ ] Add usage counters
- [ ] Add AI cost tracking
- [ ] Add storage usage tracking
- [ ] Add account export
- [ ] Add account deletion
- [ ] Add privacy settings
- [ ] Add billing portal

Do this after the core product loop is working.

---

# Phase 15 — Quality, Security & Production Hardening

## Tasks

- [ ] Authorization audit
- [ ] Public sharing privacy audit
- [ ] Rate limiting audit
- [ ] SSRF/URL security review
- [ ] File upload security review
- [ ] Prompt injection defenses for retrieved content
- [ ] AI citation/grounding checks
- [ ] Background job idempotency tests
- [ ] Load testing
- [ ] Search relevance evaluation
- [ ] Backup strategy
- [ ] Database restore test
- [ ] Error monitoring
- [ ] Cost alerts

---

# Feature-to-Phase Matrix

| Feature | Phase |
|---|---:|
| 1. Universal Capture | 2 |
| 2. URL Detection & Routing | 3 |
| 3. Content Extraction | 3 |
| 4. Apify Integration | 3 |
| 5. AI Summarization | 4 |
| 6. AI Categorization | 4 |
| 7. AI Tag Generation | 4 |
| 8. Memory Card Generation | 6 |
| 9. PDF/Document Processing | 6 |
| 10. Voice-to-Memory | 6 |
| 11. Smart Notes | 2 / 6 |
| 12. Smart Editor | 6 |
| 13. Background Processing | 5 |
| 14. Duplicate Detection | 7 / 8 |
| 15. Semantic Embeddings | 7 |
| 16. Hybrid Search | 7 |
| 17. Ask Recall AI | 8 |
| 18. Contextual Retrieval | 8 |
| 19. Memory Connections | 9 |
| 20. AI Connection Suggestions | 9 |
| 21. Typed Relationships | 9 |
| 22. Spaces | 10 |
| 23. AI Space Summary | 10 |
| 24. Timeline View | 10 |
| 25. Public Space Sharing | 11 |
| 26. Interactive Shared Space | 11 |
| 27. Collaborative Spaces | 11 |
| 28. Duplicate / Clone Space | 11 |
| 29. Telegram / Chat Capture Bot | 12 |
| 30. Resurfacing / Memory Digest | 13 |

---

# End-to-End Acceptance Journey

The complete product should eventually support this journey:

1. User finds a useful Reel.
2. User shares it to RecallAI.
3. RecallAI immediately acknowledges the capture.
4. Background processing extracts the content.
5. AI creates title, summary, category, and tags.
6. A Memory Card appears in the vault.
7. The system detects related memories.
8. User accepts a connection.
9. User adds the memory to a Space.
10. Space AI generates an overview.
11. Months later the user asks: “Where was that Reel about this?”
12. Hybrid retrieval finds it.
13. Ask Recall returns the card plus connected context.
14. User shares the Space with a friend.
15. Friend opens it without an account.
16. Friend explores the memories.
17. Friend asks AI a question about the Space.
18. Friend clones the Space into their own account.

This is the complete RecallAI product loop.

---

# Testing Strategy

## Backend tests

- pytest
- API integration tests
- Database tests
- Worker tests
- Provider adapter tests
- Authorization tests
- Search evaluation tests

## Frontend tests

- Component tests
- Playwright end-to-end tests

## Critical E2E scenarios

### Capture

Save URL → process → card appears.

### Note

Write note → process → searchable memory.

### Document

Upload PDF → extract → chunk → embed → retrieve.

### Voice

Upload voice → transcribe → retrieve.

### Retrieval

Natural-language query → hybrid search → grounded answer + sources.

### Connections

Open card → connect related card → view relationship.

### Space

Create Space → add memories → AI summary → timeline.

### Sharing

Publish Space → anonymous viewer opens → content is visible → private data remains hidden.

### Collaboration

Invite user → editor adds memory → owner sees it.

### Clone

Public Space → clone → recipient gets private copy.

### Bot

Send URL/voice/text to Telegram → memory appears in the user's vault.

### Digest

Digest generated → user opens → resurfaced memory is clickable.

---

# Development Rule

At every stage:

1. Implement the domain behavior.
2. Add migration.
3. Add tests.
4. Add API.
5. Add minimal UI.
6. Verify the end-to-end flow.
7. Only then move to the next dependency.

Do not build all backend modules in isolation and postpone integration until the end.

The fastest route is to keep the product continuously runnable.
