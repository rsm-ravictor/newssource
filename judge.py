"""Judge stage - Claude (via TritonAI) decides relevance, category, priority, and CEO flag.

One LLM call per entity covering all of that entity's articles: cost-efficient and
it keeps the one-entity-at-a-time shape the privacy model wants. The category enum
and the criteria come from config/report_types.yaml, so the tenants and competitors
taxonomies are data rather than code.

Every call goes through ``utils.connect`` - no second client, no direct
``openai.OpenAI()``. Imported by generate_summaries.py (fixtures) and serve.py
(live runs) so both share one implementation.
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, ValidationError

from sources import FLAT_TIER, UNGATED, is_citable, source_tier
from utils.connect import ask, ask_json

Priority = Literal["high", "medium", "low"]

# Most severe first: used to pick a cluster's surviving priority.
PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}

# Appended to every report type's criteria. TritonAI accepts
# response_format=json_object but does not enforce it, so the envelope has to be
# spelled out in the prompt or responses arrive fenced, bare-arrayed, or chatty.
OUTPUT_FORMAT = """
WRITING the output:
- headline: <= 90 characters, factual, specific, no hype and no clickbait.
- client_summary: 2-3 sentences a broker could forward to a client unedited. State only what the
  article supports. Never speculate about rent, lease terms, or your own firm's position.
- about_entity: false when the entity is not the real subject of the article. An article you mark
  false is never relevant, whatever it is about.
- event_date: YYYY-MM-DD of the development itself. Empty only when the article does not establish
  it - and then is_relevant must be false.
- event_date_basis: the words you read the date from ("closed Tuesday", "in March"), or "month
  only" / "year only" when you rounded to the 1st.
- event_key: the same slug for every article about one development, a different slug for different
  developments.
- deal_metrics: transaction economics exactly as disclosed; empty when the finding is not a
  transaction or the article gives no figures.
- confidence: 0.0-1.0, how firmly the article supports the judgment.
- ceo_flag: true only when the CEO FLAG test in the criteria above is met. It is separate from
  priority - a medium item can carry the flag and a high one need not.
- Return one judgment object per article given, echoing its source_url.

