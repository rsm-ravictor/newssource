"""Read-only web view of the history store, deployed as a Vercel function.

WHAT THIS IS FOR
    The runner is a long batch job - a full roster takes hours - and nothing about
    that fits inside a serverless request. So the runner stays local and this
    deploys instead: a page over the same Postgres store, for people who need to
    read what was found without running anything.

READ-ONLY, AND DELIBERATELY SO
    Every statement below is a SELECT. Nothing here judges, searches, sends or
    writes, so the worst a bug can do is show the wrong number. To have that
    guaranteed rather than reviewed, give this deployment its own Neon role with
    only SELECT granted and point its DATABASE_URL at that; the code is unchanged.

WHY IT DOES NOT IMPORT db.py
    A serverless bundle only ships what the builder can see, and db.py pulls in
    search.py for url_hash, which pulls in the search client - none of which a
    reader needs. The handful of SELECTs here are worth the duplication to keep
    the deployment one file with one dependency.

NO PORTFOLIO DATA, the same rule the store follows: entity name, city and roster
rank only. Rent, lease terms and addresses are not in these tables at all.
"""

from __future__ import annotations

import html
import os
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

PRIORITIES = ("high", "medium", "low")
REPORT_TYPES = ("tenants", "competitors")


def connect():
    """Open the store this deployment reads. Vercel injects DATABASE_URL, either
    from the Neon integration or from the project's environment settings."""
    import psycopg
    from psycopg.rows import dict_row

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set on this deployment")
    return psycopg.connect(dsn, row_factory=dict_row)


def query(conn, sql: str, args=()) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def totals(conn) -> dict:
    rows = query(conn, """
        SELECT (SELECT COUNT(*) FROM runs)                            AS runs,
               (SELECT COUNT(*) FROM alerts)                          AS alerts,
               (SELECT COUNT(*) FROM results)                         AS articles,
               (SELECT COUNT(*) FROM alerts WHERE emailed_at IS NULL) AS unsent,
               (SELECT COUNT(*) FROM alerts WHERE ceo_flag = 1)       AS ceo
    """)
    return rows[0] if rows else {}


def findings(conn, *, report_type="", priority="", entity="", limit=100) -> list[dict]:
    sql = ["SELECT * FROM alerts WHERE 1=1"]
    args: list = []
    if report_type:
        sql.append("AND report_type = %s")
        args.append(report_type)
    if priority:
        sql.append("AND priority = %s")
        args.append(priority)
    if entity:
        # Case-insensitive contains. The pattern is still a bound parameter, so a
        # name containing % is matched as text rather than interpreted.
        sql.append("AND entity_name ILIKE %s")
        args.append("%" + entity + "%")
    sql.append("ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,"
               " created_at DESC LIMIT %s")
    args.append(limit)
    return query(conn, " ".join(sql), args)


def recent_runs(conn, limit: int = 10) -> list[dict]:
    return query(conn, "SELECT * FROM runs ORDER BY started_at DESC LIMIT %s", (limit,))


# ---------------------------------------------------------------------------
# Rendering. Plain strings rather than a template engine: one file, one
# dependency, nothing to resolve at cold start.
# ---------------------------------------------------------------------------

