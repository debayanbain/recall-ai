# RecallAI — Chat Engine & Chatbot Architecture

This document provides a comprehensive, under-the-hood explanation of how RecallAI's chatbot and RAG (Retrieval-Augmented Generation) system functions.

---

## 1. Core Architectural Philosophy

RecallAI’s chatbot is designed around a strict set of engineering and security constraints:

> **Decide intent first with zero tokens, retrieve only when necessary, send the smallest useful context (cards vs. detail), and mechanically validate everything before and after the LLM.**

```
                      [ User Input ]
                            │
            ┌───────────────┴───────────────┐
            ▼                               ▼
    [ Telegram Webhook ]            [ Web API: /chat/ask ]
    (app/services/telegram)         (app/api/v1/chat.py)
            │                               │
            ▼                               ▼
     parse_message()                   AskRequest
            │                               │
            └───────────────┬───────────────┘
                            ▼
              Normalized InboundMessage
              (surface, user_id, text, etc.)
                            │
                            ▼
           ┌─────────────────────────────────┐
           │   Deterministic Intent Router   │  (app/services/chat_engine/router.py)
           │    (Regex & Structural Rules)   │  No LLM Tokens Used
           └────────────────┬────────────────┘
                            │
   ┌───────────────┬────────┴───────┬───────────────┬──────────────┐
   ▼               ▼                ▼               ▼              ▼
[CAPTURE]       [STATUS]        [COMMAND]        [META/CHAT]    [RECALL]
Save Link/    Read Postgres    /note, /recent,   Scope Check    Search & RAG
Media to      Row Directly      /status, /help    & General      Pipeline
Queue         (0 LLM calls)     Handlers          Chat           (pgvector + LLM)
                                                    │              │
                                                    └──────┬───────┘
                                                           ▼
                                            ┌──────────────────────────────┐
                                            │ Output & Stream Validation   │
                                            │ (Strip fake URLs / fake IDs) │
                                            └──────────────┬───────────────┘
                                                           │
                                            ┌──────────────┴───────────────┐
                                            ▼                              ▼
                                     [Telegram HTML]                [Web SSE Stream]
                                     (render.py)                    (Delta, Status, End)
```

The core Chat Engine (`app/services/chat_engine`) is completely **platform-independent**. Telegram and Web HTTP are purely input/output adapters ("surfaces").

---

## 2. Inbound Ingestion & Surface Normalization

Every inbound interaction from any interface is parsed into a normalized data structure: `InboundMessage` (`app/services/chat_engine/types.py`).

### A. Telegram Surface
* **Dispatcher:** `app/services/telegram/dispatch.py` (`TelegramDispatcher.handle`)
* **Privacy & Isolation:**
  * Enforces `is_private` check. Any group chat update is dropped immediately to prevent leaking personal vault data to other group members.
  * Resolves Telegram sender ID to a RecallAI user via the `telegram_accounts` database table. If unlinked, returns a connect button.
* **Typing Indicator:** Spawns a background task executing `sendChatAction("typing")` every 4.5 seconds so the user sees immediate feedback while retrieval/generation runs.
* **Parsing:** `app/services/surfaces/telegram/parse.py` converts raw Telegram update JSON into `InboundMessage`, identifying entities (links, attachments, text).

### B. Web HTTP / SSE Surface
* **Endpoint:** `POST /api/v1/chat/ask` (`app/api/v1/chat.py`)
* **Security & Auth:** Enforces Same-Site CSRF validation (`assert_same_site`) and extracts `user_id` strictly from the authenticated session cookie.
* **Rate Limiting:** Sliding-window rate limiter per user (`rate_limit.consume("ask", user_id, ASK_PER_HOUR)`).
* **Streaming Protocol:** Streams Server-Sent Events (`status`, `delta`, `items`, `end`) over `StreamingResponse`.

---

## 3. Zero-Token Deterministic Intent Routing

Before invoking embedding models or LLMs, the router (`app/services/chat_engine/router.py`) evaluates message structure in strict priority order using deterministic regex and presence checks:

| Priority | Condition / Pattern | Intent | Handled By | Action Description |
| :--- | :--- | :--- | :--- | :--- |
| **1** | Has photo, document, or audio attachment | `CAPTURE` | `TelegramCaptureService` | Ingests file/media into vault queue. |
| **2** | Empty / whitespace-only string | `CHAT` | `ChatEngine` | Safe fallback without triggering saves. |
| **3** | Starts with `/` (e.g. `/note`, `/recent`) | `COMMAND` | Surface Dispatcher | Dispatches to command handler. |
| **4** | Contains an `http(s)://` link | `CAPTURE` | `TelegramCaptureService` | Saves URL to vault (beats conversational text). |
| **5** | Questions about assistant ("who are you", "help") | `META` | `ChatEngine` | Answers about product capabilities (0 retrieval). |
| **6** | Status questions ("is it saved?", "did it work?") | `STATUS` | `status.py` | Reads Postgres directly (0 LLM tokens). |
| **7** | Retrieval phrasing ("did I save", "find", "search") | `RECALL` | `RecallChatService` | Enters RAG & semantic search pipeline. |
| **8** | Fallback / conversational text | `CHAT` | `ChatEngine` | Enters scoped conversational chat. |

