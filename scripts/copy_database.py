"""Copy every row from one configured database into the other -- Neon to Supabase, or back.

Both databases stay configured side by side (`DATABASE_URL` is Neon,
`SUPABASE_DATABASE_URL` is Supabase) and `DATABASE_PROVIDER` picks the one the app talks
to. They are independent: flipping the selector moves no data, so a vault that exists
on one is empty on the other until this copies it.

    uv run python scripts/copy_database.py --from neon --to supabase           # report only
    uv run python scripts/copy_database.py --from neon --to supabase --apply   # copy

Both URLs come from `.env` unless `--env-file` names another file -- production is
`--env-file .env.production`, which keeps its URLs off the command line and out of shell
history. A file that does not exist is refused rather than quietly read as empty. The
host of each side is printed first, so it is visible which two databases are involved.

Before it copies anything it requires:

  * **the same Alembic revision on both sides.** The target's schema comes from
    `alembic upgrade head`, never from this script, so a copy can only land in a schema
    the migrations built:  DATABASE_PROVIDER=supabase uv run alembic upgrade head
    `--allow-revision-mismatch` waives this one check, for a source that must not be
    written to at all -- migrating it just to copy out of it would be the only write it
    ever receives from here. The target must still have a revision, and the column and
    type comparison below still has to pass, which is what the check was standing in for.
  * **an empty target.** It never merges and never deletes. A target holding even one
    row is refused, so a mistyped `--to` cannot interleave two vaults or wipe one.
  * **matching columns and types** for every table, compared by name -- the two schemas
    were built in different orders (one by migrations over months, one by create_all),
    so column *positions* differ and are never relied on.

How it copies:

  * The source is read inside ONE read-only, repeatable-read transaction: a consistent
    snapshot across every table, and a database the server itself will refuse to let
    this script write to.
  * The target is written inside ONE transaction. Anything failing -- a type, a foreign
    key, the row-count check at the end -- rolls the whole copy back and leaves the
    target as empty as it was.
  * Tables go parents-first, ordered from the target's own foreign keys, as binary COPY
    with explicit column lists. Generated columns (memory_connections.pair_low /
    pair_high) are left out and recomputed by the target.
  * Sequences are moved past the copied ids, and every table's count is compared with
    the source's before the commit.

Uploaded files are not touched: rows carry `storage_key`, and the bucket is the same
whichever database points at it. Connection URLs are never printed -- they carry the
password.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Literal, cast

import asyncpg
from dotenv import dotenv_values
from sqlalchemy.engine.url import make_url

from app.core.config import Settings, settings

Provider = Literal["neon", "supabase"]

_DATABASE_KEYS = ("DATABASE_URL", "SUPABASE_DATABASE_URL", "DATABASE_PROVIDER")

# Owned by Alembic, not data: both sides must already agree on it, and it is checked
# rather than copied.
_NOT_DATA = {"alembic_version"}


def _ident(name: str) -> str:
    """Quote an identifier. Every name here comes from the catalog, and is quoted anyway."""
    return '"' + name.replace('"', '""') + '"'


async def _connect(config: Settings, provider: Provider) -> asyncpg.Connection:
    try:
        url = make_url(config.database_url_for(provider))
    except RuntimeError as exc:  # the setting is missing; the message names it, not a URL
        raise SystemExit(f"{provider}: {exc}") from None
    print(f"{provider}: {url.host}/{url.database}")
    ssl = url.query.get("ssl")
    try:
        return await asyncpg.connect(
            user=url.username,
            password=url.password,
            host=url.host,
            port=url.port or 5432,
            database=url.database,
            ssl=ssl if isinstance(ssl, str) else None,
            timeout=30,
        )
    except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
        # The server's own message ("exceeded the quota", "password authentication
        # failed") is what to act on; asyncpg does not put the password in it.
        raise SystemExit(f"{provider}: cannot connect -- {type(exc).__name__}: {exc}") from None


async def _revision(conn: asyncpg.Connection) -> str | None:
    if not await conn.fetchval("SELECT to_regclass('public.alembic_version') IS NOT NULL"):
        return None
    return cast(str | None, await conn.fetchval("SELECT version_num FROM alembic_version"))


def _kind(udt_name: str, max_length: int | None) -> str:
    """A column's type for comparison.

    A `varchar` with no length and a `text` differ in name only: same storage, same binary
    COPY format. They do turn up side by side, because a schema built by create_all takes
    the model's `str` (varchar) where the migrations that built the other one wrote `Text`.
    A bounded varchar stays distinct -- it can refuse a value the other side holds.
    """
    if udt_name in ("text", "varchar"):
        return "text" if max_length is None else f"varchar({max_length})"
    return udt_name


async def _columns(conn: asyncpg.Connection) -> dict[str, dict[str, str]]:
    """{table: {column: type}} for base tables in public, generated columns excluded."""
    rows = await conn.fetch(
        """
        SELECT c.table_name, c.column_name, c.udt_name, c.character_maximum_length
        FROM information_schema.columns c
        JOIN information_schema.tables t
          ON t.table_schema = c.table_schema AND t.table_name = c.table_name
        WHERE c.table_schema = 'public'
          AND t.table_type = 'BASE TABLE'
          AND c.is_generated = 'NEVER'
        ORDER BY c.table_name, c.ordinal_position
        """
    )
    tables: dict[str, dict[str, str]] = {}
    for row in rows:
        if row["table_name"] not in _NOT_DATA:
            kind = _kind(row["udt_name"], row["character_maximum_length"])
            tables.setdefault(row["table_name"], {})[row["column_name"]] = kind
    return tables


async def _parent_first(conn: asyncpg.Connection, tables: set[str]) -> list[str]:
    edges = await conn.fetch(
        """
        SELECT child.relname AS child, parent.relname AS parent
        FROM pg_constraint con
        JOIN pg_class child ON child.oid = con.conrelid
        JOIN pg_class parent ON parent.oid = con.confrelid
        JOIN pg_namespace n ON n.oid = child.relnamespace
        WHERE con.contype = 'f' AND n.nspname = 'public'
        """
    )
    graph: dict[str, set[str]] = {table: set() for table in tables}
    for edge in edges:
        child, parent = edge["child"], edge["parent"]
        if child in graph and parent in graph and child != parent:
            graph[child].add(parent)
    try:
        return list(TopologicalSorter(graph).static_order())
    except CycleError as exc:
        raise SystemExit(f"Foreign keys form a cycle ({exc.args[1]}); refusing to guess.") from exc


async def _count(conn: asyncpg.Connection, table: str) -> int:
    return cast(int, await conn.fetchval(f"SELECT count(*) FROM public.{_ident(table)}"))


async def _reset_sequences(conn: asyncpg.Connection) -> int:
    owned = await conn.fetch(
        """
        SELECT table_name, column_name,
               pg_get_serial_sequence(format('public.%I', table_name), column_name) AS seq
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND pg_get_serial_sequence(format('public.%I', table_name), column_name) IS NOT NULL
        """
    )
    for row in owned:
        table, column = _ident(row["table_name"]), _ident(row["column_name"])
        await conn.execute(
            f"SELECT setval($1, COALESCE((SELECT max({column}) FROM public.{table}), 1), "
            f"(SELECT max({column}) FROM public.{table}) IS NOT NULL)",
            row["seq"],
        )
    return len(owned)


def _schema_problems(
    source: dict[str, dict[str, str]], target: dict[str, dict[str, str]]
) -> list[str]:
    problems = []
    for table, columns in source.items():
        if table not in target:
            problems.append(f"{table}: missing in target")
            continue
        for column, kind in columns.items():
            if column not in target[table]:
                problems.append(f"{table}.{column}: missing in target")
            elif (other := target[table][column]) != kind:
                problems.append(f"{table}.{column}: {kind} in source, {other} in target")
    return problems


async def main(
    config: Settings,
    source: Provider,
    target: Provider,
    apply: bool,
    allow_revision_mismatch: bool = False,
) -> None:
    src = await _connect(config, source)
    dst = await _connect(config, target)
    try:
        # The snapshot every count and every COPY below reads from. read-only makes the
        # server refuse any write to the source, whatever this script does by mistake.
        async with src.transaction(isolation="repeatable_read", readonly=True):
            src_rev, dst_rev = await _revision(src), await _revision(dst)
            print(f"source {source}: revision {src_rev}")
            print(f"target {target}: revision {dst_rev}")
            if dst_rev is None:
                raise SystemExit(
                    f"The {target} database has no schema yet. Build it first:\n"
                    f"  DATABASE_PROVIDER={target} uv run alembic upgrade head"
                )
            if src_rev != dst_rev:
                if not allow_revision_mismatch:
                    raise SystemExit(
                        "The two databases are on different migrations. Bring both to head:\n"
                        "  DATABASE_PROVIDER=<provider> uv run alembic upgrade head\n"
                        "or pass --allow-revision-mismatch if the source must stay untouched."
                    )
                print("revisions differ: allowed by flag, relying on the column comparison")

            src_columns, dst_columns = await _columns(src), await _columns(dst)
            problems = _schema_problems(src_columns, dst_columns)
            if problems:
                raise SystemExit("Schemas differ:\n  " + "\n  ".join(problems))

            order = await _parent_first(dst, set(src_columns))
            counts = {table: await _count(src, table) for table in order}
            occupied = {t: n for t in order if (n := await _count(dst, t))}

            print(f"\n{'table':28} {'rows':>8}")
            for table in order:
                print(f"{table:28} {counts[table]:>8}")
            print(f"{'total':28} {sum(counts.values()):>8}")

            if occupied:
                raise SystemExit(
                    f"\nThe {target} database already holds rows "
                    f"({', '.join(f'{t}={n}' for t, n in occupied.items())}). This script "
                    "only copies into an empty database; it never merges or deletes."
                )
            if not apply:
                print(f"\nDry run. Nothing written. Re-run with --apply to copy into {target}.")
                return

            async with dst.transaction():
                with tempfile.TemporaryDirectory() as tmp:
                    for index, table in enumerate(order):
                        columns = list(src_columns[table])
                        spool = Path(tmp) / f"{index}.copy"
                        select = ", ".join(_ident(c) for c in columns)
                        await src.copy_from_query(
                            f"SELECT {select} FROM public.{_ident(table)}",
                            output=str(spool),
                            format="binary",
                        )
                        await dst.copy_to_table(
                            table,
                            schema_name="public",
                            source=str(spool),
                            columns=columns,
                            format="binary",
                        )
                        spool.unlink()
                        print(f"  copied {table}")

                sequences = await _reset_sequences(dst)
                mismatched = {
                    t: (counts[t], n) for t in order if (n := await _count(dst, t)) != counts[t]
                }
                if mismatched:
                    # Raising inside the transaction block is what rolls the copy back.
                    raise SystemExit(f"Row counts differ after copy, rolled back: {mismatched}")

            print(
                f"\nCopied {sum(counts.values())} rows across {len(order)} tables into "
                f"{target}; {sequences} sequence(s) moved past the copied ids."
            )
    finally:
        await src.close()
        await dst.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--from", dest="source", choices=["neon", "supabase"], required=True)
    parser.add_argument("--to", dest="target", choices=["neon", "supabase"], required=True)
    parser.add_argument("--apply", action="store_true", help="copy; without it, report only")
    parser.add_argument(
        "--allow-revision-mismatch",
        action="store_true",
        help="copy between different Alembic revisions if every column and type still matches",
    )
    parser.add_argument(
        "--env-file",
        help="read both URLs from this file instead of .env (e.g. .env.production)",
    )
    args = parser.parse_args()
    if args.source == args.target:
        sys.exit("--from and --to must be different databases.")
    config = settings
    if args.env_file:
        # pydantic-settings skips a missing env file without a word, which would leave
        # the defaults -- or whatever the shell exports -- standing in for the file the
        # operator named. Refused instead.
        if not Path(args.env_file).is_file():
            sys.exit(f"--env-file {args.env_file}: no such file.")
        # pydantic-settings ranks an exported shell variable ABOVE the env file, so a
        # stray `export DATABASE_URL=...` would silently redirect the copy. The database
        # keys are therefore passed from the file explicitly, which outranks both.
        from_file = {
            key: value
            for key, value in dotenv_values(args.env_file).items()
            if key in _DATABASE_KEYS and value
        }
        config = Settings(_env_file=args.env_file, **from_file)  # type: ignore[arg-type]
        print(f"settings from {args.env_file}")
    asyncio.run(
        main(config, args.source, args.target, args.apply, args.allow_revision_mismatch)
    )