CSS = """
:root {
  --sunken:#f6f8fc; --card:#ffffff; --card-head:#f2f5fa; --pill:#e9edf4;
  --line:#dfe4ee; --text:#0f172a; --muted:#475467; --faint:#79839a;
  --accent:#0369a1; --accent-bg:rgba(3,105,161,0.08);
  --bad:#b91c1c; --warn:#a16207;
}
@media (prefers-color-scheme: dark) {
  :root {
    --sunken:#10151e; --card:#121824; --card-head:#161d29; --pill:#1c2532;
    --line:#262d3b; --text:#e6e9ef; --muted:#8b95a7; --faint:#5d6779;
    --accent:#7dd3fc; --accent-bg:rgba(125,211,252,0.10);
    --bad:#f87171; --warn:#fbbf24;
  }
}
* { box-sizing: border-box; }
body {
  margin:0; background:var(--sunken); color:var(--text);
  font:14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}
.wrap { max-width:1060px; margin:0 auto; padding:26px 20px 60px; }
h1 { font-size:19px; margin:0 0 3px; letter-spacing:-0.01em; }
.sub { color:var(--faint); font-size:12px; margin:0 0 22px; }
.tiles { display:flex; gap:10px; flex-wrap:wrap; margin-bottom:22px; }
.tile { background:var(--card); border:1px solid var(--line); border-radius:10px;
        padding:11px 16px; min-width:104px; }
.tile b { display:block; font-size:21px; font-weight:600; letter-spacing:-0.02em; }
.tile span { font-size:11px; color:var(--faint); text-transform:uppercase; letter-spacing:0.05em; }
form { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-bottom:18px; }
select, input, button {
  font:inherit; font-size:13px; color:var(--text); background:var(--card);
  border:1px solid var(--line); border-radius:7px; padding:7px 10px;
}
button { background:var(--accent-bg); border-color:var(--accent); color:var(--accent);
         cursor:pointer; font-weight:600; }
.card { background:var(--card); border:1px solid var(--line); border-radius:11px;
        overflow:hidden; margin-bottom:20px; }
.card h2 { font-size:12px; text-transform:uppercase; letter-spacing:0.06em; color:var(--muted);
  margin:0; padding:11px 16px; background:var(--card-head);
  border-bottom:1px solid var(--line); font-weight:600; }
.finding { padding:14px 16px; border-bottom:1px solid var(--line); }
.finding:last-child { border-bottom:0; }
.head { display:flex; gap:9px; align-items:baseline; flex-wrap:wrap; margin-bottom:5px; }
.entity { font-weight:600; }
.tag { font-size:10.5px; text-transform:uppercase; letter-spacing:0.05em; padding:2px 7px;
       border-radius:20px; background:var(--pill); color:var(--muted); }
.tag.high { color:var(--bad); }
.tag.medium { color:var(--warn); }
.tag.low { color:var(--faint); }
.headline { font-weight:500; margin:0 0 5px; }
.summary { color:var(--muted); font-size:13px; margin:0 0 7px; }
.meta { font-size:11.5px; color:var(--faint); display:flex; gap:10px; flex-wrap:wrap; }
.meta a { color:var(--accent); }
.scroll { overflow-x:auto; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th, td { text-align:left; padding:9px 16px; border-bottom:1px solid var(--line); white-space:nowrap; }
th { font-size:11px; text-transform:uppercase; letter-spacing:0.05em; color:var(--faint);
     font-weight:600; }
tr:last-child td { border-bottom:0; }
.empty { padding:34px 16px; text-align:center; color:var(--faint); }
.note { font-size:11.5px; color:var(--faint); margin-top:26px; line-height:1.6; }
"""


def esc(value) -> str:
    return html.escape("" if value is None else str(value))


def options(values, selected) -> str:
    out = ['<option value="">all</option>']
    for value in values:
        mark = " selected" if value == selected else ""
        out.append('<option value="{0}"{1}>{0}</option>'.format(esc(value), mark))
    return "".join(out)


def finding_html(row: dict) -> str:
    bits = [b for b in (
        esc(row.get("source_name")),
        "event " + esc(row["event_date"]) if row.get("event_date") else "",
        "published " + esc(row["published_date"]) if row.get("published_date") else "",
        esc(row.get("deal_metrics")) if row.get("deal_metrics") else "",
    ) if b]
    link = ""
    if row.get("source_url"):
        link = '<a href="{}" target="_blank" rel="noopener noreferrer">source</a>'.format(
            esc(row["source_url"]))
    rank = " &middot; #{}".format(row["rank"]) if row.get("rank") else ""
    return """
    <div class="finding">
      <div class="head">
        <span class="entity">{entity}</span>
        <span class="tag {priority}">{priority}</span>
        <span class="tag">{category}</span>
        <span class="tag">{report_type}{rank}</span>
      </div>
      <p class="headline">{headline}</p>
      <p class="summary">{summary}</p>
      <div class="meta">{meta} {link}</div>
    </div>""".format(
        entity=esc(row.get("entity_name")),
        priority=esc(row.get("priority")),
        category=esc(row.get("category")),
        report_type=esc(row.get("report_type")),
        rank=rank,
        headline=esc(row.get("headline")),
        summary=esc(row.get("client_summary")),
        meta=" &middot; ".join(bits),
        link=link,
    )


