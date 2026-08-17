"""Render the digest emails and the preview harness to static HTML.

No API key and no network are required - this reads JSON fixtures and writes plain
files, so the preview is always buildable and hostable.

    python build_preview.py                        # both report types, high + medium
    python build_preview.py --type competitors     # just one
    python build_preview.py --priorities high      # production default per CONTEXT.md
    python build_preview.py --empty                # render the no-findings state
    python build_preview.py --open                 # build, then open in a browser

Everything type-specific (categories, criteria, wording) comes from
config/report_types.yaml, so neither this script nor the email template hardcodes
the tenants or competitors taxonomy.

Outputs:
    docs/index.html              the preview harness (GitHub Pages entry point)
    docs/digest-tenants.html     raw emails, exactly as they would be sent
    docs/digest-competitors.html
"""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

ROOT = Path(__file__).resolve().parent
TEMPLATES = ROOT / "templates"
DOCS = ROOT / "docs"
CONFIG = ROOT / "config" / "report_types.yaml"

PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def load_config() -> dict:
    if not CONFIG.exists():
        sys.exit(f"error: config not found: {CONFIG}")
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def load_json(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"error: fixture not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"error: {path.name} is not valid JSON - {exc}")


def read_watchlist(path: Path) -> list[str]:
    """Names from a watchlist file, ignoring comments and blank lines.

    Accepts "Name" or "Name, City" and keeps only the name.
    """
    if not path.exists():
        return []
    names = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        names.append(line.split(",")[0].strip())
    return names


def build_groups(fixture: dict, priorities: list[str]) -> list[dict]:
    """Filter to the selected priorities and sort for reading order.

    Entities with the most urgent finding come first; within an entity, findings
    are ordered high -> medium -> low. Entities left with nothing drop out.
    """
    groups = []
    for company in fixture.get("companies", []):
        kept = [a for a in company.get("alerts", []) if a.get("priority") in priorities]
        if not kept:
            continue
        kept.sort(key=lambda a: PRIORITY_ORDER.get(a.get("priority"), 9))
        groups.append(
            {
                "display_name": company["display_name"],
                "city": company.get("city", ""),
                "alerts": kept,
            }
        )
    groups.sort(
        key=lambda g: (
            PRIORITY_ORDER.get(g["alerts"][0].get("priority"), 9),
            -len(g["alerts"]),
            g["display_name"],
        )
    )
    return groups


def priority_label(priorities: list[str]) -> str:
    """Human phrasing for the footer/threshold line."""
    if priorities == ["high"]:
        return "high priority only"
    if len(priorities) == 1:
        return f"{priorities[0]} priority"
    return f"{', '.join(priorities[:-1])} and {priorities[-1]} priority"


def plural(n: int, noun: str) -> str:
    return noun if n == 1 else noun + "s"


def build_context(spec: dict, shared: dict, priorities: list[str], empty: bool) -> dict:
    """Assemble the render context for one report type."""
    fixture = load_json(ROOT / spec["alerts"])
    watchlist = read_watchlist(ROOT / spec["watchlist"])

    groups = [] if empty else build_groups(fixture, priorities)
    total_alerts = sum(len(g["alerts"]) for g in groups)
    total_companies = len(groups)

    # The watchlist is the source of truth for "how many are monitored"; the
    # fixture's own count is only a fallback for a missing list.
    portfolio_size = len(watchlist) or fixture.get("portfolio_size", 0)

    if watchlist:
        unlisted = [g["display_name"] for g in groups if g["display_name"] not in watchlist]
        if unlisted:
            print(f"  warning: not on {Path(spec['watchlist']).name}: {', '.join(unlisted)}", file=sys.stderr)

    # Built by hand rather than with %-d / %#d, neither of which is cross-platform.
    run_date_raw = fixture.get("run_date", "")
    try:
        dt = datetime.strptime(run_date_raw, "%Y-%m-%d")
        run_date = f"{dt.strftime('%B')} {dt.day}, {dt.year}"
    except ValueError:
        run_date = run_date_raw or "today"

    noun = spec["entity_noun"]
    if total_alerts:
        subject = (
            f"{spec['subject_prefix']}: {total_alerts} {plural(total_alerts, 'development')} "
            f"across {total_companies} {plural(total_companies, noun)}"
        )
        lead = groups[0]["alerts"][0]
        preheader = f"{groups[0]['display_name']} — {lead['headline']}"
    else:
        subject = f"{spec['subject_prefix']}: no significant developments this week"
        preheader = f"{portfolio_size} {spec['entity_noun_plural']} screened, nothing met the alert threshold."

    return {
        "brandmark": spec["brandmark"],
        "title": spec["title"],
        "run_date": run_date,
        "period_label": fixture.get("period_label", ""),
        "subject": subject,
        "preheader": preheader,
        "total_alerts": total_alerts,
        "total_companies": total_companies,
        "portfolio_size": portfolio_size,
        "affected_label": spec["affected_label"],
        "monitored_label": spec["monitored_label"],
        "entity_noun_plural": spec["entity_noun_plural"],
        "privacy_note": spec["privacy_note"],
        "priority_label": priority_label(priorities),
        "categories": spec["categories"],
        "priorities": shared["priorities"],
        "alert_groups": groups,
    }


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

    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=True,             # LLM-written summaries are untrusted text
        undefined=StrictUndefined,   # a missing placeholder fails the build, not the email
        trim_blocks=True,
        lstrip_blocks=False,
    )
    digest_tpl = env.get_template("digest.html")

    DOCS.mkdir(exist_ok=True)
    reports = []
    for key in wanted:
        spec = specs[key]
        print(f"{spec['label']}:")
        context = build_context(spec, config, priorities, args.empty)
        email_html = digest_tpl.render(**context)

        raw_name = f"digest-{key}.html"
        (DOCS / raw_name).write_text(email_html, encoding="utf-8")
        print(
            f"  {context['total_alerts']} findings across {context['total_companies']} "
            f"{context['entity_noun_plural']} -> docs/{raw_name} ({len(email_html):,} bytes)"
        )

        reports.append(
            {
                "key": key,
                "label": spec["label"],
                "subject": context["subject"],
                "preheader": context["preheader"],
                "from_name": spec["from_name"],
                "from_address": spec["from_address"],
                "to_address": spec["to_address"],
                "sent_label": f"{context['run_date']}, 8:02 AM",
                "email_html": email_html,
                "raw_name": raw_name,
                "total_alerts": context["total_alerts"],
                "total_companies": context["total_companies"],
                "entity_noun_plural": context["entity_noun_plural"],
                "initials": "".join(w[0] for w in spec["brandmark"].split()[:2]).upper(),
            }
        )

    # Imported lazily and defensively: the static build should never fail just
    # because the openai package (a connect.py dependency) isn't installed.
    try:
        from utils.connect import DEFAULT_MODEL
    except ImportError:
        DEFAULT_MODEL = "unavailable - install requirements.txt"

    preview_html = env.get_template("preview.html").render(
        reports=reports,
        default_type=reports[0]["key"],
        built_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        model_note=f"default model {DEFAULT_MODEL}",
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
