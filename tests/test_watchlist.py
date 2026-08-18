"""Roster-parsing tests. No network, no API key required.

    python -m unittest discover -s tests -v

The rosters are hand-maintained Markdown, so the parser has to survive ordinary
document furniture - prose above a table, per-section tables, an empty column in
the middle of a row - without inventing entities or shifting fields. The rank tests
matter most: rank drives stage-2 prioritization, so a rank read off the wrong column
would quietly re-order the whole briefing.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from render import build_groups  # noqa: E402
from watchlist import (  # noqa: E402
    clean_name,
    dedupe,
    parse_watchlist,
    read_entries,
    read_watchlist,
)

TENANT_DOC = """# Tenant Roster

# Tenant Roster - Office

Tenant name and city. Some tenants appear more than once (multiple suites/leases).

| # | Tenant Name | City |
|---|---|---|
| 1 | GOOGLE | San Francisco |
| 2 | LPL HOLDINGS, INC | San Diego |
| 3 | DivcoWest | Portland |
| 24 | GOOGLE | San Francisco |

# Tenant Roster - Retail & Multi Unit

| # | Tenant Name | City |
|---|---|---|
| 1 | LOWE'S | Waipahu |
"""

COMPETITOR_DOC = """# AAT Competitor List by Market

Reference data on competing owners across core markets. Columns: Company | Category | Ticker | Notes.

## San Diego

| Company | Category | Ticker | Notes |
|---|---|---|---|
| Kilroy Realty | Office | KRC | Office including UTC competition |
| AvalonBay | Multifamily | | |

## Oahu / Waikiki

