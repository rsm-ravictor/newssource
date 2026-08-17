"""Judge stage demo - turn raw article hits into digest-ready findings via TritonAI.

This is the CONTEXT.md judge stage, scaled down to the prototype: one LLM call
per company covering all of that company's unjudged articles, a Pydantic schema
for structured output, and criteria that include explicit exclusions. Every call
goes through ``utils.connect`` - no second client, no direct ``openai.OpenAI()``.

    python generate_summaries.py                  # judge data/mock_articles.json
    python generate_summaries.py --verbose        # print route + server model id
    python generate_summaries.py --model gemini-3-flash
    python generate_summaries.py --limit 2        # first two companies only

Writes data/sample_alerts.json, which build_preview.py then renders. Requires
TRITONAI_API_KEY in .env; without it the call raises rather than falling back.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Literal, Optional

# .env must be loaded BEFORE utils.connect is imported — connect.py reads
# os.environ at call time, and this is a script, not a notebook.
from dotenv import load_dotenv

load_dotenv()

from pydantic import BaseModel, Field, ValidationError  # noqa: E402

from utils.connect import DEFAULT_MODEL, ask, ask_json, get_client  # noqa: E402

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

Category = Literal["financial_distress", "expansion", "office_move", "leadership_change"]
Priority = Literal["high", "medium", "low"]


class Judgment(BaseModel):
    """One article, judged."""

    source_url: str = Field(description="echo the source_url of the article being judged")
    is_relevant: bool
    category: Optional[Category] = None
    priority: Optional[Priority] = None
    headline: str = Field(default="", description="<= 90 characters, factual, no hype")
    client_summary: str = Field(default="", description="2-3 sentences, client-ready")
    confidence: float = 0.0
    reason_if_excluded: str = ""


class CompanyJudgment(BaseModel):
    judgments: list[Judgment]


CRITERIA = """You are a commercial real-estate analyst screening news about tenants in a
landlord's portfolio. For each article, decide whether it is a MEANINGFUL development that a
landlord's asset-management team would act on.

RELEVANT categories (pick exactly one):
- financial_distress: covenant breaches, defaults, restructuring/bankruptcy advisors, layoffs,
  going-concern doubt, missed payments, credit downgrades.
- expansion: funding rounds or contracts that fund growth, headcount growth, new locations,
  stated intent to take more space.
- office_move: consolidations, relocations, subleasing, headquarters changes, footprint
  reductions, space searches, lease decisions.
- leadership_change: CEO/CFO/managing-partner changes, or leadership changes explicitly tied to
  a real-estate or cost strategy review.

EXCLUDE (set is_relevant=false and fill reason_if_excluded):
- Routine business news with no space or credit implication.
- Product launches, feature releases, pricing news.
- Marketing, awards, sponsorships, "best places to work", CSR announcements.
- Minor personnel changes below the executive level, ordinary associate or staff hires.
- Opinion pieces, listicles, and articles that merely mention the company in passing.

PRIORITY rubric:
- high: an act-this-week signal. Credit risk to rent collection, or a concrete move/expansion
  decision that affects space demand now.
- medium: a real signal worth tracking that has no immediate action attached.
- low: weak or speculative, relevant only as background.

WRITING the output:
- headline: <= 90 characters, factual, specific, no hype and no clickbait.
- client_summary: 2-3 sentences a broker could forward to a client unedited. State only what the
  article supports. Never speculate about rent, lease terms, or the landlord's own position.
- confidence: 0.0-1.0, how firmly the article supports the judgment.
- Return one judgment object per article given, echoing its source_url.

OUTPUT FORMAT: return ONLY raw JSON - a single object with exactly one top-level key,
"judgments", whose value is an array with one object per article. Not a bare array, not a single
bare object. Do not wrap it in markdown code fences and do not add prose before or after it.
TritonAI does not enforce the OpenAI json_object response format, so this instruction is what
actually keeps the response parseable.
"""


def _unfence(text: str) -> str:
    """Pull the first complete JSON object out of a fenced or chatty response.

    Brace-matched rather than trimmed to the last ``}``: models append prose
    *after* valid JSON as often as they wrap it in fences, and a response that
    starts with ``{`` can still have a trailing paragraph. Quotes and escapes
    are tracked so a ``}`` inside a string does not end the scan early.
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


