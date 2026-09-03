"""Copy the SQLite history into Postgres. One-shot, re-runnable, additive.

    python copy_history.py --dry-run      # count what would move, write nothing
    python copy_history.py                # data/history.db  ->  $DATABASE_URL

WHY A SCRIPT AND NOT A MIGRATION
    The dedupe surface is the one thing in this project that cannot be
    regenerated: `results` is why a URL already judged is never judged again, and
    `alerts.emailed_at` is why a finding is never sent twice. Rebuilding it would
    mean re-paying for every article ever fetched, and would still not recover
    what was already emailed. So the move is a deliberate, watched step, not
    something that happens as a side effect of pointing at a new store.

    Nothing is deleted from the source. Run it, check the numbers, and keep the
    file until you trust the new store - a copy that exists in one place is not
    a copy.

SAFE TO RUN TWICE
    Every insert ignores a row that is already there, so a re-run copies only
    what is missing. That is also what makes it usable as a top-up if a run lands
    in SQLite after the first copy.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import db  # noqa: E402

# Copied in this order so a row never lands before the run it belongs to. Nothing
# here enforces a foreign key, but a store that reads consistently mid-copy is
# worth the two seconds it costs to order the list.
TABLES = ("runs", "results", "alerts", "run_progress")


def rows_of(conn, table: str) -> list[dict]:
    return [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]


def copy(source, target, *, dry_run: bool = False) -> dict[str, dict]:
    """Copy every table, reporting what moved and what was already there."""
    report: dict[str, dict] = {}
    for table in TABLES:
        rows = rows_of(source, table)
        before = target.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        written = 0
        if not dry_run:
            for row in rows:
                written += db.insert(target, table, row)
            target.commit()
        report[table] = {
            "source": len(rows), "already_there": before, "written": written,
            "skipped": len(rows) - written if not dry_run else 0,
        }
    return report


def reset_identity(target) -> None:
    """Point the alerts id sequence past the highest copied id.

    The rows keep their original alert_id so that anything already recorded as
    emailed stays matched to the same finding. Postgres does not know its
    sequence was bypassed, and the next insert would collide without this.
    """
    target.execute(
        "SELECT setval(pg_get_serial_sequence('alerts', 'alert_id'),"
        " COALESCE((SELECT MAX(alert_id) FROM alerts), 0) + 1, false) AS n"
    ).fetchone()
    target.commit()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="source", default=None,
                    help="source SQLite file (default: data/history.db)")
    ap.add_argument("--to", dest="target", default=None,
                    help="target connection string (default: $DATABASE_URL)")
    ap.add_argument("--dry-run", action="store_true", help="count only, write nothing")
    args = ap.parse_args()

    dsn = args.target or os.environ.get("DATABASE_URL")
    if not dsn:
        print("no target: set DATABASE_URL in .env or pass --to", file=sys.stderr)
        return 2

    source_path = Path(args.source) if args.source else db.DEFAULT_PATH
    if not source_path.exists():
        print(f"no source database at {source_path}", file=sys.stderr)
        return 2

    source = db.connect(source_path)
    target = db.connect(url=dsn)
    print(f"from  {db.describe(source)}")
    print(f"to    {db.describe(target)}")
    print("dry run - nothing will be written\n" if args.dry_run else "")

    report = copy(source, target, dry_run=args.dry_run)
    width = max(len(t) for t in TABLES)
    for table, counts in report.items():
        line = f"  {table:<{width}}  {counts['source']:>6} in source"
        if args.dry_run:
            line += f", {counts['already_there']:>6} already in target"
        else:
            line += f" -> {counts['written']:>6} written, {counts['skipped']:>6} already there"
        print(line)

    if not args.dry_run:
        reset_identity(target)
        print("\nverifying")
        for table in TABLES:
            here = source.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            there = target.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            mark = "ok " if there >= here else "MISSING ROWS"
            print(f"  {mark} {table:<{width}}  source {here:>6}   target {there:>6}")
        print("\nThe SQLite file is untouched. Keep it until you trust the new store.")

    source.close()
    target.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
