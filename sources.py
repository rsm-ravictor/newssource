"""Source policy: which outlet a URL belongs to, and whether it may be cited.

Tier position does two jobs, and they are deliberately different jobs:

    ordering    a lower position reads first inside a priority band (render.py)
    citation    a position worse than `citable_max_position` may never be the
                link a briefing hands the reader (judge.py)

Both read the same `source_tiers` block in config/report_types.yaml, so the
config stays the single place the outlet policy is written down. Living in its
own module rather than in render.py keeps the judge from importing the renderer
just to learn what a domain is worth; render re-exports these names so existing
callers and tests are unaffected.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

# Where a source lands when config/report_types.yaml has no source_tiers block at
# all: one flat tier, so ordering falls back to exactly the pre-tier behaviour.
FLAT_TIER = 0

# With no configured line, every source may be cited - the pre-gating behaviour.
UNGATED = 10**6


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


# Words that do not help identify a firm's own domain: kilroyrealty.com and
# kilroy.com are both Kilroy, and "the Irvine Company" publishes at irvinecompany.com.
_NOISE_WORDS = {
    "the", "and", "inc", "llc", "lp", "llp", "plc", "co", "corp", "corporation",
    "company", "companies", "group", "holdings", "holding", "trust", "reit",
    "properties", "property", "realty", "real", "estate", "partners", "partnership",
    "capital", "management", "investments", "investment", "development",
    "developers", "ventures", "international", "global", "usa", "us",
}


def own_domain(url: str, entity_name: str) -> bool:
    """Is this URL the entity's own site?

    A company's investor-relations page or newsroom is the *primary* record of its
    own announcement - often better than a wire restating it - but it lives on a
    domain no tier list can enumerate in advance. So it is recognised by name:
    ``investors.autodesk.com`` is Autodesk's, ``adsknews.autodesk.com`` too.

    Deliberately strict about short cores. A two- or three-letter fragment would
    match half the web, so only cores of four characters or more count, and the
    core has to appear as a whole label in the host rather than anywhere in the
    string - which keeps "Irvine Company" off ``citywatchla.com`` and stops a name
    like "US Realty" matching every ``.us`` domain.
    """
    host = urlparse(url or "").netloc.lower().split(":")[0]
    if not host or not entity_name:
        return False
    words = [w for w in re.split(r"[^a-z0-9]+", entity_name.lower()) if w]
    cores = [w for w in words if w not in _NOISE_WORDS and len(w) >= 4]
    # Also try the whole name with separators removed: "kilroyrealty.com".
    joined = "".join(w for w in words if w not in {"the"})
    if len(joined) >= 6:
        cores.append(joined)
    labels = host.split(".")
    return any(core in labels for core in cores)


def citable_limit(config: dict) -> int:
    """The worst tier position still allowed to carry a finding.

    Absent from the config means ungated: every source may be cited, which is how
    this behaved before citation gating existed.
    """
    block = config.get("source_tiers") or {}
    value = block.get("citable_max_position")
    return UNGATED if value is None else int(value)


def is_citable(
    url: str, lookup: dict[str, int], default: int, limit: int, entity_name: str = ""
) -> bool:
    """May this URL be the link the briefing shows?

    A social post or an AI answer page is a fine way to *find* a story and a poor
    way to *evidence* it, so discovery and citation are separate questions. With no
    tier lookup configured everything is citable, matching the ungated default.

    The entity's own domain is always citable: an IR release or company newsroom is
    the primary record of its own announcement, and gating it out in the name of
    "primary sources" would be exactly backwards. It is still tiered as
    company-issued for reading order, so a wire report of the same event leads.
    """
    if not lookup:
        return True
    if entity_name and own_domain(url, entity_name):
        return True
    return source_tier(url, lookup, default) <= limit
