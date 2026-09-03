"""The agent harness: the model decides what to do, this package decides what it may do.

Named `harness/` rather than `agent/` for one dull reason -- `app/ai/chat/agent.py` is
the driver the older tool lane still runs on, and a package cannot sit beside a module of
the same name. That file is the fallback underneath this one until the flip, so it stays.
"""