---

## 4. Detailed Execution Lanes

### 1. The `STATUS` Lane — Direct Database State
* **File:** `app/services/chat_engine/status.py`
* **Mechanism:** When a user asks *"did that save?"* or *"what is the status of my last upload?"*, asking a generative LLM is non-deterministic. Instead, the status lane queries the user's latest `VaultItem` record directly from PostgreSQL.
* **Cost:** **0 LLM calls, 0 embeddings**.
* **Output:** Returns deterministic, real-time status (e.g. "Saved: Python Concurrency Guide — Ready", or "Still processing video audio...").

### 2. The `CAPTURE` Lane — Ingestion Pipeline
* **File:** `app/services/telegram/capture.py`
* **Mechanism:** Plain links, files, media, and `/note <text>` commands create a new `VaultItem` with status `pending`.
* **Asynchronous Queue:** Dispatches background jobs to Celery/Redis for scraping, transcription, summary enrichment, and vector embedding creation.

### 3. The `CHAT` / `META` Lane — Guarded Small Talk
* **Files:** `app/services/chat_engine/scope.py`, `app/ai/chat/chain.py`
* **Scope Gate:** Uses pattern checking to block general-purpose queries (e.g., "write a Python script", "what is the capital of France").
  * *Rationale:* Prevents prompt injection, stops token budget exhaustion, and ensures user expectations remain anchored on vault capabilities.
* **Execution:** Conversational chat chain answers from short-term memory with **zero access to vault data**.

---

## 5. The `RECALL` Lane — Advanced RAG Pipeline

When a user asks a question about their saved memories, the engine follows an optimized, safe RAG process (`app/services/recall_chat.py`):

```
                                    User Question
                                          │
                        ┌─────────────────┴─────────────────┐
                        ▼                                   ▼
             [ Multi-Turn Tool Loop ]              [ Single-Shot Planner ]
             (settings.RECALL_TOOLS_ENABLED)       (Fallback / Classic)
                        │                                   │
              Model calls tools:                    LLM extracts filters:
              - SearchMemories                      - search_text
              - ListMemories                        - date / time range
              - GetMemory                           - content_types / category
                        │                                   │
                        └─────────────────┬─────────────────┘
                                          ▼
                             [ pgvector Similarity Search ]
                             (ORDER BY embedding <=> query_vec)
                                          │
                                          ▼
                            [ Evidence Assessment Gate ]
                            (Floor & Score Margin Filter)
                                          │
                        ┌─────────────────┴─────────────────┐
                        ▼                                   ▼
                [ No Evidence ]                    [ Supported Evidence ]
                Fixed sentence                     Build Compact Cards
                (0 LLM tokens)                     (or Detail Card if verbatim requested)
                                                            │
                                                            ▼
                                                   [ Redis Chat History ]
                                                   (Last 4 turns loaded)
                                                            │
                                                            ▼
                                                   [ LLM Answer Generation ]
                                                   (Strict Anti-Injection Prompt)
                                                            │
                                                            ▼
                                                   [ Output Validator ]
                                                   (Scrub fake URLs / IDs / length)
```

### Step 5.1: Query Formulation & Search
RecallAI supports two search modes:

1. **Multi-Turn Tool-Calling Agent (`app/ai/chat/tools.py`):**
   * The model has access to 3 structured, read-only tools:
     * `SearchMemories(query, days, content_types, category)`: Semantic similarity search.
     * `ListMemories(days, content_types, category)`: Time-based newest-first listing.
     * `GetMemory(memory_id)`: Fetches full content for a memory already surfaced in the current conversation.
   * Hard limits: `max_calls=4`, `max_rounds=3`.
   * **Security:** Every tool execution is hard-bound to the session's authenticated `user_id` inside `MemoryToolbox` (`app/services/chat_engine/toolbox.py`). The LLM has no mechanism to query another user's vault.

2. **Single-Shot Planned Search (`app/ai/chat/planner.py`):**
   * Uses structured output to extract a `MemoryQuery` with semantic query text, time offsets, and content filters.
   * Performs cosine distance search (`<=>`) in PostgreSQL with pgvector.

