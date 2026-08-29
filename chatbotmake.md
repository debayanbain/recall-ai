# RecallAI — Chat Engine + RAG Architecture Implementation Specification

## Objective

Refactor RecallAI so that:

1. Telegram is only a **surface/adapter**.
2. The chat engine is **platform-independent**.
3. Every inbound message is converted into a normalized `InboundMessage`.
4. The chat engine decides the message intent before spending on retrieval or an LLM.
5. Memory retrieval happens only for the `RECALL` intent.
6. Retrieval sends **compact memory cards**, not full document contents, to the LLM by default.
7. Full content is fetched only when the user explicitly needs detailed information.
8. Telegram-specific HTML formatting stays entirely inside the Telegram surface layer.
9. Chat history is capped to reduce input tokens.
10. Existing tenant isolation and existing ingestion/storage pipelines remain unchanged.

The guiding rule is:

> **Decide intent first, retrieve only when necessary, send the smallest useful context, then call the LLM.**

---

# 1. Non-goals

Do NOT implement any of the following in this refactor:

* WhatsApp integration
* Discord integration
* Instagram DM integration
* New social connectors
* Unified webhook verification
* `surface_accounts` database migration
* New database migrations
* Hybrid search
* Reciprocal Rank Fusion (RRF)
* Keyword search
* Reranking
* Chunking
* Query rewriting
* Re-embedding
* Merged enrichment calls
* New extraction pipeline
* Changes to authentication
* Changes to worker architecture
* Changes to existing `VaultItem` schema
* Changes to ingestion/extraction behavior except where strictly required for this refactor
* Replacing the existing vector search implementation
* Moving the existing write-side embedding/enrichment pipeline

Put future ideas into `docs/roadmap.md` only if that file already exists or can be safely created without touching unrelated code.

---

# 2. Hard scope rule

Only modify files necessary for this architecture.

Primary expected files:

```text
app/services/chat_engine/types.py
app/services/chat_engine/router.py
app/services/chat_engine/retrieval.py
app/services/chat_engine/cards.py
app/services/chat_engine/engine.py

app/services/surfaces/telegram/parse.py
app/services/surfaces/telegram/render.py
app/services/surfaces/telegram/client.py

app/ai/prompts.py

app/ai/chat/history.py

app/services/telegram/dispatch.py
app/services/recall_chat.py
```

The exact existing paths may differ. First inspect the repository and map the existing implementation to this architecture.

Do NOT blindly rename large parts of the application.

Do NOT create duplicate implementations when existing functionality can be moved behind an interface.

---

# 3. Architecture

Target architecture:

```text
                              USER
                               │
                               ▼
                    ┌────────────────────┐
                    │ Telegram Webhook   │
                    └─────────┬──────────┘
                              │
                              ▼
                    ┌────────────────────┐
                    │ Telegram Adapter   │
                    │                    │
                    │ parse.py           │
                    └─────────┬──────────┘
                              │
                              ▼
                       InboundMessage
                              │
                              ▼
                    ┌────────────────────┐
                    │    Chat Engine     │
                    │                    │
                    │ router              │
                    │ retrieval           │
                    │ cards               │
                    │ conversation        │
                    └─────────┬──────────┘
                              │
                         OutboundReply
                              │
                              ▼
                    ┌────────────────────┐
                    │ Telegram Renderer  │
                    │                    │
                    │ render.py          │
                    └─────────┬──────────┘
                              │
                              ▼
                           Telegram
```

The dependency rule is:

```text
Telegram Surface
      ↓
Chat Engine
      ↓
AI / Repository / Existing services
```

Never the reverse.

The chat engine must NOT import Telegram classes, Telegram webhook payload types, Telegram HTML helpers, or Telegram client implementations.

---

# 4. Folder structure

Create this logical structure:

```text
app/
├── services/
│   ├── chat_engine/
│   │   ├── __init__.py
│   │   ├── types.py
│   │   ├── router.py
│   │   ├── retrieval.py
│   │   ├── cards.py
│   │   └── engine.py
│   │
│   └── surfaces/
│       └── telegram/
│           ├── __init__.py
│           ├── parse.py
│           ├── render.py
│           ├── client.py
│           └── limits.py
│
└── telegram/
    └── dispatch.py
```

