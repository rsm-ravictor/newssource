"""Judge stage - Claude (via TritonAI) decides relevance, category, and priority.

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
from typing import Literal, Optional

from pydantic import BaseModel, Field, ValidationError

from utils.connect import ask, ask_json

Priority = Literal["high", "medium", "low"]

# Appended to every report type's criteria. TritonAI accepts
# response_format=json_object but does not enforce it, so the envelope has to be
# spelled out in the prompt or responses arrive fenced, bare-arrayed, or chatty.
OUTPUT_FORMAT = """
WRITING the output:
- headline: <= 90 characters, factual, specific, no hype and no clickbait.
- client_summary: 2-3 sentences a broker could forward to a client unedited. State only what the
  article supports. Never speculate about rent, lease terms, or your own firm's position.
- confidence: 0.0-1.0, how firmly the article supports the judgment.
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
        category: Optional[CategoryT] = None  # type: ignore[valid-type]
        priority: Optional[Priority] = None
        headline: str = Field(default="", description="<= 90 characters, factual, no hype")
        client_summary: str = Field(default="", description="2-3 sentences, client-ready")
        confidence: float = 0.0
        reason_if_excluded: str = ""

    class CompanyJudgment(BaseModel):
        judgments: list[Judgment]

    return CompanyJudgment


def criteria_for(spec: dict) -> str:
    """A report type's criteria plus the shared output-format contract."""
    return spec["criteria"].rstrip() + "\n" + OUTPUT_FORMAT


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
) -> tuple[list[dict], int, int, list[str]]:
    """Judge a list of entities, returning (kept_entities, kept, dropped, failed).

    ``progress`` is called with short status strings so a CLI or the live runner
    can report entity-by-entity without this module knowing about either.
    """
    schema = make_schema(list(spec["categories"]))
    criteria = criteria_for(spec)

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

        alerts = []
        for j in result.judgments:
            art = by_url.get(j.source_url)
            if art is None:
                print(f"  ! judgment for unknown url {j.source_url!r}", file=sys.stderr)
                continue
            if not j.is_relevant or not j.category or not j.priority:
                dropped += 1
                # Say why: on a live run over real news most articles are noise, and
                # "0 kept" is only trustworthy if you can see what was thrown out.
                say(f"    - {art['title'][:58]} ({(j.reason_if_excluded or 'no reason given')[:64]})")
                continue
            kept += 1
            say(f"    + {j.priority}/{j.category}: {j.headline[:58]}")
            alerts.append(
                {
                    "category": j.category,
                    "priority": j.priority,
                    "headline": j.headline,
                    "client_summary": j.client_summary,
                    "confidence": round(j.confidence, 2),
                    # Provenance comes from the local record, not the model.
                    "source_name": art["source_name"],
                    "source_url": art["source_url"],
                    "published_date": art["published_date"],
                }
            )

        say(f"{entity['display_name']}: {len(alerts)} kept, {len(by_url) - len(alerts)} excluded")
        if alerts:
            out.append(
                {
                    "display_name": entity["display_name"],
                    "city": entity.get("city", ""),
                    "alerts": alerts,
                }
            )
        if sleep:
            time.sleep(sleep)

    return out, kept, dropped, failed