### Step 5.2: Evidence Assessment Gate (`app/services/chat_engine/evidence.py`)
Vector databases always return top-$k$ nearest neighbors, regardless of relevance. Handing weak matches to an LLM forces it to hallucinate connections.

The Evidence Gate evaluates results before generating answers:
* **Relevance Floor (`RECALL_MIN_SCORE`):** Discards memories below the absolute similarity threshold.
* **Score Margin (`RECALL_SCORE_MARGIN`):** Discards memories significantly weaker than the best match to avoid diluting context.
* **Tri-State Output:**
  1. `no_evidence`: Returns a fixed *"I couldn't find anything about that in your vault"* with **0 LLM generation tokens**.
  2. `insufficient`: Weak matches detected; prompt is instructed to explicitly acknowledge uncertainty.
  3. `supported`: High-confidence evidence matches.

### Step 5.3: Token Minimization (Cards vs. Detail) (`app/services/chat_engine/cards.py`)
* **Compact Memory Cards (Default):** By default, only metadata and summaries are passed to the LLM prompt:
  ```xml
  <memory id="a3f1c920" type="youtube" title="FastAPI Async Deep Dive" url="https://youtube.com/watch?v=...">
  Summary: Guide to async database sessions and background task processing in FastAPI...
  Tags: python, fastapi, backend
  </memory>
  ```
* **Detail Cards (`wants_detail()`):** Full text / transcripts are only loaded when the user explicitly requests verbatim quotes or deep details ("*what did that document say in detail?*").

### Step 5.4: Sliding-Window Redis History (`app/ai/chat/history.py`)
* Stored in Redis under key `tg:chat:{user_id}:{external_chat_id}` with expiration TTL.
* Trims history to 6 turns on write; supplies only the last 4 turns to the prompt to maintain pronouns and follow-up context without prompt bloat.

---

## 6. Output & Stream Validation (Post-LLM Safety)

Prompts are treated as requests, not guarantees. `validate_answer` (`app/services/chat_engine/validation.py`) enforces strict mechanical safety on all generated text:

1. **Memory ID Verification:** Any citation like `[a3f1c920]` that does not match a memory present in the supplied evidence is stripped.
2. **URL Whitelisting:** Any URL in the model output that was not present in the retrieved memory headers is replaced with `[link omitted]`.
3. **Length Caps:** Automatically trims answers that exceed configured character boundaries at clean sentence/word boundaries.
4. **Real-Time Streaming Validator (`StreamValidator`):**
   * In Web SSE mode, buffers incomplete word fragments across chunks.
   * Verifies URLs and IDs whole at whitespace boundaries before emitting frames to the client.

---

## 7. Surface Rendering & Delivery

* **Telegram Surface (`app/services/surfaces/telegram/render.py`):**
  * Converts structured blocks (`TextBlock`, `ItemListBlock`, `ErrorBlock`) into safe Telegram HTML (`<b>`, `<i>`, `<a>`, `<code>`).
  * Escapes raw user text to prevent HTML injection in the Telegram client.
* **Web SSE Surface (`app/api/v1/chat.py`):**
  * Emits typed JSON Server-Sent Events:
    * `event: status` — e.g. `{"stage": "searching your memories"}`
    * `event: delta` — e.g. `{"text": "You saved..."}`
    * `event: items` — structured JSON representation of memory cards
    * `event: end` — completion payload with cited memory IDs and validation metadata

---

## 8. Summary of Defensive Controls

| Threat / Vulnerability | Defense Mechanism | Primary Code Location |
| :--- | :--- | :--- |
| **Indirect Prompt Injection** | Memory text is enclosed in `<memory>` XML tags; system prompt instructs model to treat blocks strictly as untrusted data; tools are strictly read-only. | `app/ai/chat/tools.py`<br>`app/ai/prompts.py` |
| **Cross-Tenant Data Exposure** | `user_id` is supplied exclusively server-side from session or verified account links; tool parameters never accept tenant IDs. | `app/services/chat_engine/toolbox.py` |
| **Hallucinated URLs & Sources** | Post-generation regex validator scrubs all links not present in retrieved context. | `app/services/chat_engine/validation.py` |
| **Hallucinations on Empty Search** | Evidence Assessment Gate evaluates vector scores and halts generation if similarity is below threshold. | `app/services/chat_engine/evidence.py` |
| **Infinite Agent Execution Loops** | Strict execution caps on tool loop rounds and calls (`max_rounds=3`, `max_calls=4`). | `app/ai/chat/tools.py` |
| **Token Cost Blowup** | Compact memory cards by default; chat history bounded at 4 turns; zero-token routing for status and capture. | `app/services/chat_engine/cards.py`<br>`app/services/chat_engine/router.py` |
