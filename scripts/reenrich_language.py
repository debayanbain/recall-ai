"""Re-run enrichment on cards written in the wrong language.

`ENRICHMENT_LANGUAGE` decides what language a summary, its tags and its label come back
in, but it only applies to items enriched *after* it was set. Everything already in the
vault keeps the language it was written in, and nothing re-drives it: `reprocess` refuses
a `completed` item on purpose, because re-running the whole pipeline to reproduce a good
result is exactly the thing worth refusing. So changing the setting is not a backfill.
This is the backfill.

    make reenrich                       # dry run: what would change, and what it costs
    make reenrich-apply                 # actually write
    uv run python scripts/reenrich_language.py --user <uuid> --limit 50 --apply

What it does per item: re-reads the text already stored on the row and asks for the four
card fields again. What it deliberately does NOT do:

* **It never re-fetches `source_url`.** That is `reprocess`'s job and a different risk
  surface. A page that has changed since the save would rewrite `content` under a user
  who never asked for it, and a dead link would turn a good memory into a failure.
* **It never touches `content`, `ai_highlights` or `processing_status`.** The body is
  unchanged, so quotes taken from it are still verbatim and still land where the model
  pointed. An item that is `completed` stays `completed` for the whole run -- there is no
  window in which the UI shows a spinner over a memory that is fine.
* **It writes nothing without `--apply`.** A backfill that spends money per item and
  overwrites four columns is not something to discover you have started.

The embedding IS recomputed by default (`--no-embed` opts out), and that is the one
judgment call here. `embed_input` is title + summary + body, so a summary rewritten from
Bengali into English changes the vector the item is ranked by -- leaving the old one
means the card reads in English and still only matches Bengali queries, which is the
half-migration nobody would be able to see.

Selection is by SCRIPT, which is an observable fact and not language identification. It
catches the case this exists for -- a Bengali card in an English vault -- and it cannot
catch a Spanish card in an English one, because both are Latin. `--all` is the honest
answer to that: re-enrich everything in scope and pay for it.
"""
from __future__ import annotations

import argparse
import asyncio
import uuid
from dataclasses import dataclass

from sqlmodel import col, select

from app.ai import enrichment
from app.core.config import settings
from app.core.languages import language_name
from app.core.scripts import contradicts_script, script_of
from app.db.session import task_session
from app.models.base import ProcessingStatus
from app.models.vault import VaultItem
from app.repositories.vault import VaultRepository
from app.services.processing_service import ProcessingService

#: How many rows are read at a time. The database is a region away (~290ms a statement),
#: so the listing is one statement per batch rather than one per item -- but the whole
#: vault in a single result set is a script that fails on the vaults that need it most.
_BATCH = 200


@dataclass
class Plan:
    """What a single row would become. Printed in a dry run, applied otherwise."""

    item: VaultItem
    script: str | None


def _card_text(item: VaultItem) -> str:
    """The model-written text whose language this decides on.

    Summary, label and tags together rather than the summary alone: a card can easily
    have an English summary and Bengali tags, and re-doing it on the strength of any one
    of the three is right -- they are produced by one call and read as one card.
    """
    return " ".join(
        part
        for part in (
            item.summary or "",
            item.ai_label or "",
            " ".join(item.ai_tags or []),
        )
        if part
    )


def _needs_rewrite(
    item: VaultItem, target: str, everything: bool
) -> tuple[bool, str | None]:
    """Whether this card's language disagrees with the configured one.

    `contradicts_script` is the same test the transcription path uses to notice a model
    reporting a language the characters rule out, and it reads correctly here: the
    configured language is what we claim the card should be, the script is what it
    actually is. It is narrow on purpose -- a Hindi target against Devanagari characters
    agrees, and only a real contradiction counts.
    """
    script = script_of(_card_text(item))
    if everything:
        return True, script
    return contradicts_script(target, script), script


async def _plans(
    repo: VaultRepository,
    *,
    target: str,
    user_id: uuid.UUID | None,
    limit: int | None,
    everything: bool,
) -> list[Plan]:
    """Every completed item whose card is in the wrong language, oldest first.

    The script test runs in Python rather than in SQL: Postgres has no notion of "which
    script is this", and a range regex in a query is the same table scan with the rule
    written somewhere nobody would think to update it.
    """
    plans: list[Plan] = []
    offset = 0
    while True:
        statement = (
            select(VaultItem)
            .where(
                col(VaultItem.deleted_at).is_(None),
                VaultItem.processing_status == ProcessingStatus.completed,
                col(VaultItem.summary).is_not(None),
            )
            .order_by(col(VaultItem.created_at))
            .offset(offset)
            .limit(_BATCH)
        )
        if user_id is not None:
            statement = statement.where(VaultItem.user_id == user_id)

        rows = list((await repo.session.exec(statement)).all())
        if not rows:
            return plans

        for item in rows:
            wanted, script = _needs_rewrite(item, target, everything)
            if not wanted:
                continue
            if not (item.content or item.title or "").strip():
                # Nothing to read it back from. An item enriched from a title alone that
                # has since lost it is not a card this can rebuild, and inventing one
                # from the URL is how a vault fills with confident nonsense.
                continue
            plans.append(Plan(item=item, script=script))
            if limit is not None and len(plans) >= limit:
                return plans

        offset += _BATCH


