"""Watchlist parsing - who to search, and how important they are.

The point of reference is the two hand-maintained Markdown rosters at the repo root:

    tenant-list.md       | # | Tenant Name | City |          <- "#" is the PRIORITY RANK
    competitor-list.md   | Company | Category | Ticker | Notes |   (market from "## City")

Markdown TABLES are read by column name, so the meaning of a field comes from its
header rather than its position. Recognized headers:

    name      Company / Tenant Name / Name / Competitor / Entity / Firm / Where
    city      City / Market / Location / Submarket / Metro
    rank      # / No / Rank / Priority / Order          <- feeds stage-2 prioritization
    category  Category / Segment / Type / Sector
    ticker    Ticker / Symbol
    notes     Notes / Comment

A table with no City column inherits the city from the nearest "## Heading", which
is how competitor-list.md is organized (one table per market).

The older flat shapes still parse, so a pasted list or an ingest.py output works:

    - Kilroy Realty | San Diego        (bullet, pipe-delimited)
    * Simon Property Group, San Diego  (bullet, comma-delimited)
    1. Regency Centers                 (numbered, no city)
    Essex | San Diego                  (bare line)

Headings, blockquotes, code fences, horizontal rules, table separators, header rows
and ordinary prose are skipped, so a normal-looking Markdown document works without
a special format.

Only the NAME and CITY are ever sent to the network. Rank, category, ticker and
notes stay local: rank shapes prioritization, the rest is for the reference page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path

# Leading list markers: "- ", "* ", "+ ", "1. ", "1) ".
_BULLET = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)")
# Markdown emphasis and inline code around a name.
_DECOR = re.compile(r"[*_`]+")
# A table separator such as |---|:---:|
_TABLE_RULE = re.compile(r"^\|?[\s:|-]+\|?$")
# Words that mean "this row is a header, not an entity".
_HEADER_WORDS = {
    "name", "names", "company", "companies", "competitor", "competitors",
    "tenant", "tenants", "tenant name", "entity", "entities", "where", "firm",
    "landlord", "#", "rank",
}

# Header text -> field. Matched case-insensitively on the stripped cell.
_COLUMNS: dict[str, str] = {}
for _field, _aliases in {
    "name": ("company", "companies", "tenant name", "tenant", "name", "competitor",
             "entity", "firm", "landlord", "who", "where"),
    "city": ("city", "market", "location", "submarket", "metro"),
    "rank": ("#", "no", "no.", "num", "rank", "priority", "order"),
    "category": ("category", "segment", "type", "sector", "use"),
    "ticker": ("ticker", "symbol"),
    "notes": ("notes", "note", "comment", "comments"),
}.items():
    for _alias in _aliases:
        _COLUMNS[_alias] = _field

# A section heading is only trusted as a city when it reads like a place: short,
# and not a document title. Guards against "# Tenant Roster - Office" becoming a city.
_NOT_A_CITY = re.compile(r"\b(list|roster|tenant|competitor|portfolio|overview|by market)\b", re.I)


# ── Name cleaning (shared with ingest.py, which imports these) ─────────────
# Roster names arrive SHOUTED and suffixed ("LPL HOLDINGS, INC"), which searches
# badly and collides with the "Name, City" flat format. Names that already carry
# mixed case ("DivcoWest") are left alone - only the case of an ALL-CAPS name is
# rewritten, so a hand-typed list is never mangled.

# Trailing corporate forms, stripped so the query is the trading name.
LEGAL_SUFFIXES = {
    "inc", "inc.", "incorporated", "llc", "l.l.c.", "llc.", "lp", "l.p.", "llp",
    "corp", "corp.", "corporation", "co", "co.", "company", "ltd", "ltd.",
    "limited", "plc", "pc", "p.c.", "pa", "lllp", "na", "n.a.",
}

# Tokens that must not be title-cased into nonsense.
KEEP_UPPER = {
    "US", "USA", "UK", "LPL", "ABC", "IQHQ", "REI", "CVS", "UPS", "HSBC", "BBVA",
    "PNC", "EQR", "KFC", "GNC", "AAA", "JPL", "BJS", "CBRE", "JLL", "TMG", "RSM",
    "DEQ", "IRS", "ZS", "PTC", "SK", "MN", "PCL", "USI", "ARS", "EA", "AT",
}

_MC_O = re.compile(r"^(Mc|O')([a-z])")

# Function words that read wrong in caps mid-name ("State OF Oregon"). Checked
# after KEEP_UPPER, so a real acronym like AT&T's "AT" is not lowercased.
_LOWER_WORDS = {"of", "and", "the", "for", "at", "in", "on", "to", "by", "or", "de", "la"}


def smart_title(name: str) -> str:
    """Title-case a SHOUTED name without mangling acronyms or possessives."""
    words = []
    for i, raw in enumerate(name.split()):
        if raw.upper() in KEEP_UPPER:
            words.append(raw.upper())
        elif i and raw.lower() in _LOWER_WORDS:
            words.append(raw.lower())
        elif len(raw) <= 3 and raw.isalpha() and raw.isupper():
            words.append(raw.upper())          # short all-caps reads as an acronym
        elif "." in raw and raw == raw.upper():
            words.append(raw.upper())          # dotted acronym: H.G., P.C.
        elif any(ch.isdigit() for ch in raw):
            words.append(raw.upper())          # 7-ELEVEN, 24 HOUR
        else:
            word = raw.capitalize()
            word = _MC_O.sub(lambda m: m.group(1) + m.group(2).upper(), word)
            words.append(word)
    return " ".join(words)


def clean_name(raw: str) -> str:
    """Normalize a roster name for searching.

    A SHOUTED name is title-cased and has its trailing legal form peeled off. A
    name that already carries mixed case was typed that way by a human, so only
    the delimiter characters are touched - "DivcoWest" and "Queen Emma Land
    Company" survive intact.
    """
    name = re.sub(r"\s+", " ", str(raw).strip())
    if not name:
        return ""

    if name == name.upper():
        # Peel suffixes repeatedly, so "FOO, INC, LLC" reduces to "FOO".
        changed = True
        while changed:
            changed = False
            name = name.rstrip(" ,.;")
            parts = re.split(r"[,\s]+", name)
            if len(parts) > 1 and parts[-1].lower().strip(".,") in LEGAL_SUFFIXES:
                name = " ".join(parts[:-1])
                changed = True
        name = smart_title(name.rstrip(" ,.;"))

    # A surviving comma would be read as a city; a pipe is the field delimiter.
    return name.rstrip(" ,.;").replace(",", "").replace("|", "/")


@dataclass(frozen=True)
class Entry:
    """One watchlist row. Only ``name`` and ``city`` are ever searched on."""

    name: str
    city: str = ""
    rank: int | None = None
    # The roster spelling, kept only when cleaning changed it, so the reference
    # page can show what the source file actually says.
    raw_name: str = ""
    category: str = ""
    ticker: str = ""
    notes: str = ""
    section: str = ""

    @property
    def label(self) -> str:
        return f"{self.name} — {self.city}" if self.city else self.name

    @property
    def segment(self) -> str:
        """The part of the section heading that names the roster it came from.

        "Tenant Roster - Retail & Multi Unit" is a heading; "Retail & Multi Unit"
        is the segment, and it is what the rank is counted within.
        """
        return self.section.split(" - ")[-1].strip() if " - " in self.section else self.section


def _cells(row: str) -> list[str]:
    """Cells of a Markdown table row, keeping interior blanks.

    Only the leading and trailing empties created by the "|" borders are dropped;
    an empty Ticker column in the middle has to survive or every later column
    would shift left by one.
    """
    parts = [p.strip() for p in row.split("|")]
    if parts and not parts[0]:
        parts.pop(0)
    if parts and not parts[-1]:
        parts.pop()
    return parts


def _header_map(cells: list[str]) -> dict[str, int] | None:
    """Field -> column index, if this row looks like a table header."""
    mapping: dict[str, int] = {}
    for i, cell in enumerate(cells):
        field = _COLUMNS.get(cell.strip().lower())
        if field and field not in mapping:
            mapping[field] = i
    # Without a name column there is nothing to search on, so it is not a header
    # we can use - and a data row never has a cell literally called "Company".
    return mapping if "name" in mapping else None


def _split_fields(text: str) -> tuple[str, str]:
    """Name and city from one flat row. Pipe wins; otherwise the first comma."""
    if "|" in text:
        parts = [p.strip() for p in text.split("|")]
    else:
        head, _, tail = text.partition(",")
        parts = [head.strip(), tail.strip()]
    parts = [p for p in parts if p]
    if not parts:
        return "", ""
    return parts[0], (parts[1] if len(parts) > 1 else "")


def _looks_like_prose(line: str) -> bool:
    """True for an explanatory sentence rather than an entity row.

    The rosters carry a description line above the table ("Columns: Company |
    Category | Ticker | Notes."), which contains pipes and would otherwise parse
    as a company. Sentences end in punctuation or run long; names do neither.
    """
    return line.rstrip().endswith((".", ":", "!", "?")) or len(line.split()) > 12


def _rank_of(text: str) -> int | None:
    text = text.strip().lstrip("#").strip()
    return int(text) if text.isdigit() else None


def _city_from_section(section: str) -> str:
    """A "## San Diego" heading used as the city for a table with no City column.

    "Oahu / Waikiki" style headings name two places; the first is the searchable
    one, so the query does not carry a slash.
    """
    if not section or _NOT_A_CITY.search(section) or len(section.split()) > 4:
        return ""
    return section.split("/")[0].strip()


def _entry(name: str, **fields) -> Entry:
    """Build an Entry with the roster spelling cleaned for searching."""
    clean = clean_name(name)
    return Entry(name=clean or name, raw_name=name if clean != name else "", **fields)


def parse_watchlist(text: str) -> list[Entry]:
    """Entries from Markdown or plain-text list content, in file order."""
    entries: list[Entry] = []
    in_fence = False
    section = ""
    columns: dict[str, int] | None = None

    for raw in text.splitlines():
        line = raw.strip()

        if line.startswith("```") or line.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if not line:
            # A blank line does not end a table on its own, but a heading does.
            continue
        if line.startswith("#"):
            section = line.lstrip("#").strip()
            columns = None          # each section gets its own table header
            continue
        if line.startswith(">"):
            continue
        # Horizontal rules and table separators.
        if set(line) <= set("-=*_ ") or _TABLE_RULE.match(line):
            continue

        # ── Markdown table row ────────────────────────────────────────────
        if line.startswith("|"):
            cells = _cells(line)
            if not cells:
                continue
            found = _header_map(cells)
            if found is not None and columns is None:
                columns = found
                continue
            if found is not None and columns is not None:
                continue        # a repeated header inside the same table

            def cell(field: str) -> str:
                idx = (columns or {}).get(field)
                return cells[idx] if idx is not None and idx < len(cells) else ""

            if columns:
                name = _DECOR.sub("", cell("name")).strip()
                city = cell("city")
                rank = _rank_of(cell("rank"))
                category, ticker, notes = cell("category"), cell("ticker"), cell("notes")
            else:
                # A table with no recognizable header: positional, name then city.
                name = _DECOR.sub("", cells[0]).strip()
                city = cells[1] if len(cells) > 1 else ""
                rank, category, ticker, notes = None, "", "", ""

            if not name or name.lower() in _HEADER_WORDS:
                continue
            entries.append(
                _entry(
                    name,
                    city=city or _city_from_section(section),
                    rank=rank,
                    category="" if category.lower() in ("(unspecified)", "n/a", "-") else category,
                    ticker=ticker,
                    notes=notes,
                    section=section,
                )
            )
            continue

        # ── Flat line: bullet, numbered, or bare ──────────────────────────
        columns = None
        bulleted = bool(_BULLET.match(line))
        body = _DECOR.sub("", _BULLET.sub("", line)).strip()
        if not body:
            continue
        # Prose only ever appears unbulleted; a bulleted line is always an entry.
        if not bulleted and _looks_like_prose(body):
            continue

        name, city = _split_fields(body)
        if not name or name.lower() in _HEADER_WORDS:
            continue
        entries.append(
            _entry(name, city=city or _city_from_section(section), section=section)
        )

    return entries


def canonicalize(entries: list[Entry], aliases: dict[str, str] | None = None) -> list[Entry]:
    """Fold roster spellings of one firm onto a single name.

    A hand-maintained roster drifts: the same landlord is entered as "Kilroy" in
    one market and "Kilroy Realty" in another. Left alone that is two entities -
    two searches, two index pills, two ways for one event to appear twice - so the
    variants are mapped onto one canonical spelling before dedupe runs.

    This renames; it does not merge across markets. One firm competing in two
    markets is still two roster entries, which is deliberate: their submarket
    relevance differs. Matching is case- and punctuation-insensitive, and the
    original roster spelling is preserved in ``raw_name`` so the reference page can
    still show what the file says.
    """
    if not aliases:
        return entries

    def norm(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()

    lookup = {norm(variant): canon for canon, variants in aliases.items() for variant in variants}
    lookup.update({norm(canon): canon for canon in aliases})

    out = []
    for entry in entries:
        canon = lookup.get(norm(entry.name))
        if canon and canon != entry.name:
            out.append(replace(entry, name=canon, raw_name=entry.raw_name or entry.name))
        else:
            out.append(entry)
    return out


def dedupe(entries: list[Entry]) -> list[Entry]:
    """One row per (name, city), keeping the best rank.

    The tenant roster lists a company once per lease, so GOOGLE appears at rank 1
    and again at 24. Searching it twice would double the cost for one result, and
    the lower number is the one that should drive prioritization.
    """
    best: dict[tuple[str, str], Entry] = {}
    order: list[tuple[str, str]] = []
    for entry in entries:
        key = (entry.name.lower(), entry.city.lower())
        if key not in best:
            best[key] = entry
            order.append(key)
            continue
        kept = best[key]
        # A rank beats no rank; otherwise the lower number wins.
        if entry.rank is not None and (kept.rank is None or entry.rank < kept.rank):
            best[key] = replace(kept, rank=entry.rank)
    return [best[k] for k in order]


def read_entries(path: Path, aliases: dict[str, str] | None = None) -> list[Entry]:
    """Parse a watchlist file, canonicalized and deduped, falling back to a sibling extension.

    Lets config name either tenant-list.md or tenant-list.txt without the caller
    caring which one is actually maintained. ``aliases`` folds roster spellings of
    one firm together before dedupe, so the variants collapse rather than being
    searched separately.
    """
    candidates = [path]
    for other in (".md", ".txt"):
        if path.suffix != other:
            candidates.append(path.with_suffix(other))

    for candidate in candidates:
        if candidate.exists():
            entries = parse_watchlist(candidate.read_text(encoding="utf-8"))
            return dedupe(canonicalize(entries, aliases))
    return []


def read_watchlist(path: Path) -> list[tuple[str, str]]:
    """(name, city) pairs - the narrow view used by the search-only callers."""
    return [(e.name, e.city) for e in read_entries(path)]