Adapt names to the existing repository if necessary, but preserve these responsibilities.

---

# 5. `types.py`

Create platform-neutral message and response types.

Example:

```python
from dataclasses import dataclass, field
from typing import Literal

SurfaceName = Literal["telegram"]

@dataclass(frozen=True)
class Attachment:
    data: bytes
    mime_type: str
    filename: str | None = None


@dataclass(frozen=True)
class InboundMessage:
    surface: str
    external_user_id: str
    external_chat_id: str
    text: str | None
    attachments: list[Attachment] = field(default_factory=list)
    is_private: bool = True


@dataclass(frozen=True)
class TextBlock:
    text: str


@dataclass(frozen=True)
class ItemListBlock:
    items: list[object]


@dataclass(frozen=True)
class ErrorBlock:
    message: str


Block = TextBlock | ItemListBlock | ErrorBlock


@dataclass(frozen=True)
class OutboundReply:
    blocks: list[Block]
```

Preserve strong typing.

Do not expose Telegram types here.

If the project already has equivalent domain types, reuse them rather than creating duplicates.

---

# 6. Router

Create:

```text
app/services/chat_engine/router.py
```

It must be pure business logic.

No:

* database calls
* network calls
* model calls
* async
* Telegram imports

Use an enum:

```python
from enum import Enum

class Intent(str, Enum):
    COMMAND = "command"
    CAPTURE = "capture"
    META = "meta"
    RECALL = "recall"
    CHAT = "chat"
```

And:

```python
def route(text: str | None) -> Intent:
    ...
```

### Routing order is mandatory

Use exactly this priority:

```text
1. COMMAND
2. CAPTURE
3. META
4. RECALL
5. CHAT
```

### COMMAND

Return `COMMAND` when the text starts with `/`.

Examples:

```text
/start
/recent
/search redis
/help
```

Do not move command execution into the router.

The router only identifies the intent.

Existing command dispatch remains responsible for command handling.

### CAPTURE

Use the existing `first_url` logic or existing URL detector.

A URL should be recognized before META/RECALL/CHAT.

Example:

```text
https://youtube.com/...
```

returns:

```text
CAPTURE
```

Do not change the existing capture pipeline.

### META

Recognize lightweight bot-identity/help queries using a small explicit pattern set.

Examples:

```text
who are you
what is your name
what can you do
who made you
are you a bot
are you human
help
```

A question mark alone must never cause `RECALL`.

This is the bug being fixed.

### RECALL

Use memory-related signals rather than punctuation.

Examples:

```text
what did I save about redis
what did I save this week
show me what I saved
find my notes about docker
did I save anything about postgres
remember what I saved about AI
last week
this month
```

A question mark by itself is NOT evidence of `RECALL`.

### CHAT

Everything else falls back to `CHAT`.

Examples:

```text
hi
hii
hello
tell me a joke
what is dependency injection
how are you
```

Normal chat must not trigger memory retrieval automatically.

---

# 7. Router tests

Create:

```text
tests/ai/test_router.py
```

At minimum test:

```text
/start                             → COMMAND
/recent                            → COMMAND

https://youtube.com/foo            → CAPTURE

Who are you?                       → META
What is your name?                 → META
What can you do?                   → META
Are you a bot?                     → META
Who made you?                      → META
Help                               → META

What did I save this week?         → RECALL
What did I save about Redis?       → RECALL
Show me my saved AI notes          → RECALL
Did I save anything about Docker?  → RECALL
Find my notes about Postgres       → RECALL

Hi                                 → CHAT
Hii                                → CHAT
Tell me a joke                     → CHAT
What is Node.js?                   → CHAT
```

Also test precedence:

```text
/start?foo=bar
```

must remain `COMMAND`.

A URL containing words such as "help" must remain `CAPTURE`.

---

# 8. Bot identity

In:

```text
app/ai/prompts.py
```

add:

```python
BOT_IDENTITY = """
You are RecallAI, a personal AI memory assistant.
You help users save links, files, and notes and answer questions about what they saved.
You should clearly identify yourself as RecallAI when asked about your identity or capabilities.
""".strip()
```

