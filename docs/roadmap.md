# Roadmap

Deliberately deferred, with the reason. Nothing here is a TODO someone should pick up
casually -- each line is a decision that was made once and can be re-made with evidence.

## Deferred from the chat-engine / RAG refactor

**Relocating the rest of the Telegram surface.** The architecture doc lays out
`app/services/surfaces/telegram/{client,limits}.py`, but `client.py`, `limits.py`,
`formatting.py`, `capture.py`, `linking.py` and `dispatch.py` still live in
`app/services/telegram/`. Moving only two of them would split one package across two
paths, which is worse than either arrangement, and moving all six is a mechanical rename
touching every import for no architectural gain -- the invariant that matters (the engine
never imports the surface) is enforced by `tests/chat_engine/test_boundaries.py`
regardless of where the surface lives. Worth doing on the day a second surface exists and
the shared/specific split stops being obvious.

**`Attachment` carries a `file_id`, not bytes.** The doc's inbound type carries
`data: bytes`, which would mean `parse.py` downloads. `telegram/capture.py` already
downloads: it enforces the size cap, sniffs the real type from the magic bytes and streams
to B2. Duplicating that in the parser, or replacing it, is out of scope by the same doc's
own rules. A surface whose files must be fetched before routing can add an optional
`data` field then.

**Detail intent is a phrase list, not a classifier.** `router.wants_detail` matches about
nine literal phrases. It will miss paraphrases. The alternative is a model call to decide
whether to make a model call, which has no cost ceiling -- revisit only with logged
evidence of how often it misses, which `model_call` events now make measurable.

**The `<memory>` fence is neutralised, not escaped.** `chain._neutralize_fence` breaks the
tag rather than encoding it, because the block format is read by a model rather than
parsed. A stricter format (base64, or a random per-request delimiter) is the real fix if
injection ever shows up in practice.

## Known rough edges this refactor did not touch

* `VaultItem.deleted_at` exists and reads filter on it, but `VaultRepository.delete()`
  hard-deletes.
* `GET /search` is still `ILIKE`; only the chat path uses `search_semantic`.
* `enqueue_process_item` fires before the request session commits on the web path.
