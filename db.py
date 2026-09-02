"""History store - every run, every article seen, every alert judged relevant.

Deliberately small: stdlib ``sqlite3``, three tables, plain SQL, no ORM and no
migration framework. This module is meant to be handed off and repointed, so
there is nothing to learn before reading it.

WHY IT LIVES UNDER THE PIPELINE, NOT IN THE UI
    serve.py is a demo surface and is expected to be dropped once the briefing
    sends itself by email. So the store is called from the *stages*: the live
    runner records history today, and a cron job that only sends mail records it
    the same way tomorrow, through the same two calls:

        keep = db.unseen(conn, key, name, articles)   # before judging
        db.record(conn, run_id, key, entity, arts, alerts)   # after judging

TWO THINGS IT BUYS BEYOND A LOG
    1. Cross-run dedupe. ``results`` is keyed by (report_type, entity, url_hash),
       so a URL seen in any earlier run is never searched into a second LLM call.
       That is a direct credit saving on every run after the first.
    2. A send queue. ``alerts.emailed_at IS NULL`` is the list of findings that
       have never gone out. The email step, when it is built, is:

           for row in db.pending(conn, priorities=("high",)):
               ...send...
           db.mark_emailed(conn, [row["alert_id"] for row in rows])

       Without it, an email-driven system re-sends the same finding every run.

generate_summaries.py deliberately does NOT record: it judges the offline article
fixtures, and mock findings in the history would corrupt the dedupe surface for
real runs.

CATEGORY IS A PLAIN TEXT COLUMN, ON PURPOSE
    CONTEXT.md specced a CHECK constraint listing the four original categories.
    The taxonomy is config-driven and has already changed once - tenants gained
    ma_ownership_change, competitors gained pricing_valuation_comps and
    corporate_investor_events - so a CHECK would mean an ALTER every time
    config/report_types.yaml is edited. The config is the authority on valid
    categories; the table just stores what the judge returned.

NO PORTFOLIO DATA. Entity name, city and roster rank only - the same fields the
search stage is already allowed to see. Rent, lease terms and addresses are not
in this schema at all, so the store cannot become a second copy of the rent roll.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

# search.py owns URL canonicalization (utm_* stripped, fragments dropped), and the
# dedupe keys must be built the same way there and here or the same story lands twice.
from search import url_hash

ROOT = Path(__file__).resolve().parent

# Override with NEWS_DB to point a run at a scratch file, or at a shared path
# once this is more than one person's machine.
DEFAULT_PATH = ROOT / "data" / "history.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id            TEXT PRIMARY KEY,
    report_type       TEXT NOT NULL,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    lookback_days     INTEGER,
    model             TEXT,
    entities_searched INTEGER DEFAULT 0,
    articles_found    INTEGER DEFAULT 0,
    articles_skipped  INTEGER DEFAULT 0,
    kept              INTEGER DEFAULT 0,
    dropped           INTEGER DEFAULT 0,
    status            TEXT DEFAULT 'running'
);

-- Every article ever fetched, judged or not. The dedupe surface: a URL in here
-- is never judged again for the same entity.
CREATE TABLE IF NOT EXISTS results (
    report_type        TEXT NOT NULL,
    entity_name        TEXT NOT NULL,
    url_hash           TEXT NOT NULL,
    source_url         TEXT NOT NULL,
    source_name        TEXT,
    title              TEXT,
    published_date     TEXT,
    first_seen_run     TEXT,
    first_seen_at      TEXT NOT NULL,
    is_relevant        INTEGER,
    reason_if_excluded TEXT,
    PRIMARY KEY (report_type, entity_name, url_hash)
);

-- Judged-relevant findings. One row per (entity, url): the same story arriving
-- from a second outlet is a second row, the same URL twice is not.
CREATE TABLE IF NOT EXISTS alerts (
    alert_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    report_type    TEXT NOT NULL,
    entity_name    TEXT NOT NULL,
    city           TEXT,
    rank           INTEGER,
    segment        TEXT,
    category       TEXT NOT NULL,
    priority       TEXT NOT NULL,
    headline       TEXT,
    client_summary TEXT,
    confidence     REAL,
    ceo_flag       INTEGER DEFAULT 0,
    source_name    TEXT,
    source_url     TEXT,
    url_hash       TEXT NOT NULL,
    published_date TEXT,
    -- When the DEVELOPMENT happened, as distinct from published_date, which is
    -- when someone wrote about it. The briefing shows this one.
    event_date     TEXT,
    event_key      TEXT,
    deal_metrics   TEXT,
    run_id         TEXT,
    created_at     TEXT NOT NULL,
    emailed_at     TEXT,
    UNIQUE (report_type, entity_name, url_hash)
);

-- emailed_at first: "what has never been sent" is the query the email step runs.
CREATE INDEX IF NOT EXISTS idx_alerts_event    ON alerts (report_type, entity_name, event_key);
CREATE INDEX IF NOT EXISTS idx_alerts_unsent   ON alerts (emailed_at, priority);
CREATE INDEX IF NOT EXISTS idx_alerts_type     ON alerts (report_type, priority);
CREATE INDEX IF NOT EXISTS idx_alerts_ceo      ON alerts (ceo_flag);
CREATE INDEX IF NOT EXISTS idx_alerts_entity   ON alerts (entity_name);
CREATE INDEX IF NOT EXISTS idx_runs_type       ON runs (report_type, started_at);
"""


