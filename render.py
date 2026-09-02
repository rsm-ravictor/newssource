"""Render stage - alerts data into digest HTML.

Shared by build_preview.py (fixtures -> docs/) and serve.py (live runs -> browser),
so a live run and a published snapshot go through exactly the same template and the
same context contract.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

# Source policy lives in sources.py so the judge can gate citations without
# importing the renderer. Re-exported here: this is where callers and tests
# have always found it.
from sources import FLAT_TIER, source_tier, source_tiers  # noqa: F401

# Roster parsing. Re-exported: callers import both from here.
from watchlist import read_entries, read_watchlist  # noqa: F401

ROOT = Path(__file__).resolve().parent
TEMPLATES = ROOT / "templates"
DOCS = ROOT / "docs"
CONFIG = ROOT / "config" / "report_types.yaml"

PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}

# Sorts an unranked entity after every ranked one instead of ahead of rank 1.
UNRANKED = 10**6


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


def get_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=True,             # LLM-written summaries are untrusted text
        undefined=StrictUndefined,   # a missing placeholder fails the build, not the email
        trim_blocks=True,
        lstrip_blocks=False,
    )


def slugify(name: str) -> str:
    """Anchor id for an entity: lowercase, non-alphanumeric runs become hyphens.

    The same function feeds the index pills and the detail-section ids, so the two
    are guaranteed to match.
    """
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "entity"


def build_top_intel(
    groups: list[dict],
    *,
    cap_per_entity: int = 2,
    limit: int = 5,
    tiers=None,
    reserve_beyond_rank: int | None = None,
    reserve_slots: int = 0,
) -> list[dict]:
    """The 'Top Intel' rows: at most N per entity, most severe first, capped.

    Capping per entity first stops one noisy company from filling the whole box.
    The sort is stable, so entities keep their reading order within a severity.

    ``reserve_beyond_rank`` fixes the failure mode that rank ordering creates: sort
    strictly by severity then rank and the box fills with the biggest names every
    week, so a real event at tenant #430 is never read. When set, up to
    ``reserve_slots`` of the box are held for entities ranked worse than that
    threshold, filled with their most severe findings. Reserved rows are chosen by
    the same sort as everything else - this changes who is *eligible* for the last
    seats, never the standard a finding has to meet. Nothing is padded: if no
    lower-ranked entity has a finding, the seats go back to the general pool.
    """
    lookup, default = tiers or ({}, FLAT_TIER)
    rows = []
    for group in groups:
        for alert in group["alerts"][:cap_per_entity]:
            rows.append({
                **alert,
                "company": group["display_name"],
                "slug": group["slug"],
                "rank": group.get("rank"),
            })
    # Severity first, then roster rank, then source tier: two urgent findings are
    # ordered by how much the entity matters, and a tie between them is settled by
    # which outlet carried it. Unranked report types fall back to the old stable
    # order, and with no source_tiers configured every row shares FLAT_TIER.
    rows.sort(key=lambda r: (
        PRIORITY_ORDER.get(r.get("priority"), 9),
        r.get("rank") or UNRANKED,
        source_tier(r.get("source_url", ""), lookup, default),
    ))

    if not reserve_beyond_rank or reserve_slots <= 0:
        return rows[:limit]

    def is_smaller(row: dict) -> bool:
        rank = row.get("rank")
        return bool(rank) and rank > reserve_beyond_rank

    head = rows[:limit]
    held = min(reserve_slots, limit)
    # Only intervene if the natural top N has fewer lower-ranked rows than the
    # reservation asks for, and there are some to promote.
    missing = held - sum(1 for r in head if is_smaller(r))
    if missing <= 0:
        return head
    promotions = [r for r in rows[limit:] if is_smaller(r)][:missing]
    if not promotions:
        return head
    # Drop the weakest large-tenant rows to make room, keeping the most severe.
    keep = [r for r in head if is_smaller(r)] + [r for r in head if not is_smaller(r)][
        : limit - len(promotions) - sum(1 for r in head if is_smaller(r))
    ]
    merged = keep + promotions
    merged.sort(key=lambda r: (
        PRIORITY_ORDER.get(r.get("priority"), 9),
        r.get("rank") or UNRANKED,
        source_tier(r.get("source_url", ""), lookup, default),
    ))
    return merged[:limit]


# Fields added by later pipeline stages that older alert fixtures and snapshots do
# not carry. Defaulted here rather than guarded in the template, so StrictUndefined
# keeps catching real typos instead of being softened to a .get() everywhere.
OPTIONAL_ALERT_FIELDS = {
    "event_date": "",
    "event_date_basis": "",
    "event_key": "",
    "deal_metrics": "",
    "corroborations": 0,
}


def normalize_alert(alert: dict) -> dict:
    """One alert with every optional field present, so any snapshot renders."""
    return {**OPTIONAL_ALERT_FIELDS, **alert}


def build_groups(entities: list[dict], priorities: list[str], *, tiers=None) -> list[dict]:
    """Filter to the selected priorities and sort for reading order.

    Entities with the most urgent finding come first; within an entity, findings
    are ordered high -> medium -> low, and findings of equal severity are ordered
    by source tier so the reliable outlet leads. Entities left with nothing drop
    out. Nothing is ever dropped for its source - tier only orders.
    """
    lookup, default = tiers or ({}, FLAT_TIER)
    groups = []
    for entity in entities:
        kept = [
            normalize_alert(a)
            for a in entity.get("alerts", [])
            if a.get("priority") in priorities
        ]
        if not kept:
            continue
        kept.sort(key=lambda a: (
            PRIORITY_ORDER.get(a.get("priority"), 9),
            source_tier(a.get("source_url", ""), lookup, default),
        ))
        groups.append(
            {
                "display_name": entity["display_name"],
                "city": entity.get("city", ""),
                "rank": entity.get("rank"),
                "segment": entity.get("segment", ""),
                "slug": slugify(entity["display_name"]),
                "alerts": kept,
            }
        )
    # Roster rank outranks finding count: on the tenant side a single urgent item at
    # the #3 tenant leads the briefing over three items at #400. Unranked entities
    # (the competitors side) all share UNRANKED, so their order is unchanged.
    groups.sort(
        key=lambda g: (
            PRIORITY_ORDER.get(g["alerts"][0].get("priority"), 9),
            g["rank"] or UNRANKED,
            # Lead finding's tier: on the unranked competitors side this is what
            # puts the firm whose top item came from a wire ahead of the one whose
            # top item came from a social post.
            source_tier(g["alerts"][0].get("source_url", ""), lookup, default),
            -len(g["alerts"]),
            g["display_name"],
        )
    )
    return disambiguate(groups)


def disambiguate(groups: list[dict]) -> list[dict]:
    """Make each group's anchor and index label unique.

    The competitor roster lists one firm per market, so Boston Properties can be
    both a San Francisco and a Bellevue entry - two legitimate groups sharing a
    name. Left alone they would emit the same element id twice and both index
    pills would scroll to the first one. Repeated names get the city folded into
    the slug and appended to the pill; unique names are untouched, which is the
    normal case.
    """
    counts: dict[str, int] = {}
    for group in groups:
        counts[group["display_name"]] = counts.get(group["display_name"], 0) + 1

    used: set[str] = set()
    for group in groups:
        group["pill_label"] = group["display_name"]
        if counts[group["display_name"]] > 1:
            if group["city"]:
                group["pill_label"] = f"{group['display_name']} · {group['city']}"
            group["slug"] = slugify(f"{group['display_name']} {group['city']}")
        # Two entries in the same city, or a city-less duplicate, still need
        # distinct ids, so the last resort is a counter.
        slug, n = group["slug"], 2
        while slug in used:
            slug, n = f"{group['slug']}-{n}", n + 1
        group["slug"] = slug
        used.add(slug)
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
    # Derived from `shared` rather than passed in: every caller already hands over
    # the whole config, so the tier list cannot drift out of sync with the run.
    tiers = source_tiers(shared)
    groups = [] if empty else build_groups(entities, priorities, tiers=tiers)
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
        "briefing_name": shared.get("briefing_name", "Intel Briefing"),
        "brandmark": spec["brandmark"],
        "title": spec["title"],
        "run_date": format_run_date(run_date),
        # Top Intel box + the ticker/index need these derived views.
        "top_intel": build_top_intel(
            groups,
            tiers=tiers,
            # Only a ranked report type can reserve by rank; the competitors side
            # has no rank column, so the block is simply absent from its config.
            reserve_beyond_rank=(spec.get("top_intel_reserve") or {}).get("beyond_rank"),
            reserve_slots=(spec.get("top_intel_reserve") or {}).get("slots", 0),
        ),
        "generated_at": datetime.now().strftime("%b %d, %Y at %I:%M %p").replace(" 0", " "),
        # The ticker scrolls a duplicated list; 33px per row, 4 rows visible.
        "ticker_row_px": 33,
        "ticker_window_px": 132,
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
