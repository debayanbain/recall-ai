"""Put a finished memory back through the pipeline.

`VaultService.reprocess` deliberately refuses a `completed` item: re-running a good one
spends the whole pipeline to reproduce a result that is already there, and offering that
as a button invites it on every healthy memory. The exception it does allow -- a `skipped`
image that becomes readable the moment a vision key exists -- names the real case this
script exists for: **the pipeline gained a capability the item predates.**

A carousel saved before `INSTAGRAM_SLIDE_MAX` existed is exactly that. The row is
`completed` and honest about what it knew: a caption saying "in this carousel I've shared
80+ trusted job platforms", and no slides, because nothing kept them. Re-running is the
only way those pictures reach the vault, and there is no UI path to ask for it.

So this is a script rather than a route, for the same reason `backfill_connections.py` is:
it costs money per item (here an actor run, N image copies and up to
`INSTAGRAM_SLIDE_VISION_MAX` vision calls), and a cost with no ceiling does not belong
behind a button somebody can hold down.

    uv run python scripts/rerun_item.py <item-id> [<item-id> ...]
    uv run python scripts/rerun_item.py --type instagram --missing-slides --apply

Without `--apply` it prints what it would re-drive and writes nothing.
"""
from sqlalchemy.dialects.postgresql import Any
from typing import Any

import argparse
import asyncio
import sys
import uuid

from sqlalchemy import text

from app.db.session import task_session
from app.models.base import ProcessingStatus
from app.queue.client import enqueue_process_item


async def _rows(ids: list[str], kind: str | None, missing_slides: bool) -> list[dict[str, Any]]:
    where = ["deleted_at is null"]
    params: dict[str, object] = {}
    if ids:
        where.append("id = any(:ids)")
        params["ids"] = [uuid.UUID(i) for i in ids]
    if kind:
        where.append("type = :kind")
        params["kind"] = kind
    if missing_slides:
        # The carousels that predate slide capture. `metadata` is the real column name;
        # `item_metadata` is the Python attribute (see CLAUDE.md).
        where.append("not (metadata ? 'slides')")
    sql = (
        "select id, type, processing_status, left(coalesce(title,''), 60) as title "
        f"from vault_items where {' and '.join(where)} order by created_at desc"
    )
    async with task_session() as session:
        result = await session.execute(text(sql), params)
        return [dict(row) for row in result.mappings().all()]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ids", nargs="*", help="memory ids to re-drive")
    parser.add_argument("--type", dest="kind", help="restrict to one ContentType")
    parser.add_argument(
        "--missing-slides",
        action="store_true",
        help="only items with no captured carousel slides",
    )
    parser.add_argument(
        "--apply", action="store_true", help="actually re-queue (default: dry run)"
    )
    args = parser.parse_args()
    if not args.ids and not args.kind and not args.missing_slides:
        parser.error("name some ids, or narrow with --type / --missing-slides")

    rows = await _rows(args.ids, args.kind, args.missing_slides)
    if not rows:
        print("nothing matched")
        return 0

    for row in rows:
        print(f"  {row['id']}  {row['processing_status']:<11} {row['title']}")
    print(f"{len(rows)} memor{'y' if len(rows) == 1 else 'ies'}")

    if not args.apply:
        print("\nDry run. Re-run with --apply to queue these.")
        return 0

    async with task_session() as session:
        # Back to `pending` before the enqueue, so a worker that picks the task up
        # immediately does not find a row still claiming to be finished -- and so a
        # crash between the two leaves an item the stranded-item sweeper will re-queue
        # rather than one that looks done and is not.
        await session.execute(
            text("update vault_items set processing_status = :s, processing_error = null, "
                 "retry_count = 0 where id = any(:ids)"),
            {"s": ProcessingStatus.pending.value, "ids": [row["id"] for row in rows]},
        )
        await session.commit()

    for row in rows:
        await enqueue_process_item(row["id"])
        print(f"queued {row['id']}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
