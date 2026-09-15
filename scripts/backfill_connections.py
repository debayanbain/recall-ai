"""Draw connections across memories saved before the feature existed, once.

The derivation runs at the end of a capture and only ever looks outward from the *new*
memory. That is what makes it O(1) per capture instead of O(n^2) over a vault, and it is
honest -- but it also means a vault that predates the feature has no edges at all and will
never grow any between the things already in it. This is that work, as a script you can
run twice.

    uv run python scripts/backfill_connections.py                 # report only
    uv run python scripts/backfill_connections.py --apply         # write the suggestions
    uv run python scripts/backfill_connections.py --min-score 0.7 # try a different floor

**The dry run spends no money, and `--apply` spends none by default.** Every vector it
compares was written by the pipeline at capture time; this reads them back and does
arithmetic. That is worth stating because it makes the dry run the one honest way to check
`CONNECTION_MIN_SCORE` against *your own vault* without paying for a fresh set of
embeddings -- `scripts/measure_connection_floor.py` measures a handful of pairs you write
by hand and does spend money; this measures every pair the feature would actually propose
and does not.

**`--judge` is the exception and it is opt-in for that reason.** A live capture asks a
model which of its recalled neighbours are really connected; over a whole vault that is
one call per memory, which is a bill nobody agreed to by typing `--apply`. So the script
forces the judge OFF unless asked, which also keeps `--apply` writing exactly what the
dry run just reported -- a report produced by arithmetic and an apply decided by a model
would be two different answers presented as one.

So the intended order is: run this with no flags, read the distribution, decide whether
`CONNECTION_MIN_SCORE` sits in the right place, change it if not, then run with `--apply`.

Two things it deliberately does not do:

* **It never confirms anything.** Everything written is `suggested`, exactly as a live
  capture would write it, and a person still taps each one. A backfill that filed edges
  directly would be the auto-confirm this feature was designed not to have, applied to the
  whole vault at once.
* **It never re-proposes a dismissed pair.** `suggest_many` conflicts on the unordered
  pair, and a declined row *is* the conflicting row -- so re-running this is a no-op over
  everything already decided, which is what makes it safe to run twice.

Lives in `scripts/` rather than as a beat task on purpose: it is a one-off for rows
captured before the feature existed, and a sweep that runs forever over a vault that only
ever grows is a cost with no ceiling.
"""
from __future__ import annotations

import argparse
import asyncio
import uuid
from collections import Counter
from typing import Any

from sqlmodel import col, select, text

from app.core.config import settings
from app.db.session import task_session
from app.models.base import ProcessingStatus
from app.models.vault import VaultChunk, VaultItem
from app.repositories.connection import ConnectionRepository
from app.repositories.vault import VaultRepository
from app.services.connection_derivation import ConnectionDeriver

#: Commit every this many items. A vault is walked one statement at a time against a
#: database in another region, so a single transaction over thousands of rows is a long
#: lock and a lot to lose to one dropped connection. Re-running resumes for free, because
#: every write conflicts on a pair that already exists.
_BATCH = 25

#: How many neighbours the dry run *looks* at, regardless of the configured candidate cap.
#: The report is about the shape of the distribution, and a report that only ever sees the
#: nearest five cannot show where the floor should sit.
_REPORT_WIDTH = 10


async def _items(
    session: object, user_id: uuid.UUID | None, limit: int | None
) -> list[VaultItem]:
    """Completed memories that actually have an embedding, oldest first.

    The join is what keeps `skipped` uploads and half-processed rows out: an item with no
    chunk has nothing to compare, and walking it would be one round trip to learn that.
    """
    query = (
        select(VaultItem)
        .join(VaultChunk, col(VaultChunk.vault_item_id) == col(VaultItem.id))
        .where(
            col(VaultItem.deleted_at).is_(None),
            VaultItem.processing_status == ProcessingStatus.completed,
            VaultChunk.chunk_index == 0,
            col(VaultChunk.embedding).is_not(None),
        )
        .order_by(col(VaultItem.created_at))
    )
    if user_id is not None:
        query = query.where(VaultItem.user_id == user_id)
    if limit:
        query = query.limit(limit)
    rows = await session.exec(query)  # type: ignore[attr-defined]
    return list(rows.all())


def _bucket(score: float) -> str:
    return f"{int(score * 10) / 10:.1f}"


async def _table_missing(session: Any) -> bool:
    """Whether `memory_connections` has been migrated into this database yet.

    Checked up front because of an asymmetry that wasted somebody's time once: the dry run
    reads only chunks and vault items, so it runs perfectly happily against a database
    with no connections table -- and then `--apply` fails on the first write with a
    hundred lines of asyncpg traceback. A dry run that succeeds reads as "ready".
    """
    # **Take the column, not the row.** A `text()` result yields a `Row`, and
    # `bool(Row)` is True for any non-empty row -- so `bool(result.one())` reports
    # "missing" whether or not the table is there. Only one branch of that looks correct,
    # which is exactly how it shipped: verified against a database with no table and
    # never against one with it.
    result = await session.exec(text("SELECT to_regclass('memory_connections') IS NULL"))
    return bool(result.one()[0])