| Company | Category | Ticker | Notes |
|---|---|---|---|
| Kyo-ya Hotels | Hospitality | | Sheraton Waikiki |
"""


class TestTenantRoster(unittest.TestCase):
    def setUp(self):
        self.entries = parse_watchlist(TENANT_DOC)

    def test_rank_name_and_city_come_from_their_own_columns(self):
        first = self.entries[0]
        self.assertEqual(first.rank, 1)
        self.assertEqual(first.name, "Google")
        self.assertEqual(first.city, "San Francisco")

    def test_prose_line_with_pipes_is_not_an_entity(self):
        names = [e.name for e in self.entries]
        self.assertNotIn("Tenant name and city", names)
        self.assertTrue(all("appear more than once" not in n for n in names))

    def test_section_heading_is_the_segment_not_the_city(self):
        office = [e for e in self.entries if e.segment == "Office"]
        self.assertEqual(len(office), 4)
        self.assertEqual(self.entries[-1].segment, "Retail & Multi Unit")
        # "Tenant Roster - Office" must never be mistaken for a place.
        self.assertNotIn("Tenant Roster - Office", [e.city for e in self.entries])

    def test_rank_restarts_per_segment(self):
        retail = [e for e in self.entries if e.segment == "Retail & Multi Unit"]
        self.assertEqual(retail[0].rank, 1)
        self.assertEqual(retail[0].name, "Lowe's")

    def test_dedupe_keeps_the_best_rank(self):
        deduped = dedupe(self.entries)
        googles = [e for e in deduped if e.name == "Google"]
        self.assertEqual(len(googles), 1)
        self.assertEqual(googles[0].rank, 1)


class TestCompetitorRoster(unittest.TestCase):
    def setUp(self):
        self.entries = parse_watchlist(COMPETITOR_DOC)

    def test_city_is_inherited_from_the_market_heading(self):
        self.assertEqual(self.entries[0].name, "Kilroy Realty")
        self.assertEqual(self.entries[0].city, "San Diego")

    def test_columns_map_by_header_name(self):
        kilroy = self.entries[0]
        self.assertEqual(kilroy.category, "Office")
        self.assertEqual(kilroy.ticker, "KRC")
        self.assertIn("UTC", kilroy.notes)

    def test_an_empty_middle_column_does_not_shift_the_rest(self):
        avalon = self.entries[1]
        self.assertEqual(avalon.category, "Multifamily")
        self.assertEqual(avalon.ticker, "")
        self.assertEqual(avalon.city, "San Diego")

    def test_two_place_heading_yields_the_searchable_first_one(self):
        self.assertEqual(self.entries[-1].city, "Oahu")

    def test_competitors_carry_no_rank(self):
        self.assertTrue(all(e.rank is None for e in self.entries))

    def test_document_title_is_not_an_entity(self):
        self.assertNotIn("AAT Competitor List by Market", [e.name for e in self.entries])


class TestFlatShapes(unittest.TestCase):
    """The pre-roster formats still parse, so a pasted list keeps working."""

    def test_bullets_pipes_commas_and_bare_lines(self):
        entries = parse_watchlist(
            "- Kilroy Realty | San Diego\n"
            "* Simon Property Group, San Diego\n"
            "1. Regency Centers\n"
            "| Kimco | San Diego |\n"
            "Essex | San Diego\n"
        )
        self.assertEqual(
            [(e.name, e.city) for e in entries],
            [
                ("Kilroy Realty", "San Diego"),
                ("Simon Property Group", "San Diego"),
                ("Regency Centers", ""),
                ("Kimco", "San Diego"),
                ("Essex", "San Diego"),
            ],
        )

    def test_furniture_is_skipped(self):
        entries = parse_watchlist(
            "# Heading\n\n> quoted\n---\n```\nEssex | fenced\n```\n"
            "| Company | City |\n|---|---|\n| Kimco | San Diego |\n"
        )
        self.assertEqual([(e.name, e.city) for e in entries], [("Kimco", "San Diego")])

    def test_read_watchlist_still_returns_pairs(self):
        pairs = read_watchlist(ROOT / "data" / "watchlists" / "competitors.example.txt")
        self.assertTrue(pairs)
        self.assertTrue(all(isinstance(p, tuple) and len(p) == 2 for p in pairs))


class TestCleanName(unittest.TestCase):
    def test_shouted_names_are_titled_and_desuffixed(self):
        self.assertEqual(clean_name("LPL HOLDINGS, INC"), "LPL Holdings")
        self.assertEqual(clean_name("ZS ASSOCIATES, INC"), "ZS Associates")
        self.assertEqual(clean_name("STATE OF OREGON DEQ"), "State of Oregon DEQ")
        self.assertEqual(clean_name("H.G. FENTON"), "H.G. Fenton")

    def test_mixed_case_is_left_alone(self):
        # A human typed these; title-casing would mangle them and peeling "Company"
        # off a deliberately spelled-out name would change what gets searched.
        self.assertEqual(clean_name("DivcoWest"), "DivcoWest")
        self.assertEqual(clean_name("Queen Emma Land Company"), "Queen Emma Land Company")
        self.assertEqual(clean_name("Kyo-ya Hotels"), "Kyo-ya Hotels")

    def test_delimiters_never_survive(self):
        # A comma would be read back as a city and a pipe as the field separator.
        self.assertNotIn(",", clean_name("FOO, BAR, INC"))
        self.assertNotIn("|", clean_name("A | B"))


class TestRealRosters(unittest.TestCase):
    """Skipped when the private rosters are absent - they are gitignored."""

    def test_tenant_roster_is_ranked_and_deduped(self):
        path = ROOT / "tenant-list.md"
        if not path.exists():
            self.skipTest("tenant-list.md not present")
        entries = read_entries(path)
        self.assertTrue(entries)
        self.assertTrue(all(e.city for e in entries), "every tenant needs a city to search on")
        self.assertTrue(all(e.rank for e in entries), "every tenant needs a rank")
        keys = [(e.name.lower(), e.city.lower()) for e in entries]
        self.assertEqual(len(keys), len(set(keys)), "read_entries must dedupe")

    def test_competitor_roster_has_a_city_for_every_row(self):
        path = ROOT / "competitor-list.md"
        if not path.exists():
            self.skipTest("competitor-list.md not present")
        entries = read_entries(path)
        self.assertTrue(entries)
        self.assertTrue(all(e.city for e in entries), "city comes from the market heading")


class TestGroupDisambiguation(unittest.TestCase):
    """One firm can appear in several markets; their anchors must not collide."""

    @staticmethod
    def _entity(name, city, rank=None):
        return {"display_name": name, "city": city, "rank": rank,
                "alerts": [{"priority": "high"}]}

    def test_repeated_names_get_distinct_slugs_and_labels(self):
        groups = build_groups(
            [
                self._entity("Boston Properties", "San Francisco"),
                self._entity("Boston Properties", "Bellevue"),
                self._entity("Kilroy Realty", "San Diego"),
            ],
            ["high"],
        )
        slugs = [g["slug"] for g in groups]
        self.assertEqual(len(slugs), len(set(slugs)))
        repeated = [g for g in groups if g["display_name"] == "Boston Properties"]
        self.assertTrue(all(g["city"] in g["pill_label"] for g in repeated))
        # A unique name keeps the plain label and the plain slug.
        solo = next(g for g in groups if g["display_name"] == "Kilroy Realty")
        self.assertEqual(solo["pill_label"], "Kilroy Realty")
        self.assertEqual(solo["slug"], "kilroy-realty")

    def test_same_name_same_city_still_separates(self):
        groups = build_groups(
            [self._entity("Hines", "Portland"), self._entity("Hines", "Portland")],
            ["high"],
        )
        self.assertNotEqual(groups[0]["slug"], groups[1]["slug"])

    def test_rank_orders_entities_within_a_severity(self):
        groups = build_groups(
            [
                self._entity("Small Co", "San Diego", rank=400),
                self._entity("Big Co", "San Diego", rank=3),
            ],
            ["high"],
        )
        self.assertEqual([g["display_name"] for g in groups], ["Big Co", "Small Co"])


if __name__ == "__main__":
    unittest.main()