Keep it short.

Inject this into the `converse` system prompt.

Do NOT modify unrelated `answer` chains.

The model must never need retrieval to answer:

```text
Who are you?
What can you do?
What's your name?
```

---

# 9. Chat Engine

Create:

```text
app/services/chat_engine/engine.py
```

The main public method should conceptually be:

```python
async def handle(self, message: InboundMessage) -> OutboundReply:
    ...
```

The engine should:

```text
InboundMessage
      ↓
route()
      ↓
Intent
```

Then:

```text
COMMAND
    → hand back to existing command handling mechanism

CAPTURE
    → hand back to existing capture/save mechanism

META
    → converse / fixed response
    → NO retrieval

RECALL
    → MemoryRetriever
    → memory cards
    → answer generation

CHAT
    → converse
    → NO retrieval
```

Do not duplicate command or capture behavior.

Integrate with the existing services.

---

# 10. Important distinction: CHAT vs RECALL

Do not turn every question into memory retrieval.

This must work:

```text
User:
What is dependency injection?

Intent:
CHAT

Behavior:
Normal conversational LLM response.
No memory retrieval.
```

And:

```text
User:
What did I save about dependency injection?

Intent:
RECALL

Behavior:
Retrieve memories → cards → answer.
```

This distinction is the core routing improvement.

---

# 11. Retrieval / RAG

Create:

```text
app/services/chat_engine/retrieval.py
```

This is the read side of RAG.

The write side already exists in the existing processing/ingestion pipeline and must not be moved.

Conceptually:

```python
class MemoryRetriever:
    async def recall(
        self,
        user_id,
        question: str,
        filters=None,
    ) -> list[VaultItem]:
        ...
```

Responsibilities:

1. Generate/reuse the question embedding.
2. Call the existing semantic search implementation.
3. Apply existing user/tenant scoping.
4. Return relevant `VaultItem` objects.
5. Do not generate the final LLM answer.

The retriever must remain model-light.

It may use the embedding model because vector retrieval needs one, but it must not call the answer LLM.

---

# 12. Tenant isolation

This rule is mandatory.

The retrieval path must always scope results to the current user.

Use:

```python
search_semantic(user_id=...)
```

or the repository's equivalent scoped method.

Never use:

```python
get_unscoped(...)
```

inside the chat/retrieval path.

`get_unscoped` must not be reachable from conversational retrieval.

Add a regression test if there is an existing repository test pattern for tenant isolation.

---

# 13. RAG flow

The recall flow must be:

```text
User question
      ↓
Router
      ↓
RECALL
      ↓
MemoryRetriever
      ↓
Semantic search
      ↓
Top relevant VaultItems
      ↓
Memory cards
      ↓
Hard context budget
      ↓
LLM
```

Do NOT send raw full `VaultItem.content` into the default memory context.

---

# 14. Memory cards

Create:

```text
app/services/chat_engine/cards.py
```

This is the context-compression layer.

A memory card should contain only the information needed to identify and understand the saved item at a high level.

Example:

```text
[a3f1] Sweden education job series ep 2
jobs · sweden · education — saved 12 Aug
Teaching roles and visa routes in Sweden.
- applications open in September
- visa route discussed
```

Use existing stored fields:

* `id`
* `ai_label`
* `category`
* `tags`
* saved/created date
* `summary`
* at most 2 `ai_highlights`

Do not add database fields.

Do not regenerate the summary.

Do not call the LLM to build cards.

---

# 15. `build_card`

Create:

```python
def build_card(item: VaultItem) -> str:
    ...
```

Requirements:

* Compact
* Deterministic
* No LLM call
* Summary capped around 200 characters
* Maximum 2 highlights
* Include enough metadata to distinguish memories
* Avoid dumping `content`

---

# 16. `build_context`

Create:

```python
def build_context(
    items: list[VaultItem],
    budget: int = 1200,
) -> str:
    ...
```

Use a simple token estimate:

```python
estimated_tokens = len(text) // 4
```

Keep adding cards until adding the next card would exceed the budget.

The budget is authoritative.

Do not merely use:

```text
top 8 items
```

because cards can have different sizes.

The output must stay inside the configured approximate budget.

---

