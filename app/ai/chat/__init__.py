"""LangChain-backed chat and retrieval.

Isolated behind `app.services.recall_chat` on purpose: routers, Celery tasks and the
Telegram dispatcher never import LangChain, the same way business code never imports
`GeminiProvider` directly.

Note what does **not** live here. Embeddings still come from `AIProvider`, because the
stored vectors were written by it -- Gemini's 768 dims are zero-padded to 1536 and are
not comparable with OpenAI's native 1536, so a second embedding stack would silently
rank against a different space.
"""