def judge_company(company: dict, *, schema: type, **kw):
    """``ask_json`` first; repair the response if the model fenced its JSON.

    TritonAI accepts ``response_format={"type": "json_object"}`` but does not
    enforce it, so responses intermittently arrive as ```json ... ``` and fail
    Pydantic validation inside ``ask_json``. ``connect.py`` is verbatim-locked
    and cannot strip them, so the retry happens here: same model, same client,
    same prompt - a parsing repair, not a model fallback.
    """
    prompt = build_prompt(company)
    try:
        return ask_json(prompt, schema=schema, **kw)
    except ValidationError:
        print("  ~ response was not clean JSON; retrying via ask() + repair", flush=True)
        text = ask(prompt, **kw) or ""
        return schema.model_validate(_coerce_envelope(json.loads(_unfence(text))))


def build_prompt(company: dict) -> str:
    """Only the company name and city are named; nothing from the rent roll."""
    lines = [
        f"Tenant: {company['display_name']}",
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"TritonAI model id (default: {DEFAULT_MODEL})")
    ap.add_argument("--articles", default=str(DATA / "mock_articles.json"))
    ap.add_argument("--out", default=str(DATA / "sample_alerts.json"))
    ap.add_argument("--limit", type=int, default=0, help="judge only the first N companies")
    ap.add_argument("--verbose", action="store_true", help="print route and server-reported model")
    args = ap.parse_args()

    src = Path(args.articles)
    if not src.exists():
        sys.exit(f"error: {src} not found")
    fixture = json.loads(src.read_text(encoding="utf-8"))

    companies = fixture.get("companies", [])
    if args.limit:
        companies = companies[: args.limit]

    client = get_client()  # one client reused across every company
    out_companies: list[dict] = []
    failed: list[str] = []
    kept = dropped = 0

    for company in companies:
        # Article metadata stays local; the model only returns judgments keyed by url.
        by_url = {a["source_url"]: a for a in company["articles"]}
        print(f"judging {company['display_name']} ({len(by_url)} articles)...", flush=True)

        try:
            result: CompanyJudgment = judge_company(
                company,
                schema=CompanyJudgment,
                model=args.model,
                system=CRITERIA,
                temperature=0.2,
                max_tokens=4000,
                verbose=args.verbose,
                client=client,
            )
        except Exception as exc:  # noqa: BLE001 - one bad company must not lose the run
            failed.append(company["display_name"])
            print(f"  ! skipped: {type(exc).__name__}: {str(exc)[:120]}", file=sys.stderr)
            continue

        alerts = []
        for j in result.judgments:
            art = by_url.get(j.source_url)
            if art is None:
                print(f"  ! skipping judgment for unknown url {j.source_url!r}", file=sys.stderr)
                continue
            if not j.is_relevant or not j.category or not j.priority:
                dropped += 1
                print(f"  - excluded: {art['title'][:58]}... ({j.reason_if_excluded[:60]})")
                continue
            kept += 1
            print(f"  + {j.priority:<6} {j.category:<19} {j.headline[:52]}")
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
        "_note": f"Generated by generate_summaries.py via TritonAI ({args.model}). Source articles are fictional.",
        "run_date": fixture.get("run_date", ""),
        "period_label": fixture.get("period_label", ""),
        "portfolio_size": fixture.get("portfolio_size", 0),
        "companies": out_companies,
    }
    Path(args.out).write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"\n{kept} findings kept, {dropped} excluded -> {args.out}")
    if failed:
        # Surfaced loudly rather than silently shrinking the digest.
        print(f"{len(failed)} company/companies skipped on error: {', '.join(failed)}", file=sys.stderr)
    print("next: python build_preview.py --open")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