async def run(args: argparse.Namespace) -> int:
    floor = args.min_score if args.min_score is not None else settings.CONNECTION_MIN_SCORE
    user_id = uuid.UUID(args.user) if args.user else None

    async with task_session() as session:
        if await _table_missing(session):
            print(
                "memory_connections does not exist in this database yet.\n"
                "  Run:  uv run alembic upgrade head\n"
            )
            if args.apply:
                # Refused rather than attempted: the failure is certain, and one clear
                # line beats the traceback it would otherwise produce on the first write.
                return 1
            print("Continuing anyway -- the report below reads only embeddings, so the")
            print("distribution is still accurate. Nothing can be written until you migrate.\n")

        deriver = ConnectionDeriver(
            ConnectionRepository(session), VaultRepository(session)
        )
        items = await _items(session, user_id, args.limit)
        print(f"{len(items)} memory(ies) with an embedding.")
        print(f"floor: {floor}  (configured: {settings.CONNECTION_MIN_SCORE})")
        if not items:
            return 0

        histogram: Counter[str] = Counter()
        # Keyed by the unordered pair, because that is what the database keys on: A finds
        # B and B finds A, so every real edge is seen twice and counting sightings would
        # report twice the suggestions that could actually land. That number is the one
        # somebody sets a threshold from, so it has to mean what it says.
        pairs: dict[frozenset[uuid.UUID], tuple[float, str, str]] = {}
        written = 0

        for index, item in enumerate(items, start=1):
            if args.apply:
                written += await deriver.derive(item.id)
                if index % _BATCH == 0:
                    await session.commit()
                    print(f"  ... {index}/{len(items)} scanned, {written} written")
                continue

            for candidate in await deriver.candidates(
                item.id, limit=_REPORT_WIDTH, item=item
            ):
                histogram[_bucket(candidate.score)] += 1
                if candidate.score < floor:
                    continue
                pairs.setdefault(
                    frozenset({item.id, candidate.item.id}),
                    (
                        candidate.score,
                        item.title or str(item.id)[:8],
                        candidate.item.title or str(candidate.item.id)[:8],
                    ),
                )

        if args.apply:
            await session.commit()
            print(f"\nWrote {written} suggestion(s). Nothing is confirmed -- each one "
                  "still needs a tap.")
            return 0

        print("\nscore distribution (every sighting; each pair is seen twice):")
        for bucket in sorted(histogram, reverse=True):
            bar = "#" * min(60, histogram[bucket])
            print(f"  {bucket}  {histogram[bucket]:5d}  {bar}")

        would_write = len(pairs)
        print(f"\nAt a floor of {floor}, this would write {would_write} suggestion(s).")
        if would_write:
            # The number that decides whether the floor is usable. A backfill is the one
            # moment a whole vault's worth of suggestions lands in one inbox, and an inbox
            # nobody can work through is the same as no inbox at all.
            per_item = would_write / len(items)
            print(f"That is {per_item:.1f} per memory, all in one suggestions inbox.")
            if per_item > 3:
                print(
                    "  ^ that is a lot to ask someone to tap through. A higher floor "
                    "proposes fewer and stronger pairs -- try --min-score."
                )

        ranked = sorted(pairs.values(), reverse=True)
        print("\nstrongest pairs (eyeball these: are they really related?)")
        for score, left, right in ranked[: args.show]:
            print(f"  {score:.3f}  {left[:44]!r}  <->  {right[:44]!r}")

        weakest = ranked[-args.show :][::-1]
        if weakest:
            print("\nweakest pairs that still clear the floor (these decide the floor)")
            for score, left, right in weakest:
                print(f"  {score:.3f}  {left[:44]!r}  <->  {right[:44]!r}")

        print("\nNothing was written. Re-run with --apply once the floor looks right.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="write the suggestions (default: dry run)"
    )
    parser.add_argument("--user", help="restrict to one user id")
    parser.add_argument("--limit", type=int, help="stop after N memories")
    parser.add_argument(
        "--min-score",
        type=float,
        help="try a floor other than the configured CONNECTION_MIN_SCORE (dry run only)",
    )
    parser.add_argument(
        "--show", type=int, default=15, help="how many example pairs to print"
    )
    parser.add_argument(
        "--judge",
        action="store_true",
        help=(
            "let the model decide and label each edge, as a live capture does. "
            "One model call per memory -- see the module docstring"
        ),
    )
    args = parser.parse_args()
    if not args.judge:
        # Mutating the cached settings singleton, which is what `tests/conftest.py` does
        # to the same flag and for the same reason: there is one switch, and a second way
        # to express "off" would be a second thing to keep in step.
        settings.CONNECTION_JUDGE_ENABLED = False
    elif args.apply:
        print(
            "--judge: this will make one model call per memory. "
            "Ctrl-C now if that was not the intention.\n"
        )
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
