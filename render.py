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
from urllib.parse import urlparse

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

# Roster parsing. Re-exported: callers import both from here.
from watchlist import read_entries, read_watchlist  # noqa: F401

ROOT = Path(__file__).resolve().parent
TEMPLATES = ROOT / "templates"
DOCS = ROOT / "docs"
CONFIG = ROOT / "config" / "report_types.yaml"

PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}

# Sorts an unranked entity after every ranked one instead of ahead of rank 1.
UNRANKED = 10**6

# Where a source lands when config/report_types.yaml has no source_tiers block at
# all: one flat tier, so ordering falls back to exactly the pre-tier behaviour.
FLAT_TIER = 0


def source_tiers(config: dict) -> tuple[dict[str, int], int]:
    """Flatten the config block into {domain: position} plus the unlisted position.

    Position is 1-based and taken from the order the tiers are written in, so the
    config reads top-to-bottom as best-to-worst with no separate rank field to keep
    in sync.
    """
    block = config.get("source_tiers") or {}
    lookup: dict[str, int] = {}
    for i, tier in enumerate(block.get("order") or [], start=1):
        for domain in tier.get("domains") or []:
            lookup[domain.strip().lower().lstrip(".")] = i
    return lookup, int(block.get("default_position", FLAT_TIER))


def source_tier(url: str, lookup: dict[str, int], default: int) -> int:
    """Tier position for one article's URL.

    Matches the host or any parent of it, so m.facebook.com and web.facebook.com
    both resolve to facebook.com without every subdomain being listed. The longest
    match wins, so a specific subdomain can be tiered apart from its parent.
    """
    if not lookup:
        return FLAT_TIER
    host = urlparse(url or "").netloc.lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    best = None
    for domain, pos in lookup.items():
        if host == domain or host.endswith("." + domain):
            if best is None or len(domain) > len(best[0]):
                best = (domain, pos)
    return best[1] if best else default


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
    groups: list[dict], *, cap_per_entity: int = 2, limit: int = 5, tiers=None
) -> list[dict]:
    """The 'Top Intel' rows: at most N per entity, most severe first, capped.

    Capping per entity first stops one noisy company from filling the whole box.
    The sort is stable, so entities keep their reading order within a severity.
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
    return rows[:limit]


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
        kept = [a for a in entity.get("alerts", []) if a.get("priority") in priorities]
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
        "top_intel": build_top_intel(groups, tiers=tiers),
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