# 17. Full-content retrieval

Default behavior:

```text
Retrieve
  ↓
Cards
  ↓
LLM
```

Do not fetch full content unless the user's question clearly requests detail.

Examples of detail intent:

```text
What exactly did that article say?
Explain the details from that saved post.
What did the article say about cache invalidation?
```

For these cases:

```text
Retrieve
  ↓
Best 1–2 items
  ↓
Fetch full content
  ↓
Answer
```

Do NOT fetch full content for every recall query.

Do not implement a complicated intent classifier for detail in this iteration. Use a small deterministic check or the existing planner signal if one already exists and is safe.

---

# 18. Zero-result short circuit

Preserve and extend the existing behavior.

If retrieval returns no relevant memories:

```text
Do not call the expensive answer LLM unnecessarily.
```

Return the existing fixed “nothing found” style response, adapted to the new `OutboundReply` type.

Do not allow the model to invent a memory.

This is a hard requirement.

---

# 19. Chat history

In:

```text
app/ai/chat/history.py
```

keep only the latest 4 turns/messages used for generation.

Do NOT implement summarization in this refactor.

If the existing history implementation returns more than 4 turns, slice it before constructing the prompt.

Goal:

```text
last 4 turns only
```

This is a cost-control mechanism.

---

# 20. Output limits

Configure the chat LLM with an appropriate `max_output_tokens` limit using the provider's existing API.

Do not create a new provider abstraction.

Add to the relevant prompts:

```text
Answer in under 4 sentences unless the user explicitly asks for more detail.
```

Do not use excessive output for Telegram.

---

# 21. Telegram surface adapter

Telegram-specific code must stay in:

```text
app/services/surfaces/telegram/
```

### `parse.py`

Convert the Telegram payload into:

```python
InboundMessage
```

This is the only place that should understand the raw Telegram webhook/update structure.

It may extract:

* Telegram user ID
* Telegram chat ID
* text
* downloaded attachments
* private/group context

Do not pass Telegram `Update` objects into the chat engine.

---

# 22. Telegram renderer

Create:

```text
app/services/surfaces/telegram/render.py
```

This is the only place that knows Telegram HTML.

The engine must return blocks.

The renderer converts blocks to Telegram-safe HTML.

It owns escaping for:

```text
&
<
>
```

and any other Telegram HTML escaping required by the existing implementation.

Do not return Telegram HTML from `ChatEngine`.

Do not put Telegram tags into `OutboundReply`.

---

# 23. Fix the stray `- ` bug

The existing formatting bug:

```text
- Hi there!
```

must not appear because of accidental markdown bullets.

In the Telegram rendering/formatting layer, for a single-line reply:

```text
- Hello
* Hello
# Hello
```

may be normalized appropriately.

Do NOT alter multi-line Markdown behavior globally unless the existing formatter requires it.

Keep this fix isolated to display/rendering.

---

# 24. Telegram client

The client is responsible only for sending the rendered result to Telegram.

It must not contain:

* routing logic
* retrieval logic
* RAG logic
* prompt logic
* business intent detection

Keep request URL logging behavior unchanged and never log sensitive request URLs.

---

# 25. Dispatch

The current Telegram dispatch layer may continue to own webhook verification and Telegram entry-point concerns.

But message-shape routing must move out of:

```text
telegram/dispatch.py
```

and into:

```text
chat_engine/router.py
```

The dispatch layer should become approximately:

```text
Telegram webhook
   ↓
parse()
   ↓
InboundMessage
   ↓
ChatEngine.handle()
   ↓
OutboundReply
   ↓
render()
   ↓
Telegram client
```

Do not put ChatEngine logic back into dispatch.

---

# 26. Existing command handling

Do not rebuild `/start`, `/recent`, `/search`, etc.

Existing command dispatch remains responsible for actual command execution.

The router only answers:

```text
"This is a command."
```

Preserve all existing command behavior.

---

# 27. Existing capture handling

Do not rebuild the extraction or save pipeline.

A capture:

```text
URL
```

must continue to enter the existing processing pipeline.

The chat engine should identify it as `CAPTURE` and hand it to the existing capture logic.

Do not move ingestion into `chat_engine`.

---

