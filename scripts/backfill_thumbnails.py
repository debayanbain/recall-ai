"""Copy still-living scraped thumbnails into the bucket, once.

`0015_thumbnail_key` deliberately backfills nothing: by the time it shipped, most stored
`og:image` URLs had already expired, and a migration that makes thousands of outbound
requests is a migration that times out halfway through and leaves no record of where it
got to. This is that work, as a script you can run twice.

It only touches items with no mirror yet, and it is safe to re-run: a source that is
already dead is logged and skipped, and `thumbnails.mirror` itself never raises.

    uv run python scripts/backfill_thumbnails.py           # report only
    uv run python scripts/backfill_thumbnails.py --apply   # mirror and commit

Lives in `scripts/` rather than as a beat task because it is a one-off for rows captured
before the feature existed. Every capture from here on mirrors at processing time.
"""
from __future__ import annotations

import argparse
import asyncio

from sqlmodel import col, select

from app.db.session import task_session
from app.models.vault import VaultItem
from app.services import thumbnails
from app.storage import get_storage


async def main(apply: bool) -> None:
    storage = get_storage()
    if storage is None:
        print("No bucket configured; nothing to do.")
        return

    async with task_session() as session:
        rows = await session.exec(
            select(VaultItem).where(
                col(VaultItem.thumbnail_url).is_not(None),
                col(VaultItem.thumbnail_key).is_(None),
                col(VaultItem.deleted_at).is_(None),
            )
        )
        items = list(rows.all())
        print(f"{len(items)} item(s) with a scraped thumbnail and no mirror.")

        mirrored = 0
        for item in items:
            if not apply:
                print(f"  would try {item.id}  {(item.thumbnail_url or '')[:80]}")
                continue
            await thumbnails.mirror(item, storage)
            if item.thumbnail_key:
                mirrored += 1
                session.add(item)

        if apply:
            await session.commit()
            print(f"Mirrored {mirrored} of {len(items)}; the rest had already expired.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the mirrors (default: dry run)")
    asyncio.run(main(parser.parse_args().apply))