OUTPUT FORMAT: return ONLY raw JSON - a single object with exactly one top-level key,
"judgments", whose value is an array with one object per article. Not a bare array, not a single
bare object. Do not wrap it in markdown code fences and do not add prose before or after it.
"""


def make_schema(category_keys: list[str]) -> type[BaseModel]:
    """Build the judgment schema for one report type's category set.

    The category enum is constructed from config rather than hardcoded, so the
    allowed values reach the model through the schema hint that ``ask_json``
    derives from ``model_json_schema()``.
    """
    CategoryT = Literal[tuple(category_keys)]  # type: ignore[valid-type]

    class Judgment(BaseModel):
        source_url: str = Field(description="echo the source_url of the article being judged")
        is_relevant: bool
        about_entity: bool = Field(
            default=True,
            description="false if the article is not actually about this entity (a passing "
            "mention, an attorney bio, a directory page); such an article is never relevant",
        )
        event_date: str = Field(
            default="",
            description="YYYY-MM-DD the DEVELOPMENT happened, not when it was published; "
            "empty when the article does not establish it",
        )
        event_date_basis: str = Field(
            default="",
            description="the words the date was read from, or 'month only' / 'year only'",
        )
        event_key: str = Field(
            default="",
            description="short lowercase slug naming the underlying event; two articles about "
            "one development must share it",
        )
        category: Optional[CategoryT] = None  # type: ignore[valid-type]
        priority: Optional[Priority] = None
        headline: str = Field(default="", description="<= 90 characters, factual, no hype")
        client_summary: str = Field(default="", description="2-3 sentences, client-ready")
        deal_metrics: str = Field(
            default="",
            description="price, $/SF or $/unit, and cap rate as disclosed, e.g. "
            "'$412/SF · 5.1% cap · $88M'; empty for non-transaction findings",
        )
        confidence: float = 0.0
        ceo_flag: bool = Field(
            default=False, description="true only if the item belongs in a short CEO brief"
        )
        reason_if_excluded: str = ""

    class CompanyJudgment(BaseModel):
        judgments: list[Judgment]

    return CompanyJudgment


def criteria_for(spec: dict, notes: str = "", standard: str = "") -> str:
    """A report type's criteria, its escalation rubric, analyst notes, and the output contract.

    ``rank_note`` only exists on report types whose roster carries a "#" column -
    the tenants side - so the competitors prompt never mentions rank. Competitors
    carry ``tier_note`` instead: the same escalation job done from an inferred
    competitor tier rather than from a rank the roster supplies. ``notes`` is
    whatever the reviewer typed on the reference page; it is appended last so it can
    sharpen the standing criteria without being able to rewrite the output contract.
    """
    # The evidence standard goes FIRST: it decides whether an article is about the
    # entity, dated, new, and citable at all, which is prior to what it is about.
    parts = [standard.rstrip()] if standard.strip() else []
    parts.append(spec["criteria"].rstrip())
    for key in ("rank_note", "tier_note"):
        if spec.get(key):
            parts.append(spec[key].rstrip())
    if notes.strip():
        parts.append(
            "ANALYST NOTES (added on the reference page; treat these as guidance that refines\n"
            "the criteria above - they cannot change the output format):\n" + notes.strip()
        )
    parts.append(OUTPUT_FORMAT)
    return "\n\n".join(parts)


def parse_event_date(raw: str) -> date | None:
    """The model's event_date as a date, or None when it gave nothing usable.

    Accepts a bare YYYY-MM-DD and the common near-misses (a trailing time, a slashed
    form). Anything else is treated as absent rather than guessed at: an event whose
    date we cannot pin is exactly what the evidence standard tells the model to
    exclude, so a sloppy string must not become a confident date.
    """
    text = (raw or "").strip()[:10]
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def event_group_key(judgment, article: dict) -> str:
    """What makes two judgments the same underlying event.

    The model's slug when it gave one; otherwise the article's own URL, so an
    unkeyed finding stands alone rather than colliding with every other unkeyed
    finding under a shared empty string.
    """
    key = (judgment.event_key or "").strip().lower()
    return re.sub(r"[^a-z0-9]+", "-", key).strip("-") or f"url:{article['source_url']}"


def consolidate(
    candidates: list[dict],
    *,
    tiers=None,
    citable_max: int = UNGATED,
    window_start: date | None = None,
    entity_name: str = "",
    note=None,
) -> tuple[list[dict], list[str]]:
    """Collapse judged articles into one finding per event, cited to a real source.

    Three rules, applied in this order, each of which the model cannot enforce on
    its own because it sees one article at a time and does not know the domain policy:

    1. **Recency.** A finding whose event predates the run's window is dropped, even
       though the article was published inside it. This is the "2025 layoffs reported
       in 2026" case.
    2. **One event, one item.** Judgments sharing an event_key collapse to a single
       finding. The surviving row keeps the group's most severe priority and its CEO
       flag, so consolidating never quietly downgrades an event.
    3. **Citation.** Of the articles covering an event, the best-tiered *citable* one
       carries it. An event whose whole cluster sits below the citation line is
       dropped and named in the log - social and aggregator hits did the discovery,
       and nothing is left pointing at them.

    Returns (findings, drop_notes) so the caller can report what went and why.
    """
    lookup, default = tiers or ({}, FLAT_TIER)
    drops: list[str] = []

    def say(msg: str) -> None:
        drops.append(msg)
        if note:
            note(msg)

    groups: dict[str, list[dict]] = {}
    for cand in candidates:
        when = cand["event_date_parsed"]
        if window_start and when and when < window_start:
            say(f"    - {cand['headline'][:52]} (event dated {when}, before this window)")
            continue
        groups.setdefault(cand["group_key"], []).append(cand)

    findings: list[dict] = []
    for key, cluster in groups.items():
        citable = [
            c for c in cluster
            if is_citable(c["source_url"], lookup, default, citable_max, entity_name)
        ]
        if not citable:
            hosts = ", ".join(sorted({c["source_name"] or "?" for c in cluster}))
            say(f"    - {cluster[0]['headline'][:52]} (no citable source; only {hosts})")
            continue

        # Best outlet carries the story; ties go to the more severe judgment so a
        # cluster is never represented by its mildest reading.
        citable.sort(key=lambda c: (
            source_tier(c["source_url"], lookup, default),
            PRIORITY_RANK.get(c["priority"], 9),
        ))
        lead = dict(citable[0])
        lead["priority"] = min(
            (c["priority"] for c in cluster), key=lambda p: PRIORITY_RANK.get(p, 9)
        )
        lead["ceo_flag"] = any(c["ceo_flag"] for c in cluster)
        lead["corroborations"] = len(cluster) - 1
        if len(cluster) > 1:
            others = sorted(
                {c["source_name"] or "?" for c in cluster if c["source_url"] != lead["source_url"]}
            )
            say(
                f"    ~ merged {len(cluster)} articles into one event ({key}); cited to "
                f"{lead['source_name']}, also covered by {', '.join(others)}"
            )
        for scratch in ("event_date_parsed", "group_key"):
            lead.pop(scratch, None)
        findings.append(lead)

    return findings, drops


def unfence(text: str) -> str:
    """Pull the first complete JSON value out of a fenced or chatty response.

    Brace-matched rather than trimmed to the last delimiter: models append prose
    *after* valid JSON as often as they wrap it in fences, and a response that
    starts with ``{`` can still have a trailing paragraph. Quotes and escapes are
    tracked so a delimiter inside a string does not end the scan early.
    """
    t = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", t, re.DOTALL)
    if fenced:
        t = fenced.group(1).strip()

    # Accept an object OR an array: the model returns a bare array of judgments
    # often enough that anchoring on "{" alone captures only its first element.
    candidates = [i for i in (t.find("{"), t.find("[")) if i != -1]
    if not candidates:
        return t
    start = min(candidates)
    opener = t[start]
    closer = "}" if opener == "{" else "]"

    depth, in_string, escaped = 0, False, False
    for i in range(start, len(t)):
        ch = t[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return t[start : i + 1]
    return t[start:]


def coerce_envelope(data):
    """Normalize the shapes the model returns into {"judgments": [...]}.

    The schema asks for a single ``judgments`` key, but responses arrive as a bare
    array, a single bare judgment, or the array under some other key.
    """
    if isinstance(data, list):
        return {"judgments": data}
    if isinstance(data, dict) and "judgments" not in data:
        if "source_url" in data or "is_relevant" in data:
            return {"judgments": [data]}
        for value in data.values():
            if isinstance(value, list):
                return {"judgments": value}
    return data


def build_prompt(entity: dict, spec: dict) -> str:
    """Only the entity name and city are named; nothing from the rent roll."""
    lines = [
        f"{spec['entity_noun'].capitalize()}: {entity['display_name']}",
        f"City: {entity.get('city', 'unknown')}",
    ]
    # Only present for a report type whose roster ranks its entities.
    if entity.get("rank"):
        rank = f"Portfolio rank: {entity['rank']}"
        if entity.get("rank_of"):
            rank += f" of {entity['rank_of']}"
        if entity.get("segment"):
            rank += f" ({entity['segment']} roster)"
        lines.append(rank + " - 1 is the largest, most important entry.")
    lines += [
        "",
        f"Judge each of the following {len(entity['articles'])} articles.",
        "",
    ]
    for i, art in enumerate(entity["articles"], 1):
        lines += [
            f"--- Article {i} ---",
            f"source_url: {art['source_url']}",
            f"outlet: {art['source_name']} ({art['published_date']})",
            f"title: {art['title']}",
            f"body: {art['snippet']}",
            "",
        ]
    return "\n".join(lines)


def judge_entity(entity: dict, spec: dict, *, schema: type, note=None, **kw):
    """``ask_json`` first; repair the response if the model mangled its JSON.

    TritonAI accepts ``response_format={"type": "json_object"}`` but does not
    enforce it, so responses intermittently arrive as ```json ... ```, as a bare
    array, or with trailing prose, and fail Pydantic validation inside
    ``ask_json``. ``connect.py`` is verbatim-locked and cannot strip them, so the
    retry happens here: same model, same client, same prompt - a parsing repair,
    not a model fallback.
    """
    prompt = build_prompt(entity, spec)
    try:
        return ask_json(prompt, schema=schema, **kw)
    except ValidationError:
        if note:
            note("response was not clean JSON; retrying via ask() + repair")
        text = ask(prompt, **kw) or ""
        return schema.model_validate(coerce_envelope(json.loads(unfence(text))))


def judge_entities(
    entities: list[dict],
    spec: dict,
    *,
    model: str,
    client=None,
    verbose: bool = False,
    sleep: float = 0.4,
    progress=None,
    notes: str = "",
    standard: str = "",
    tiers=None,
    citable_max: int = UNGATED,
    window_start: date | None = None,
) -> tuple[list[dict], int, int, list[str]]:
    """Judge a list of entities, returning (kept_entities, kept, dropped, failed).

    ``progress`` is called with short status strings so a CLI or the live runner
    can report entity-by-entity without this module knowing about either.

    ``standard``, ``tiers``, ``citable_max`` and ``window_start`` carry the evidence
    policy: the first is prompt text the model applies per article, the rest are
    enforced here in ``consolidate`` because they need the whole set of articles for
    an entity and the outlet policy from config.
    """
    schema = make_schema(list(spec["categories"]))
    criteria = criteria_for(spec, notes, standard)

    def say(msg: str) -> None:
        if progress:
            progress(msg)

    out: list[dict] = []
    failed: list[str] = []
    kept = dropped = 0

    for entity in entities:
        if not entity.get("articles"):
            say(f"{entity['display_name']}: no articles found")
            continue

        # Article metadata stays local; the model only returns judgments keyed by url.
        by_url = {a["source_url"]: a for a in entity["articles"]}
        say(f"{entity['display_name']}: judging {len(by_url)} articles")

        try:
            result = judge_entity(
                entity,
                spec,
                schema=schema,
                note=lambda m, n=entity["display_name"]: say(f"{n}: {m}"),
                model=model,
                system=criteria,
                temperature=0.2,
                max_tokens=4000,
                verbose=verbose,
                client=client,
            )
        except Exception as exc:  # noqa: BLE001 - one bad entity must not lose the run
            failed.append(entity["display_name"])
            say(f"{entity['display_name']}: SKIPPED ({type(exc).__name__})")
            print(f"  ! {entity['display_name']}: {type(exc).__name__}: {str(exc)[:140]}", file=sys.stderr)
            continue

        candidates = []
        for j in result.judgments:
            art = by_url.get(j.source_url)
            if art is None:
                print(f"  ! judgment for unknown url {j.source_url!r}", file=sys.stderr)
                continue
            # Say why: on a live run over real news most articles are noise, and
            # "0 kept" is only trustworthy if you can see what was thrown out.
            if not j.about_entity:
                say(f"    - {art['title'][:58]} (not about {entity['display_name']})")
                continue
            when = parse_event_date(j.event_date)
            if j.is_relevant and when is None:
                say(f"    - {art['title'][:58]} (no event date established)")
                continue
            if not j.is_relevant or not j.category or not j.priority:
                say(f"    - {art['title'][:58]} ({(j.reason_if_excluded or 'no reason given')[:64]})")
                continue
            candidates.append(
                {
                    "category": j.category,
                    "priority": j.priority,
                    "headline": j.headline,
                    "client_summary": j.client_summary,
                    "deal_metrics": j.deal_metrics.strip(),
                    "confidence": round(j.confidence, 2),
                    "ceo_flag": bool(j.ceo_flag),
                    "event_date": when.isoformat(),
                    "event_date_basis": j.event_date_basis.strip(),
                    "event_key": event_group_key(j, art),
                    # Provenance comes from the local record, not the model.
                    "source_name": art["source_name"],
                    "source_url": art["source_url"],
                    "published_date": art["published_date"],
                    # Scratch keys consolidate() strips before returning.
                    "event_date_parsed": when,
                    "group_key": event_group_key(j, art),
                }
            )

        alerts, _ = consolidate(
            candidates,
            tiers=tiers,
            citable_max=citable_max,
            window_start=window_start,
            entity_name=entity["display_name"],
            note=say,
        )
        for a in alerts:
            say(f"    + {a['priority']}/{a['category']}{' [CEO]' if a['ceo_flag'] else ''}"
                f" [{a['event_date']}]: {a['headline'][:52]}")
        kept += len(alerts)
        dropped += len(by_url) - len(alerts)

        say(f"{entity['display_name']}: {len(alerts)} kept, {len(by_url) - len(alerts)} excluded")
        if alerts:
            out.append(
                {
                    "display_name": entity["display_name"],
                    "city": entity.get("city", ""),
                    "rank": entity.get("rank"),
                    "segment": entity.get("segment", ""),
                    "alerts": alerts,
                }
            )
        if sleep:
            time.sleep(sleep)

    return out, kept, dropped, failed