# 28. RAG vs write-side processing

Keep the architecture explicitly split.

## Write side

Existing:

```text
URL / File
   ↓
Extract
   ↓
Enrichment
   ↓
Summary
Tags
Category
Label
Highlights
   ↓
Embedding
   ↓
VaultItem
```

Do not move this.

## Read side

New:

```text
Question
   ↓
RECALL
   ↓
MemoryRetriever
   ↓
Existing semantic search
   ↓
VaultItems
   ↓
Memory Cards
   ↓
Context Budget
   ↓
LLM
```

This is the RAG read path.

---

# 29. Cost-control principles

The implementation must optimize in this order:

```text
1. Avoid unnecessary LLM calls.
2. Avoid unnecessary retrieval.
3. Retrieve only relevant memories.
4. Send cards instead of full content.
5. Keep a hard context budget.
6. Keep chat history small.
7. Limit output size.
```

Do not optimize by adding complexity.

---

# 30. Expected token architecture

The target default recall prompt should look approximately like:

```text
SYSTEM PROMPT
+ BOT IDENTITY
+ LAST 4 CHAT TURNS
+ COMPACT MEMORY CARDS
+ USER QUESTION
```

Not:

```text
SYSTEM PROMPT
+ HUGE CHAT HISTORY
+ 8 FULL ARTICLES
+ USER QUESTION
```

A rough target is:

```text
History:      ~250 tokens
Cards:        ~800 tokens
System:       ~300 tokens
Question:      ~20 tokens
--------------------------------
≈ 1,370 input tokens
```

These are targets, not hard numerical guarantees.

Add logging so actual usage can be measured.

---

# 31. Token logging

Use the project's existing JSONL logging infrastructure.

For each model request, capture where possible:

```text
request_id
surface
intent
model
input_tokens
output_tokens
latency_ms
```

Do not introduce a new observability system.

This is needed to verify that the architectural changes actually reduce token consumption.

---

# 32. Surface abstraction

Create a minimal conceptual surface protocol only if it fits naturally into the existing project.

Expected responsibilities:

```text
parse inbound platform payload
render outbound blocks
deliver rendered result
```

Do not build a generic platform framework.

One working Telegram adapter is enough.

---

# 33. Account lookup rule

Never use an item's metadata as the final reply destination.

Do NOT trust:

```python
item_metadata["chat_id"]
```

to decide where to send the message.

The reply destination must be resolved from the authenticated application user / surface account using the existing account lookup flow.

The source/surface value can remain metadata for informational purposes, but it is not a routing authority.

---

# 34. External identity uniqueness

Do not change the existing account schema in this refactor.

Preserve the intended invariant that external identity is globally unique for a surface.

Conceptually:

```text
(surface, external_user_id)
```

must identify one external identity.

Do not create `surface_accounts` migration now.

---

# 35. Import boundary test

Create a test that ensures the chat engine does not depend on the Telegram surface.

Conceptually:

```python
def test_chat_engine_has_no_telegram_dependency():
    ...
```

The exact implementation can inspect imports or modules.

The important invariant:

```text
chat_engine
    ❌ imports telegram surface
```

This test exists to prevent architectural regression.

---

# 36. Integration behavior

The following cases must work:

### Greeting

```text
Input:
Hii

Route:
CHAT

Behavior:
Short conversational response

Retrieval:
NO
```

### Identity

```text
Input:
Who are you? What is your name 😂

Route:
META

Behavior:
RecallAI identity response

Retrieval:
NO
```

### Recall

```text
Input:
What did I save about Redis?

Route:
RECALL

Behavior:
semantic retrieval → cards → LLM

Retrieval:
YES
```

### Command

```text
Input:
/recent

Route:
COMMAND

Behavior:
existing /recent command

LLM:
NO
```

### URL capture

```text
Input:
https://youtube.com/...

Route:
CAPTURE

Behavior:
existing ingestion pipeline

Chat answer LLM:
NO
```

### Normal question

```text
Input:
What is dependency injection?

Route:
CHAT

Behavior:
normal conversation

Memory retrieval:
NO
```

---

# 37. Required tests

At minimum implement:

```text
tests/ai/test_router.py
```

and tests for:

```text
cards.py
```