def now() -> str:
    """UTC, second resolution. Stored as ISO text so any client can read it."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open the history db, creating it and its schema if absent.

    Re-runnable: every statement in SCHEMA is IF NOT EXISTS, so calling this on
    an existing database is a no-op that just hands back a connection.
    """
    target = Path(path or os.environ.get("NEWS_DB") or DEFAULT_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    # Survives a killed run without corrupting the file, and lets a reader (the
    # UI) query while a run is writing.
    conn.execute("PRAGMA journal_mode=WAL")
    # Migrate BEFORE the schema script: SCHEMA indexes the new columns, and
    # CREATE INDEX IF NOT EXISTS still fails on a column the old table lacks.
    # On a fresh database migrate() finds no tables and does nothing.
    migrate(conn)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# Columns added after the first databases were already in use. CREATE TABLE
# IF NOT EXISTS cannot add a column to a table that exists, so they are applied
# here instead of being left to a manual rebuild - a store of real findings
# should survive a schema change.
MIGRATIONS: dict[str, dict[str, str]] = {
    "alerts": {
        "event_date": "TEXT",
        "event_key": "TEXT",
        "deal_metrics": "TEXT",
    },
}


def migrate(conn) -> list[str]:
    """Add any missing columns to existing tables. Returns what it added.

    Additive only: no column is ever dropped or retyped, so an older build
    reading a migrated database still works.
    """
    applied = []
    for table, columns in MIGRATIONS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not have:
            continue
        for name, decl in columns.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                applied.append(f"{table}.{name}")
    if applied:
        conn.commit()
    return applied


# -- runs ------------------------------------------------------------------


def start_run(conn, report_type: str, *, lookback_days=None, model=None, run_id=None) -> str:
    """Open a run row and return its id. Reuses the caller's id when given one,
    so the UI's run id and the stored one are the same string."""
    rid = run_id or uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT OR REPLACE INTO runs (run_id, report_type, started_at, lookback_days, model, status)"
        " VALUES (?, ?, ?, ?, ?, 'running')",
        (rid, report_type, now(), lookback_days, model),
    )
    conn.commit()
    return rid


def finish_run(conn, run_id: str, *, status: str = "complete", **counters) -> None:
    """Stamp a run finished. Counter names must match the runs columns."""
    allowed = {
        "entities_searched", "articles_found", "articles_skipped", "kept", "dropped",
    }
    bad = set(counters) - allowed
    if bad:
        raise ValueError(f"unknown run counters: {sorted(bad)}")
    sets = "".join(f", {k} = ?" for k in counters)
    conn.execute(
        f"UPDATE runs SET finished_at = ?, status = ?{sets} WHERE run_id = ?",
        (now(), status, *counters.values(), run_id),
    )
    conn.commit()


# -- the two calls the pipeline makes --------------------------------------


def unseen(conn, report_type: str, entity_name: str, articles: list[dict]) -> list[dict]:
    """Drop articles already recorded for this entity in any earlier run.

    Called between search and judge. ``search.py`` already stamps each article
    with ``url_hash``; anything without one is passed through rather than
    silently dropped, so a caller that skips the hash still works.
    """
    if not articles:
        return []
    hashes = [a["url_hash"] for a in articles if a.get("url_hash")]
    if not hashes:
        return list(articles)
    rows = conn.execute(
        "SELECT url_hash FROM results WHERE report_type = ? AND entity_name = ?"
        f" AND url_hash IN ({','.join('?' * len(hashes))})",
        (report_type, entity_name, *hashes),
    ).fetchall()
    seen = {r["url_hash"] for r in rows}
    return [a for a in articles if a.get("url_hash") not in seen]


