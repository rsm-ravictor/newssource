"""Render the digest email and its preview harness to static HTML.

No API key and no network are required - this reads a JSON fixture and writes
plain files, so the preview is always buildable and hostable.

    python build_preview.py                        # high + medium (shows both badges)
    python build_preview.py --priorities high      # production default per CONTEXT.md
    python build_preview.py --empty                # render the no-findings state
    python build_preview.py --open                 # build, then open in a browser

Outputs:
    docs/index.html    the preview harness (GitHub Pages entry point)
    docs/digest.html   the raw email, exactly as it would be sent
"""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

ROOT = Path(__file__).resolve().parent
TEMPLATES = ROOT / "templates"
DATA = ROOT / "data"
DOCS = ROOT / "docs"

PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}

FROM_NAME = "Tenant Intelligence"
FROM_ADDRESS = "tenant-intel@yourfirm.com"
TO_ADDRESS = "asset-management@yourfirm.com"


def load_fixture(path: Path) -> dict:
    """Read the alerts fixture, failing loudly on a malformed file."""
    if not path.exists():
        sys.exit(f"error: fixture not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"error: {path.name} is not valid JSON - {exc}")


def build_groups(fixture: dict, priorities: list[str]) -> list[dict]:
    """Filter to the selected priorities and sort for reading order.

    Tenants with the most urgent finding come first; within a tenant, findings
    are ordered high -> medium -> low. Tenants left with nothing drop out.
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


def build_context(fixture: dict, priorities: list[str], empty: bool) -> dict:
    groups = [] if empty else build_groups(fixture, priorities)
    total_alerts = sum(len(g["alerts"]) for g in groups)
    total_companies = len(groups)

    # Built by hand rather than with %-d / %#d, neither of which is cross-platform.
    run_date_raw = fixture.get("run_date", "")
    try:
        dt = datetime.strptime(run_date_raw, "%Y-%m-%d")
        run_date = f"{dt.strftime('%B')} {dt.day}, {dt.year}"
    except ValueError:
        run_date = run_date_raw or "today"

    if total_alerts:
        subject = f"Tenant intel: {total_alerts} development{'s' if total_alerts != 1 else ''} across {total_companies} tenant{'s' if total_companies != 1 else ''}"
        lead = groups[0]["alerts"][0]
        preheader = f"{groups[0]['display_name']} — {lead['headline']}"
    else:
        subject = "Tenant intel: no significant developments this week"
        preheader = f"{fixture.get('portfolio_size', 0)} tenants screened, nothing met the alert threshold."

    return {
        "run_date": run_date,
        "period_label": fixture.get("period_label", ""),
        "subject": subject,
        "preheader": preheader,
        "total_alerts": total_alerts,
        "total_companies": total_companies,
        "portfolio_size": fixture.get("portfolio_size", 0),
        "priority_label": priority_label(priorities),
        "alert_groups": groups,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--priorities",
        default="high,medium",
        help="comma-separated priorities to include (default: high,medium)",
    )
    ap.add_argument("--fixture", default=str(DATA / "sample_alerts.json"), help="path to the alerts JSON")
    ap.add_argument("--empty", action="store_true", help="render the no-findings state instead")
    ap.add_argument("--open", dest="open_browser", action="store_true", help="open the preview when done")
    args = ap.parse_args()

    priorities = [p.strip() for p in args.priorities.split(",") if p.strip()]
    unknown = [p for p in priorities if p not in PRIORITY_ORDER]
    if unknown:
        sys.exit(f"error: unknown priority {unknown} - choose from high, medium, low")

    fixture = load_fixture(Path(args.fixture))
    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=True,          # LLM-written summaries are untrusted text
        undefined=StrictUndefined,  # a missing placeholder fails the build, not the email
        trim_blocks=True,
        lstrip_blocks=False,
    )

    context = build_context(fixture, priorities, args.empty)
    email_html = env.get_template("digest.html").render(**context)

    built_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Imported lazily and defensively: the static build should never fail just
    # because the openai package (a connect.py dependency) isn't installed.
    try:
        from utils.connect import DEFAULT_MODEL
    except ImportError:
        DEFAULT_MODEL = "unavailable — install requirements.txt"

    preview_html = env.get_template("preview.html").render(
        subject=context["subject"],
        preheader=context["preheader"],
        from_name=FROM_NAME,
        from_address=FROM_ADDRESS,
        to_address=TO_ADDRESS,
        sent_label=f"{context['run_date']}, 8:02 AM",
        email_html=email_html,
        built_at=built_at,
        model_note=f"default model {DEFAULT_MODEL}",
        total_alerts=context["total_alerts"],
        total_companies=context["total_companies"],
    )

    DOCS.mkdir(exist_ok=True)
    (DOCS / "digest.html").write_text(email_html, encoding="utf-8")
    (DOCS / "index.html").write_text(preview_html, encoding="utf-8")
    # Keeps GitHub Pages from running the output through Jekyll.
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")

    print(f"built docs/index.html   ({len(preview_html):,} bytes)")
    print(f"built docs/digest.html  ({len(email_html):,} bytes)")
    print(f"  {context['total_alerts']} findings across {context['total_companies']} tenants "
          f"({args.priorities})")

    if args.open_browser:
        webbrowser.open((DOCS / "index.html").resolve().as_uri())

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