Test:

1. card contains expected metadata
2. summary is truncated
3. no more than 2 highlights
4. full content is not included
5. context obeys approximate token budget
6. context stops before budget is exceeded
7. empty items returns empty context

Test chat engine behavior:

```text
META → no retrieval
CHAT  → no retrieval
RECALL → retrieval
zero recall results → no expensive answer call
```

Test architecture boundary:

```text
chat_engine does not import Telegram surface
```

---

# 38. Validation commands

After implementation run:

```bash
uv run ruff check app tests
uv run mypy app
uv run pytest -q
```

Fix errors caused by this refactor.

Do not “fix” unrelated pre-existing failures unless they directly prevent this architecture from working.

---

# 39. Manual Telegram verification

After automated tests pass, manually verify at least:

```text
Hii
Who are you?
What can you do?
What did I save this week?
What did I save about Redis?
/recent
A real URL
A normal non-memory question
```

Confirm:

```text
Hii
→ no retrieval

Who are you?
→ META
→ no retrieval

What did I save this week?
→ RECALL
→ retrieval

URL
→ CAPTURE
→ existing processing pipeline

/recent
→ COMMAND
→ existing command behavior
```

Also confirm that responses do not incorrectly start with:

```text
- 
* 
#
```

when the response is a simple single line.

---

# 40. Implementation strategy

Before changing code:

1. Inspect the current repository.
2. Identify the existing `RecallChatService` behavior.
3. Identify current Telegram dispatch logic.
4. Identify existing semantic search.
5. Identify current chat history implementation.
6. Identify current prompt/chain structure.
7. Reuse existing abstractions wherever possible.

Then implement incrementally:

```text
Step 1
types.py

Step 2
router.py + tests

Step 3
cards.py + tests

Step 4
retrieval.py

Step 5
engine.py

Step 6
move Telegram parsing into adapter

Step 7
move Telegram rendering into adapter

Step 8
wire dispatch to ChatEngine

Step 9
history cap

Step 10
output cap

Step 11
token logging

Step 12
full test suite
```

---

# 41. Critical instructions to Claude Code

Do NOT:

* redesign unrelated architecture
* introduce new frameworks
* introduce new database tables
* introduce migrations
* build WhatsApp
* build another surface
* implement hybrid search
* implement RRF
* implement reranking
* implement chunking
* implement query rewriting
* implement merged enrichment
* change authentication
* change extractors
* change the worker pipeline
* change existing provider contracts unless absolutely required
* move write-side RAG processing into the chat engine

When uncertain, prefer the smallest change that satisfies this specification.

Do not create speculative abstractions.

Do not duplicate existing functions.

Do not change behavior that is unrelated to the routing/RAG/surface boundary.

---

# 42. Final target

The resulting system should have this shape:

```text
                         TELEGRAM
                             │
                             ▼
                       Telegram Parser
                             │
                             ▼
                     InboundMessage
                             │
                             ▼
                     ┌──────────────┐
                     │ Chat Engine  │
                     └──────┬───────┘
                            route()
                             │
       ┌─────────┬───────────┼───────────┬──────────┐
       ▼         ▼           ▼           ▼          ▼
   COMMAND    CAPTURE       META       RECALL      CHAT
       │         │           │           │          │
       ▼         ▼           ▼           ▼          ▼
   Existing   Existing   Identity     RAG flow     LLM
   command    capture       │            │
   handling   pipeline      │            ▼
                            │       MemoryRetriever
                            │            │
                            │            ▼
                            │       Semantic Search
                            │            │
                            │            ▼
                            │        VaultItems
                            │            │
                            │            ▼
                            │       Memory Cards
                            │            │
                            │            ▼
                            │       Context Budget
                            │            │
                            └──────┬─────┘
                                   ▼
                                  LLM
                                   │
                                   ▼
                            OutboundReply
                                   │
                                   ▼
                           Telegram Renderer
                                   │
                                   ▼
                                Telegram
```

The final principle is:

> **Router decides whether memory is needed. Retriever finds the relevant memories. Cards compress them. The LLM only receives the minimum context required to answer. Telegram only transports and renders the result.**

Implement this architecture with the smallest safe refactor possible.
