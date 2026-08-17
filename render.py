"""Render stage - alerts data into digest HTML.

Shared by build_preview.py (fixtures -> docs/) and serve.py (live runs -> browser),
so a live run and a published snapshot go through exactly the same template and the
same context contract.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
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


def read_watchlist(path: Path) -> list[tuple[str, str]]:
    """(name, city) pairs from a watchlist file, ignoring comments and blanks."""
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, city = line.partition(",")
        entries.append((name.strip(), city.strip()))
    return entries


def get_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=True,             # LLM-written summaries are untrusted text
        undefined=StrictUndefined,   # a missing placeholder fails the build, not the email
        trim_blocks=True,
        lstrip_blocks=False,
    )


def build_groups(entities: list[dict], priorities: list[str]) -> list[dict]:
    """Filter to the selected priorities and sort for reading order.

    Entities with the most urgent finding come first; within an entity, findings
    are ordered high -> medium -> low. Entities left with nothing drop out.
    """
    groups = []
    for entity in entities:
        kept = [a for a in entity.get("alerts", []) if a.get("priority") in priorities]
        if not kept:
            continue
        kept.sort(key=lambda a: PRIORITY_ORDER.get(a.get("priority"), 9))
        groups.append(
            {
                "display_name": entity["display_name"],
                "city": entity.get("city", ""),
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


def format_run_date(raw: str) -> str:
    """Built by hand rather than with %-d / %#d, neither of which is cross-platform."""
    try:
        dt = datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return raw or "today"
    return f"{dt.strftime('%B')} {dt.day}, {dt.year}"


def build_context(
    spec: dict,
    shared: dict,
    *,
    entities: list[dict],
    priorities: list[str],
    monitored: int,
    run_date: str,
    period_label: str,
    empty: bool = False,
) -> dict:
    """Assemble the digest render context for one report type."""
    groups = [] if empty else build_groups(entities, priorities)
    total_alerts = sum(len(g["alerts"]) for g in groups)
    total_entities = len(groups)

    noun = spec["entity_noun"]
    if total_alerts:
        subject = (
            f"{spec['subject_prefix']}: {total_alerts} {plural(total_alerts, 'development')} "
            f"across {total_entities} {plural(total_entities, noun)}"
        )
        lead = groups[0]["alerts"][0]
        preheader = f"{groups[0]['display_name']} — {lead['headline']}"
    else:
        subject = f"{spec['subject_prefix']}: no significant developments this period"
        preheader = f"{monitored} {spec['entity_noun_plural']} screened, nothing met the alert threshold."

    return {
        "brandmark": spec["brandmark"],
        "title": spec["title"],
        "run_date": format_run_date(run_date),
        "period_label": period_label,
        "subject": subject,
        "preheader": preheader,
        "total_alerts": total_alerts,
        "total_companies": total_entities,
        "portfolio_size": monitored,
        "affected_label": spec["affected_label"],
        "monitored_label": spec["monitored_label"],
        "entity_noun_plural": spec["entity_noun_plural"],
        "privacy_note": spec["privacy_note"],
        "priority_label": priority_label(priorities),
        "categories": spec["categories"],
        "priorities": shared["priorities"],
        "alert_groups": groups,
    }


def render_digest(env: Environment, context: dict) -> str:
    return env.get_template("digest.html").render(**context)


def report_meta(key: str, spec: dict, context: dict, email_html: str, raw_name: str) -> dict:
    """The per-report bundle both the static preview and the live runner display."""
    return {
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


def default_model_note() -> str:
    """Imported defensively: a static build must not fail on a missing openai dep."""
    try:
        from utils.connect import DEFAULT_MODEL

        return DEFAULT_MODEL
    except ImportError:
        return "unavailable - install requirements.txt"