def fresh_events(conn, report_type: str, entity_name: str, alerts: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split judged findings into (new events, events already reported before).

    ``unseen()`` stops the same *URL* being judged twice; this stops the same
    *event* being reported twice from a different URL. A deal covered by CoStar
    last week and Bisnow this week is one development, and a briefing that leads
    with it again is telling the reader something they have already acted on.

    Only findings carrying an ``event_key`` can be matched - one without a key is
    treated as new, since we have nothing to compare it against.
    """
    keys = [a["event_key"] for a in alerts if a.get("event_key")]
    if not keys:
        return list(alerts), []
    rows = conn.execute(
        "SELECT DISTINCT event_key FROM alerts WHERE report_type = ? AND entity_name = ?"
        f" AND event_key IN ({','.join('?' * len(keys))})",
        (report_type, entity_name, *keys),
    ).fetchall()
    known = {r["event_key"] for r in rows}
    new = [a for a in alerts if a.get("event_key") not in known]
    repeats = [a for a in alerts if a.get("event_key") in known]
    return new, repeats


def record(conn, run_id, report_type: str, entity: dict, articles: list[dict], alerts: list[dict]) -> int:
    """Persist one entity's judged batch: every article into results, the kept
    ones into alerts. Returns the number of alert rows actually inserted.

    ``INSERT OR IGNORE`` on both tables is the final dedupe backstop, so calling
    this twice for the same batch cannot double-report a finding.
    """
    def key_of(row: dict) -> str:
        """url_hash if the caller set one, else derived from the url."""
        return row.get("url_hash") or (url_hash(row["source_url"]) if row.get("source_url") else "")

    kept_by_hash = {key_of(a): a for a in alerts}
    kept_by_hash.pop("", None)
    stamp = now()

    for art in articles:
        h = key_of(art)
        if not h:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO results (report_type, entity_name, url_hash, source_url,"
            " source_name, title, published_date, first_seen_run, first_seen_at, is_relevant,"
            " reason_if_excluded) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                report_type, entity["display_name"], h, art.get("source_url", ""),
                art.get("source_name"), art.get("title"), art.get("published_date"),
                run_id, stamp, 1 if h in kept_by_hash else 0,
                art.get("reason_if_excluded"),
            ),
        )

    inserted = 0
    for alert in alerts:
        cur = conn.execute(
            "INSERT OR IGNORE INTO alerts (report_type, entity_name, city, rank, segment,"
            " category, priority, headline, client_summary, confidence, ceo_flag, source_name,"
            " source_url, url_hash, published_date, event_date, event_key, deal_metrics,"
            " run_id, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                report_type, entity["display_name"], entity.get("city"), entity.get("rank"),
                entity.get("segment"), alert["category"], alert["priority"], alert.get("headline"),
                alert.get("client_summary"), alert.get("confidence"),
                1 if alert.get("ceo_flag") else 0, alert.get("source_name"),
                alert.get("source_url"), key_of(alert), alert.get("published_date"),
                alert.get("event_date"), alert.get("event_key"), alert.get("deal_metrics"),
                run_id, stamp,
            ),
        )
        inserted += cur.rowcount or 0
    conn.commit()
    return inserted


# -- what the email step will use ------------------------------------------


def pending(conn, *, report_type=None, priorities=("high",), ceo_only=False) -> list[sqlite3.Row]:
    """Alerts that have never been emailed. This is the handoff seam: an
    email-only build needs this function and mark_emailed(), and nothing else
    from the UI."""
    sql = ["SELECT * FROM alerts WHERE emailed_at IS NULL"]
    args: list = []
    if report_type:
        sql.append("AND report_type = ?")
        args.append(report_type)
    if priorities:
        sql.append(f"AND priority IN ({','.join('?' * len(priorities))})")
        args += list(priorities)
    if ceo_only:
        sql.append("AND ceo_flag = 1")
    sql.append("ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,"
               " rank IS NULL, rank, created_at")
    return conn.execute(" ".join(sql), args).fetchall()


def mark_emailed(conn, alert_ids: list[int]) -> None:
    """Stamp alerts as sent so the next run does not resend them."""
    if not alert_ids:
        return
    stamp = now()
    conn.executemany(
        "UPDATE alerts SET emailed_at = ? WHERE alert_id = ?",
        [(stamp, i) for i in alert_ids],
    )
    conn.commit()


# -- read helpers for a history view ---------------------------------------


def recent_runs(conn, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
    ).fetchall()


def alerts_for(conn, *, report_type=None, entity_name=None, limit: int = 200) -> list[sqlite3.Row]:
    sql = ["SELECT * FROM alerts WHERE 1=1"]
    args: list = []
    if report_type:
        sql.append("AND report_type = ?")
        args.append(report_type)
    if entity_name:
        sql.append("AND entity_name = ?")
        args.append(entity_name)
    sql.append("ORDER BY created_at DESC, alert_id DESC LIMIT ?")
    args.append(limit)
    return conn.execute(" ".join(sql), args).fetchall()


def stats(conn) -> dict:
    """One-line health check, also what `python db.py` prints."""
    one = lambda q: conn.execute(q).fetchone()[0]  # noqa: E731
    return {
        "runs": one("SELECT COUNT(*) FROM runs"),
        "articles_seen": one("SELECT COUNT(*) FROM results"),
        "alerts": one("SELECT COUNT(*) FROM alerts"),
        "unsent": one("SELECT COUNT(*) FROM alerts WHERE emailed_at IS NULL"),
        "ceo_flagged": one("SELECT COUNT(*) FROM alerts WHERE ceo_flag = 1"),
    }


def main() -> int:
    """`python db.py` creates the database if needed and prints its state."""
    conn = connect()
    path = conn.execute("PRAGMA database_list").fetchone()["file"]
    print(f"history db: {path}")
    for key, value in stats(conn).items():
        print(f"  {key:<14} {value}")
    runs = recent_runs(conn, 5)
    if runs:
        print("\n  recent runs")
        for r in runs:
            print(f"    {r['started_at']}  {r['report_type']:<12} {r['status']:<9}"
                  f" kept={r['kept']} dropped={r['dropped']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
