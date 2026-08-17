"""Search stage - Tavily news lookups per entity, with canonicalization and dedupe.

Two templated queries per entity, built from ONLY the entity name and (optionally)
its city. That template expansion is the entire privacy surface of this project:
nothing from the rent roll - no rent, square footage, lease dates, or building
names - is permitted to reach the network. tests/test_search.py enforces it.

Used by serve.py (live runs) and runnable on its own for a quick check:

    python search.py --type tenants --days 7 --limit 2
    python search.py --type competitors --days 30 --dry-run   # print queries, call nothing

Costs 1 Tavily credit per query, so 2 credits per entity per run.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Query params that identify a campaign or a session rather than a document.
TRACKING_PARAMS = re.compile(
    r"^(utm_|fbclid$|gclid$|gbraid$|wbraid$|msclkid$|mc_cid$|mc_eid$|igshid$|ref$|ref_src$|"
    r"cmpid$|campaign_id$|s_kwcid$|_hsenc$|_hsmi$|yclid$|dclid$)",
    re.IGNORECASE,
)

# Outlets whose bare domain reads badly as a source name.
DOMAIN_NAME_FIXUPS = {
    "wsj.com": "WSJ",
    "ft.com": "Financial Times",
    "bizjournals.com": "Business Journals",
    "prnewswire.com": "PR Newswire",
    "globenewswire.com": "GlobeNewswire",
    "businesswire.com": "Business Wire",
    "sec.gov": "SEC",
    "costar.com": "CoStar",
    "bisnow.com": "Bisnow",
    "therealdeal.com": "The Real Deal",
    "globest.com": "GlobeSt",
    "tipranks.com": "TipRanks",
    "investing.com": "Investing.com",
    "stocktitan.net": "StockTitan",
    "reuters.com": "Reuters",
    "bloomberg.com": "Bloomberg",
}


def canonical_url(url: str) -> str:
    """Strip tracking params and fragments so the same story hashes identically."""
    try:
        parts = urlparse(url.strip())
    except ValueError:
        return url.strip()

    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING_PARAMS.match(k)]
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = parts.path.rstrip("/") or "/"

    return urlunparse((parts.scheme.lower() or "https", netloc, path, "", urlencode(kept), ""))


def url_hash(url: str) -> str:
    """Stable 16-hex id for a canonical URL - the cross-run dedupe key."""
    return hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()[:16]


def source_name(url: str) -> str:
    """Readable outlet name derived from the domain."""
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host in DOMAIN_NAME_FIXUPS:
        return DOMAIN_NAME_FIXUPS[host]
    # Drop a leading subdomain like "news." but keep two-label brands intact.
    labels = host.split(".")
    if len(labels) > 2 and labels[0] in {"news", "www2", "finance", "web", "amp"}:
        labels = labels[1:]
    stem = labels[0] if labels else host
    return stem.replace("-", " ").title()


def format_published(raw: str | None) -> str:
    """Tavily returns RFC-1123 dates; render them as 'Aug 14, 2026'."""
    if not raw:
        return ""
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return raw
    return f"{dt.strftime('%b')} {dt.day}, {dt.year}"


def build_queries(name: str, city: str, templates: list[str], *, include_city: bool = True) -> list[str]:
    """Expand the query templates for one entity.

    THE PRIVACY BOUNDARY. Only ``name`` and ``city`` are substituted; callers must
    never pass anything else, and the templates have no other placeholders.
    """
    city_value = (city or "").strip() if include_city else ""
    queries = []
    for tpl in templates:
        q = tpl.format(name=name.strip(), city=city_value)
        queries.append(re.sub(r"\s{2,}", " ", q).strip())
    return queries


def search_entity(
    client,
    name: str,
    city: str,
    *,
    templates: list[str],
    days: int,
    max_results: int = 5,
    search_depth: str = "basic",
    include_city: bool = True,
    sleep: float = 0.5,
    on_error=None,
) -> list[dict]:
    """Run every query for one entity and return deduped article dicts.

    Errors on a single query are reported and skipped rather than aborting the
    entity - one bad query should not lose the other one's results.
    """
    seen: set[str] = set()
    articles: list[dict] = []

    for query in build_queries(name, city, templates, include_city=include_city):
        try:
            resp = client.search(
                query,
                topic="news",
                days=days,
                max_results=max_results,
                search_depth=search_depth,
            )
        except Exception as exc:  # noqa: BLE001 - continue with the remaining query
            if on_error:
                on_error(f"{name}: query failed ({type(exc).__name__}: {str(exc)[:90]})")
            continue

        for hit in resp.get("results", []) or []:
            url = hit.get("url") or ""
            if not url:
                continue
            h = url_hash(url)
            if h in seen:
                continue
            seen.add(h)
            articles.append(
                {
                    "url_hash": h,
                    "title": (hit.get("title") or "").strip(),
                    "snippet": (hit.get("content") or "").strip(),
                    "source_name": source_name(url),
                    "source_url": url,
                    "published_date": format_published(hit.get("published_date")),
                    "score": hit.get("score"),
                }
            )

        if sleep:
            time.sleep(sleep)

    # Highest-scoring first so a per-entity cap keeps the most relevant hits.
    articles.sort(key=lambda a: a.get("score") or 0, reverse=True)
    return articles


def get_client():
    """Tavily client from TAVILY_API_KEY, raising clearly when unset."""
    key = os.environ.get("TAVILY_API_KEY", "")
    if not key:
        raise ValueError("TAVILY_API_KEY is not set. Add it to .env and reload.")
    from tavily import TavilyClient

    return TavilyClient(api_key=key)


def period_label(days: int) -> str:
    """Human label for a lookback window, e.g. 'Last 7 days (Aug 11 - Aug 17, 2026)'."""
    now = datetime.now(timezone.utc)
    start = now.timestamp() - days * 86400
    start_dt = datetime.fromtimestamp(start, tz=timezone.utc)
    same_year = start_dt.year == now.year
    left = f"{start_dt.strftime('%b')} {start_dt.day}" + ("" if same_year else f", {start_dt.year}")
    right = f"{now.strftime('%b')} {now.day}, {now.year}"
    return f"Last {days} days ({left} - {right})"


def main() -> int:
    from dotenv import load_dotenv

    load_dotenv()

    from render import ROOT, load_config, read_watchlist

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--type", default="tenants", help="report type from config/report_types.yaml")
    ap.add_argument("--days", type=int, default=7, help="lookback window in days")
    ap.add_argument("--limit", type=int, default=0, help="only the first N entities")
    ap.add_argument("--no-city", action="store_true", help="send the name only, omit the city")
    ap.add_argument("--dry-run", action="store_true", help="print the queries and exit, calling nothing")
    args = ap.parse_args()

    config = load_config()
    specs = config["report_types"]
    if args.type not in specs:
        sys.exit(f"error: unknown report type {args.type!r} - choose from {', '.join(specs)}")
    spec = specs[args.type]
    search_cfg = config.get("search", {})

    entries = read_watchlist(ROOT / spec["watchlist"])
    if args.limit:
        entries = entries[: args.limit]

    print(f"{spec['label']}: {len(entries)} entities, {args.days}-day lookback")
    print(f"{period_label(args.days)}\n")

    if args.dry_run:
        for name, city in entries:
            for q in build_queries(name, city, spec["query_templates"], include_city=not args.no_city):
                print(f"  {q}")
        print(f"\n{len(entries) * len(spec['query_templates'])} queries would cost that many credits")
        return 0

    client = get_client()
    total = 0
    for name, city in entries:
        articles = search_entity(
            client,
            name,
            city,
            templates=spec["query_templates"],
            days=args.days,
            max_results=search_cfg.get("max_results_per_query", 5),
            search_depth=search_cfg.get("search_depth", "basic"),
            include_city=not args.no_city,
            sleep=search_cfg.get("sleep_between_calls", 0.5),
            on_error=lambda m: print(f"  ! {m}", file=sys.stderr),
        )
        total += len(articles)
        print(f"{name}: {len(articles)} unique articles")
        for a in articles[:4]:
            print(f"    [{a['published_date'] or 'no date'}] {a['source_name']}: {a['title'][:64]}")

    print(f"\n{total} articles across {len(entries)} entities")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
