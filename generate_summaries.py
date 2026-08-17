"""Judge stage demo - turn raw article hits into digest-ready findings via TritonAI.

This is the CONTEXT.md judge stage, scaled down to the prototype: one LLM call
per entity covering all of that entity's unjudged articles, a Pydantic schema
for structured output, and criteria that include explicit exclusions. Every call
goes through ``utils.connect`` - no second client, no direct ``openai.OpenAI()``.

Both report types run through this one script. The categories and the judging
criteria come from config/report_types.yaml, so the tenants and competitors
taxonomies are data, not code.

    python generate_summaries.py                      # both report types
    python generate_summaries.py --type competitors    # just one
    python generate_summaries.py --verbose             # print route + server model id
    python generate_summaries.py --model api-gpt-oss-120b
    python generate_summaries.py --limit 2            # first two entities per type

Writes the `alerts` file named by each report type, which build_preview.py then
renders. Requires TRITONAI_API_KEY in .env; without it the call raises rather
than falling back.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Literal, Optional

# .env must be loaded BEFORE utils.connect is imported - connect.py reads
# os.environ at call time, and this is a script, not a notebook.
from dotenv import load_dotenv

load_dotenv()

from pydantic import BaseModel, Field, ValidationError  # noqa: E402

# Reused so the judge and the renderer parse watchlists and config identically.
from build_preview import ROOT, load_config, read_watchlist  # noqa: E402
from utils.connect import DEFAULT_MODEL, ask, ask_json, get_client  # noqa: E402

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


def _unfence(text: str) -> str:
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


def _coerce_envelope(data):
    """Normalize the shapes the model returns into {"judgments": [...]}.

    The schema asks for a single ``judgments`` key, but responses arrive as a
    bare array, a single bare judgment, or the array under some other key.
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


def build_prompt(company: dict, spec: dict) -> str:
    """Only the entity name and city are named; nothing from the rent roll."""
    lines = [
        f"{spec['entity_noun'].capitalize()}: {company['display_name']}",
        f"City: {company.get('city', 'unknown')}",
        "",
        f"Judge each of the following {len(company['articles'])} articles.",
        "",
    ]
    for i, art in enumerate(company["articles"], 1):
        lines += [
            f"--- Article {i} ---",
            f"source_url: {art['source_url']}",
            f"outlet: {art['source_name']} ({art['published_date']})",
            f"title: {art['title']}",
            f"body: {art['snippet']}",
            "",
        ]
    return "\n".join(lines)


def judge_company(company: dict, spec: dict, *, schema: type, **kw):
    """``ask_json`` first; repair the response if the model fenced its JSON.

    TritonAI accepts ``response_format={"type": "json_object"}`` but does not
    enforce it, so responses intermittently arrive as ```json ... ```, as a bare
    array, or with trailing prose, and fail Pydantic validation inside
    ``ask_json``. ``connect.py`` is verbatim-locked and cannot strip them, so the
    retry happens here: same model, same client, same prompt - a parsing repair,
    not a model fallback.
    """
    prompt = build_prompt(company, spec)
    try:
        return ask_json(prompt, schema=schema, **kw)
    except ValidationError:
        print("  ~ response was not clean JSON; retrying via ask() + repair", flush=True)
        text = ask(prompt, **kw) or ""
        return schema.model_validate(_coerce_envelope(json.loads(_unfence(text))))


def run_type(key: str, spec: dict, args) -> tuple[int, int, list[str]]:
    """Judge every entity for one report type and write its alerts file."""
    print(f"\n=== {spec['label']} ===")
    fixture = json.loads((ROOT / spec["articles"]).read_text(encoding="utf-8"))
    watchlist = read_watchlist(ROOT / spec["watchlist"])
    schema = make_schema(list(spec["categories"]))
    criteria = spec["criteria"].rstrip() + "\n" + OUTPUT_FORMAT

    companies = fixture.get("companies", [])
    if args.limit:
        companies = companies[: args.limit]

    client = get_client()  # one client reused across every entity
    out_companies: list[dict] = []
    failed: list[str] = []
    kept = dropped = 0

    for company in companies:
        # Article metadata stays local; the model only returns judgments keyed by url.
        by_url = {a["source_url"]: a for a in company["articles"]}
        print(f"judging {company['display_name']} ({len(by_url)} articles)...", flush=True)

        try:
            result = judge_company(
                company,
                spec,
                schema=schema,
                model=args.model,
                system=criteria,
                temperature=0.2,
                max_tokens=4000,
                verbose=args.verbose,
                client=client,
            )
        except Exception as exc:  # noqa: BLE001 - one bad entity must not lose the run
            failed.append(company["display_name"])
            print(f"  ! skipped: {type(exc).__name__}: {str(exc)[:120]}", file=sys.stderr)
            continue

        alerts = []
        for j in result.judgments:
            art = by_url.get(j.source_url)
            if art is None:
                print(f"  ! judgment for unknown url {j.source_url!r}", file=sys.stderr)
                continue
            if not j.is_relevant or not j.category or not j.priority:
                dropped += 1
                print(f"  - excluded: {art['title'][:56]}... ({j.reason_if_excluded[:58]})")
                continue
            kept += 1
            print(f"  + {j.priority:<6} {j.category:<19} {j.headline[:50]}")
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

        if alerts:
            out_companies.append(
                {
                    "display_name": company["display_name"],
                    "city": company.get("city", ""),
                    "alerts": alerts,
                }
            )
        time.sleep(0.4)  # be polite to the proxy

    payload = {
        "_note": (
            f"Generated by generate_summaries.py via TritonAI ({args.model}) for report type "
            f"'{key}'. Source articles are fictional."
        ),
        "run_date": fixture.get("run_date", ""),
        "period_label": fixture.get("period_label", ""),
        "portfolio_size": len(watchlist) or fixture.get("portfolio_size", 0),
        "companies": out_companies,
    }
    out_path = ROOT / spec["alerts"]
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"  -> {kept} kept, {dropped} excluded, written to {spec['alerts']}")
    return kept, dropped, failed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--type", default="all", help="report type: tenants, competitors, or all")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"TritonAI model id (default: {DEFAULT_MODEL})")
    ap.add_argument("--limit", type=int, default=0, help="judge only the first N entities per type")
    ap.add_argument("--verbose", action="store_true", help="print route and server-reported model")
    args = ap.parse_args()

    specs = load_config()["report_types"]
    if args.type == "all":
        wanted = list(specs)
    elif args.type in specs:
        wanted = [args.type]
    else:
        sys.exit(f"error: unknown report type {args.type!r} - choose from {', '.join(specs)}, or all")

    total_kept = total_dropped = 0
    all_failed: list[str] = []
    for key in wanted:
        kept, dropped, failed = run_type(key, specs[key], args)
        total_kept += kept
        total_dropped += dropped
        all_failed += failed

    print(f"\n{total_kept} findings kept, {total_dropped} excluded across {len(wanted)} report type(s)")
    if all_failed:
        # Surfaced loudly rather than silently shrinking a digest.
        print(f"{len(all_failed)} entity/entities skipped on error: {', '.join(all_failed)}", file=sys.stderr)
    print("next: python build_preview.py --open")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