def runs_html(rows: list[dict]) -> str:
    if not rows:
        return '<div class="empty">No runs recorded yet.</div>'
    body = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{:,}</td></tr>".format(
            esc(r["started_at"])[:16].replace("T", " "),
            esc(r["report_type"]),
            esc(r["status"]),
            r["entities_searched"] or 0,
            r["kept"] or 0,
            r["tavily_queries"] or 0,
            (r["input_tokens"] or 0) + (r["output_tokens"] or 0),
        )
        for r in rows
    )
    return ('<div class="scroll"><table><tr><th>started</th><th>type</th><th>status</th>'
            '<th>entities</th><th>kept</th><th>tavily</th><th>tokens</th></tr>'
            + body + "</table></div>")


def render(store: str, counts: dict, rows: list[dict], runs: list[dict], filters: dict) -> str:
    tiles = "".join(
        '<div class="tile"><b>{:,}</b><span>{}</span></div>'.format(counts.get(key, 0), label)
        for key, label in (("alerts", "findings"), ("unsent", "unsent"), ("ceo", "CEO-flagged"),
                           ("articles", "articles seen"), ("runs", "runs"))
    )
    found = ("".join(finding_html(r) for r in rows) if rows
             else '<div class="empty">No findings match these filters.</div>')

    return """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Intel history</title><style>{css}</style>
</head><body><div class="wrap">
  <h1>Intel history</h1>
  <p class="sub">Read-only view of {store}. Runs are started from the local runner, not here.</p>
  <div class="tiles">{tiles}</div>

  <form method="get">
    <select name="type">{types}</select>
    <select name="priority">{priorities}</select>
    <input name="entity" placeholder="entity name contains..." value="{entity}">
    <button type="submit">Filter</button>
  </form>

  <div class="card"><h2>Findings</h2>{found}</div>
  <div class="card"><h2>Recent runs</h2>{runs}</div>

  <p class="note">
    Read-only: this page runs SELECT statements and nothing else. Entity name, city and roster
    rank only &mdash; no rent, lease or address data exists in this store.
  </p>
</div></body></html>""".format(
        css=CSS,
        store=esc(store),
        tiles=tiles,
        types=options(REPORT_TYPES, filters["type"]),
        priorities=options(PRIORITIES, filters["priority"]),
        entity=esc(filters["entity"]),
        found=found,
        runs=runs_html(runs),
    )


class handler(BaseHTTPRequestHandler):  # noqa: N801 - the name the runtime looks for
    def do_GET(self):  # noqa: N802
        params = parse_qs(urlparse(self.path).query)

        def one(key: str) -> str:
            return (params.get(key) or [""])[0].strip()

        # Whitelisted rather than passed through: these reach a WHERE clause, and
        # the only values that mean anything are the ones the store can hold.
        filters = {
            "type": one("type") if one("type") in REPORT_TYPES else "",
            "priority": one("priority") if one("priority") in PRIORITIES else "",
            "entity": one("entity")[:80],
        }

        try:
            conn = connect()
        except Exception as exc:  # noqa: BLE001 - the message must not carry the DSN
            return self._send(500, "<h1>Store unavailable</h1><p>{}</p>".format(
                esc(type(exc).__name__)))

        try:
            store = urlparse(os.environ.get("DATABASE_URL", "")).hostname or "the history store"
            rows = findings(conn, report_type=filters["type"], priority=filters["priority"],
                            entity=filters["entity"])
            body = render(store, totals(conn), rows, recent_runs(conn), filters)
        except Exception as exc:  # noqa: BLE001
            body = "<h1>Query failed</h1><p>{}</p>".format(esc(type(exc).__name__))
            return self._send(500, body)
        finally:
            conn.close()

        self._send(200, body)

    def _send(self, code: int, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        # Real findings about real companies: never held by a shared cache.
        self.send_header("Cache-Control", "private, no-store")
        self.end_headers()
        self.wfile.write(payload)
