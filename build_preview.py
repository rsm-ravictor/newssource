"""Render the digest emails and the static preview harness to docs/.

No API key and no network are required - this reads JSON fixtures and writes plain
files, so the published preview is always buildable and hostable.

    python build_preview.py                        # both report types, high + medium
    python build_preview.py --type competitors     # just one
    python build_preview.py --priorities high      # production default per CONTEXT.md
    python build_preview.py --empty                # render the no-findings state
    python build_preview.py --open                 # build, then open in a browser

For a LIVE run against Tavily + TritonAI with a clickable date range, use
`python serve.py` instead; this script only renders what is already in data/.

Outputs:
    docs/index.html              the preview harness (GitHub Pages entry point)
    docs/digest-tenants.html     raw emails, exactly as they would be sent
    docs/digest-competitors.html
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from render import (
    DOCS,
    PRIORITY_ORDER,
    ROOT,
    build_context,
    default_model_note,
    get_env,
    load_config,
    load_json,
    read_watchlist,
    render_digest,
    report_meta,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--type", default="all", help="report type to build: tenants, competitors, or all")
    ap.add_argument(
        "--priorities",
        default="high,medium",
        help="comma-separated priorities to include (default: high,medium)",
    )
    ap.add_argument("--empty", action="store_true", help="render the no-findings state instead")
    ap.add_argument("--open", dest="open_browser", action="store_true", help="open the preview when done")
    args = ap.parse_args()

    priorities = [p.strip() for p in args.priorities.split(",") if p.strip()]
    unknown = [p for p in priorities if p not in PRIORITY_ORDER]
    if unknown:
        sys.exit(f"error: unknown priority {unknown} - choose from high, medium, low")

    config = load_config()
    specs = config["report_types"]

    if args.type == "all":
        wanted = list(specs)
    elif args.type in specs:
        wanted = [args.type]
    else:
        sys.exit(f"error: unknown report type {args.type!r} - choose from {', '.join(specs)}, or all")

    env = get_env()
    DOCS.mkdir(exist_ok=True)
    reports = []

    for key in wanted:
        spec = specs[key]
        print(f"{spec['label']}:")
        fixture = load_json(ROOT / spec["alerts"])
        entities = fixture.get("companies", [])

        context = build_context(
            spec,
            config,
            entities=entities,
            priorities=priorities,
            # An alerts file is a self-describing snapshot, so it carries its own
            # monitored count (serve.py stamps the real watchlist size when it saves
            # a live run). The watchlist is only a fallback here; it is what drives
            # live runs, and it may legitimately differ from a fixture snapshot.
            monitored=fixture.get("portfolio_size") or len(read_watchlist(ROOT / spec["watchlist"])),
            run_date=fixture.get("run_date", ""),
            period_label=fixture.get("period_label", ""),
            empty=args.empty,
        )
        email_html = render_digest(env, context)

        raw_name = f"digest-{key}.html"
        (DOCS / raw_name).write_text(email_html, encoding="utf-8")
        print(
            f"  {context['total_alerts']} findings across {context['total_companies']} "
            f"{context['entity_noun_plural']} -> docs/{raw_name} ({len(email_html):,} bytes)"
        )
        reports.append(report_meta(key, spec, context, email_html, raw_name))

    preview_html = env.get_template("preview.html").render(
        reports=reports,
        default_type=reports[0]["key"],
        built_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        model_note=f"default model {default_model_note()}",
    )
    (DOCS / "index.html").write_text(preview_html, encoding="utf-8")
    # Keeps GitHub Pages from running the output through Jekyll.
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")
    print(f"\nbuilt docs/index.html ({len(preview_html):,} bytes) with {len(reports)} report type(s)")

    if args.open_browser:
        webbrowser.open((DOCS / "index.html").resolve().as_uri())

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