def _describe(plan: Plan) -> str:
    """One line per item. Ids and text the user already owns -- no emails, no URLs."""
    label = plan.item.ai_label or plan.item.title or "(no label)"
    tags = ", ".join(plan.item.ai_tags or []) or "(no tags)"
    return (
        f"  {str(plan.item.id)[:8]}  [{plan.script or 'latin'}]  "
        f"{label[:60]}  |  {tags[:60]}"
    )


async def _rewrite(service: ProcessingService, item: VaultItem, *, embed: bool) -> None:
    """Re-ask for the four card fields, and optionally re-rank the item by the new one.

    `_card_fields` rather than a fresh call to `enrichment.enrich`: it is the combined
    call *and* the four-call fallback behind it, which is the behaviour the pipeline has.
    A second copy of that choice here is one that stops matching the first time either
    path changes.
    """
    text = item.content or item.title or ""
    fields = await service._card_fields(text)  # noqa: SLF001 - see the docstring

    item.summary = fields.summary
    item.ai_tags = fields.tags
    item.ai_category = fields.category
    item.ai_label = fields.label or None

    if embed:
        embed_input = f"{item.title or ''}\n{item.summary or ''}\n{text}"
        vector = await service.ai.generate_embedding(embed_input)
        await service.repo.upsert_chunk(
            item_id=item.id,
            user_id=item.user_id,
            vector=vector,
            content=embed_input[:8000],
        )

    await service.repo.add(item)


async def run(args: argparse.Namespace) -> int:
    configured = settings.ENRICHMENT_LANGUAGE
    if configured == "content" and not args.all:
        # There is nothing to converge on: every card already matches its own content by
        # definition, so a script test would select nothing and a run would be a no-op
        # that looked like "there was nothing to fix".
        print(
            'ENRICHMENT_LANGUAGE is "content", so no card can be in the wrong language.\n'
            "Set it to a language first, or pass --all to re-enrich regardless."
        )
        return 2

    target = language_name(configured) or "english"
    user_id = uuid.UUID(args.user) if args.user else None

    async with task_session() as session:
        repo = VaultRepository(session)
        service = ProcessingService(repo)

        plans = await _plans(
            repo, target=target, user_id=user_id, limit=args.limit, everything=args.all
        )
        scope = "every completed item" if args.all else f"cards not in {target.title()}"
        print(f"{len(plans)} item(s) selected -- {scope}")
        if not plans:
            return 0

        for plan in plans:
            print(_describe(plan))

        if not args.apply:
            calls = "1 combined call" if enrichment.enrichment_available() else "4 calls"
            embed = "" if args.no_embed else " + 1 embedding"
            print(
                f"\nDRY RUN -- nothing written. Applying costs ~{calls}{embed} per item."
                "\nRe-run with --apply to write."
            )
            return 0

        done = 0
        failed = 0
        for plan in plans:
            try:
                await _rewrite(service, plan.item, embed=not args.no_embed)
                # Committed per item, not per run: a provider timeout at item 300 must
                # not throw away the 299 that are already paid for.
                await session.commit()
                done += 1
            except Exception as exc:  # noqa: BLE001 - one bad item must not end the run
                await session.rollback()
                failed += 1
                # The type only. A provider's message can name the account it rejected,
                # and this prints to a terminal that gets pasted into an issue.
                print(f"  FAILED {str(plan.item.id)[:8]}  {type(exc).__name__}")
            if done and done % 25 == 0:
                print(f"  ... {done}/{len(plans)}")

        print(f"\nrewritten {done}, failed {failed}")
        return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    parser.add_argument("--user", help="restrict to one user id")
    parser.add_argument("--limit", type=int, help="stop after N items")
    parser.add_argument(
        "--all",
        action="store_true",
        help="re-enrich every completed item, not only ones in another script",
    )
    parser.add_argument(
        "--no-embed",
        action="store_true",
        help="leave the embedding alone (the card changes, its search ranking does not)",
    )
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
