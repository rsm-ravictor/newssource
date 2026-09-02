"""The evidence standard: dated, deduped, citable, and new.

These pin the four rules the reviewer asked for after reading a live briefing,
each of which the model cannot enforce on its own:

    an event is dated by when it HAPPENED, not when it was written about
    one underlying event is one item, whoever else covered it
    the cited link is a source you would show a client
    a development already reported is not reported again

The recurring trap in all four is over-reach. Gating must not quietly discard a
finding that has a good source available, consolidating must not downgrade an
event to its mildest reading, and reserving a briefing seat must not lower the
bar a finding has to clear. Each of those has a test here.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402
from judge import consolidate, parse_event_date  # noqa: E402
from render import build_top_intel  # noqa: E402
from sources import (  # noqa: E402
    UNGATED,
    citable_limit,
    is_citable,
    own_domain,
    source_tiers,
)
from watchlist import Entry, canonicalize  # noqa: E402

# A miniature tier config: one good outlet, one unlisted, one social.
CONFIG = {
    "source_tiers": {
        "default_position": 4,
        "citable_max_position": 3,
        "order": [
            {"label": "Wire", "domains": ["reuters.com"]},
            {"label": "Trade", "domains": ["bisnow.com"]},
            {"label": "Company", "domains": ["businesswire.com"]},
            {"label": "General", "domains": []},
            {"label": "Social", "domains": ["facebook.com", "instagram.com", "perplexity.ai"]},
        ],
    }
}

TIERS = source_tiers(CONFIG)
LIMIT = citable_limit(CONFIG)


def candidate(**over):
    """One judged article, shaped as judge_entities hands it to consolidate."""
    when = over.pop("when", date(2026, 8, 30))
    row = {
        "category": "acquisition",
        "priority": "medium",
        "headline": "Something happened",
        "client_summary": "",
        "deal_metrics": "",
        "confidence": 0.8,
        "ceo_flag": False,
        "event_date": when.isoformat(),
        "event_date_basis": "",
        "event_key": "one-event",
        "source_name": "Reuters",
        "source_url": "https://reuters.com/a",
        "published_date": "Aug 30, 2026",
        "event_date_parsed": when,
        "group_key": "one-event",
    }
    row.update(over)
    return row


class EventDates(unittest.TestCase):
    def test_iso_date_parses(self):
        self.assertEqual(parse_event_date("2026-08-30"), date(2026, 8, 30))

    def test_slashed_and_timestamped_forms_parse(self):
        self.assertEqual(parse_event_date("2026/08/30"), date(2026, 8, 30))
        self.assertEqual(parse_event_date("2026-08-30T14:00:00Z"), date(2026, 8, 30))

    def test_unusable_values_are_absent_not_guessed(self):
        # "last spring" must not become a date: an unpinned event is excluded
        # upstream, and inventing one here would defeat that.
        for raw in ("", "   ", "last spring", "Aug 2026", "not a date"):
            self.assertIsNone(parse_event_date(raw), raw)

    def test_event_before_the_window_is_dropped(self):
        """The '2026 article about 2025 layoffs' case."""
        rows, drops = consolidate(
            [candidate(when=date(2025, 6, 1), group_key="old")],
            tiers=TIERS,
            citable_max=LIMIT,
            window_start=date(2026, 8, 25),
        )
        self.assertEqual(rows, [])
        self.assertIn("before this window", " ".join(drops))

    def test_publication_inside_the_window_does_not_rescue_a_stale_event(self):
        stale = candidate(when=date(2019, 5, 1), group_key="old", published_date="Aug 31, 2026")
        rows, _ = consolidate([stale], tiers=TIERS, citable_max=LIMIT,
                              window_start=date(2026, 8, 25))
        self.assertEqual(rows, [])

    def test_no_window_keeps_everything_dated(self):
        """The offline fixture path passes no window and must still render."""
        rows, _ = consolidate([candidate(when=date(2019, 5, 1))], tiers=TIERS, citable_max=LIMIT)
        self.assertEqual(len(rows), 1)


class OneEventOneItem(unittest.TestCase):
    def test_articles_sharing_a_key_collapse_to_one(self):
        rows, _ = consolidate(
            [
                candidate(source_name="Bisnow", source_url="https://bisnow.com/x"),
                candidate(source_name="Reuters", source_url="https://reuters.com/y"),
            ],
            tiers=TIERS,
            citable_max=LIMIT,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["corroborations"], 1)

    def test_best_tiered_source_carries_the_story(self):
        rows, _ = consolidate(
            [
                candidate(source_name="Bisnow", source_url="https://bisnow.com/x"),
                candidate(source_name="Reuters", source_url="https://reuters.com/y"),
            ],
            tiers=TIERS,
            citable_max=LIMIT,
        )
        self.assertEqual(rows[0]["source_name"], "Reuters")

    def test_consolidating_keeps_the_most_severe_reading(self):
        """Merging must never downgrade: the cluster's high survives."""
        rows, _ = consolidate(
            [
                candidate(source_url="https://reuters.com/y", priority="low"),
                candidate(source_url="https://bisnow.com/x", priority="high", ceo_flag=True),
            ],
            tiers=TIERS,
            citable_max=LIMIT,
        )
        self.assertEqual(rows[0]["priority"], "high")
        self.assertTrue(rows[0]["ceo_flag"])

    def test_different_events_stay_separate(self):
        rows, _ = consolidate(
            [
                candidate(group_key="deal-a", source_url="https://reuters.com/a"),
                candidate(group_key="deal-b", source_url="https://reuters.com/b"),
            ],
            tiers=TIERS,
            citable_max=LIMIT,
        )
        self.assertEqual(len(rows), 2)

    def test_scratch_keys_do_not_reach_the_template(self):
        rows, _ = consolidate([candidate()], tiers=TIERS, citable_max=LIMIT)
        self.assertNotIn("event_date_parsed", rows[0])
        self.assertNotIn("group_key", rows[0])


class Citations(unittest.TestCase):
    def test_social_is_not_citable_but_wire_is(self):
        lookup, default = TIERS
        self.assertFalse(is_citable("https://instagram.com/p/1", lookup, default, LIMIT))
        self.assertFalse(is_citable("https://perplexity.ai/x", lookup, default, LIMIT))
        self.assertTrue(is_citable("https://reuters.com/a", lookup, default, LIMIT))

    def test_unlisted_domain_is_not_citable_at_this_line(self):
        lookup, default = TIERS
        self.assertFalse(is_citable("https://some-blog.example/x", lookup, default, LIMIT))

    def test_subdomain_inherits_its_parent(self):
        lookup, default = TIERS
        self.assertFalse(is_citable("https://m.facebook.com/p/1", lookup, default, LIMIT))

    def test_event_with_only_a_weak_source_is_dropped_and_named(self):
        rows, drops = consolidate(
            [candidate(source_name="Instagram", source_url="https://instagram.com/p/1")],
            tiers=TIERS,
            citable_max=LIMIT,
        )
        self.assertEqual(rows, [])
        self.assertIn("no citable source", " ".join(drops))
        self.assertIn("Instagram", " ".join(drops))

    def test_a_weak_source_is_not_wasted_when_a_good_one_shares_the_event(self):
        """Discovery still counts: the social hit brought a story a wire also has."""
        rows, _ = consolidate(
            [
                candidate(source_name="Instagram", source_url="https://instagram.com/p/1",
                          priority="high"),
                candidate(source_name="Reuters", source_url="https://reuters.com/y"),
            ],
            tiers=TIERS,
            citable_max=LIMIT,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_name"], "Reuters")
        self.assertEqual(rows[0]["priority"], "high")

    def test_the_entitys_own_domain_is_always_citable(self):
        """An IR release is the primary record - gating it out would be backwards."""
        lookup, default = TIERS
        for url in ("https://investors.autodesk.com/news", "https://adsknews.autodesk.com/x"):
            self.assertTrue(is_citable(url, lookup, default, LIMIT, "Autodesk"), url)

    def test_own_domain_matches_the_joined_name(self):
        self.assertTrue(own_domain("https://www.kilroyrealty.com/news", "Kilroy Realty"))

    def test_own_domain_does_not_match_a_coincidental_substring(self):
        """"Irvine Company" must not claim citywatchla.com or an unrelated host."""
        self.assertFalse(own_domain("https://citywatchla.com/x", "Irvine Company"))
        self.assertFalse(own_domain("https://gibsondunn.com/bio", "Jamestown"))
        self.assertFalse(own_domain("https://facebook.com/p/1", "Kimco Realty"))

    def test_own_domain_ignores_short_and_generic_words(self):
        # Nothing in "The US Realty Co" is a usable core, so it claims nothing.
        self.assertFalse(own_domain("https://realty.com/x", "The US Realty Co"))
        self.assertFalse(own_domain("https://co.uk/x", "The US Realty Co"))

    def test_a_law_firm_bio_is_not_citable(self):
        """The Jamestown case: the link went to an attorney bio page."""
        lookup, default = TIERS
        self.assertFalse(is_citable("https://gibsondunn.com/bio", lookup, default, LIMIT, "Jamestown"))

    def test_no_configured_line_gates_nothing(self):
        """Absent citable_max_position, behaviour is exactly the pre-gating one."""
        self.assertEqual(citable_limit({"source_tiers": {"order": []}}), UNGATED)
        rows, _ = consolidate(
            [candidate(source_url="https://instagram.com/p/1")],
            tiers=TIERS,
            citable_max=UNGATED,
        )
        self.assertEqual(len(rows), 1)


class FreshEvents(unittest.TestCase):
    """Cross-run suppression: reported once is reported once."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _record(self, key, url):
        alert = {
            "category": "acquisition", "priority": "high", "headline": "h",
            "client_summary": "", "confidence": 0.9, "ceo_flag": False,
            "source_name": "Reuters", "source_url": url, "published_date": "",
            "event_date": "2026-08-30", "event_key": key, "deal_metrics": "",
        }
        db.record(self.conn, "r1", "competitors", {"display_name": "Kilroy Realty"},
                  [{"source_url": url, "title": "t"}], [alert])

    def test_everything_is_new_on_an_empty_store(self):
        new, repeats = db.fresh_events(self.conn, "competitors", "Kilroy Realty",
                                       [{"event_key": "deal-a"}])
        self.assertEqual(len(new), 1)
        self.assertEqual(repeats, [])

    def test_same_event_from_a_different_url_is_a_repeat(self):
        self._record("deal-a", "https://reuters.com/first")
        new, repeats = db.fresh_events(
            self.conn, "competitors", "Kilroy Realty",
            [{"event_key": "deal-a", "source_url": "https://bisnow.com/second"}],
        )
        self.assertEqual(new, [])
        self.assertEqual(len(repeats), 1)

    def test_suppression_is_scoped_per_entity_and_report_type(self):
        self._record("deal-a", "https://reuters.com/first")
        for report_type, entity in (("tenants", "Kilroy Realty"), ("competitors", "Someone Else")):
            new, _ = db.fresh_events(self.conn, report_type, entity, [{"event_key": "deal-a"}])
            self.assertEqual(len(new), 1, f"{report_type}/{entity} should be unaffected")

    def test_a_finding_with_no_key_is_treated_as_new(self):
        self._record("deal-a", "https://reuters.com/first")
        new, repeats = db.fresh_events(self.conn, "competitors", "Kilroy Realty",
                                       [{"event_key": ""}])
        self.assertEqual(len(new), 1)
        self.assertEqual(repeats, [])

    def test_event_columns_round_trip(self):
        self._record("deal-a", "https://reuters.com/first")
        row = self.conn.execute("SELECT event_date, event_key FROM alerts").fetchone()
        self.assertEqual(row["event_date"], "2026-08-30")
        self.assertEqual(row["event_key"], "deal-a")

    def test_migration_is_idempotent(self):
        self.assertEqual(db.migrate(self.conn), [])


class ReservedSeats(unittest.TestCase):
    """Lower-ranked entities get read without lowering the bar."""

    @staticmethod
    def group(name, rank, priority="high"):
        return {
            "display_name": name, "slug": name.lower().replace(" ", "-"), "rank": rank,
            "alerts": [{"priority": priority, "headline": f"{name} item",
                        "source_url": "https://reuters.com/x"}],
        }

    def test_large_tenants_crowd_out_the_box_without_reservation(self):
        groups = [self.group(f"Big {i}", i) for i in range(1, 8)] + [self.group("Small", 430)]
        rows = build_top_intel(groups, limit=5)
        self.assertNotIn("Small", [r["company"] for r in rows])

    def test_reservation_promotes_a_lower_ranked_entity(self):
        groups = [self.group(f"Big {i}", i) for i in range(1, 8)] + [self.group("Small", 430)]
        rows = build_top_intel(groups, limit=5, reserve_beyond_rank=100, reserve_slots=2)
        self.assertIn("Small", [r["company"] for r in rows])
        self.assertEqual(len(rows), 5)

    def test_unused_seats_return_to_the_pool(self):
        groups = [self.group(f"Big {i}", i) for i in range(1, 8)]
        rows = build_top_intel(groups, limit=5, reserve_beyond_rank=100, reserve_slots=2)
        self.assertEqual(len(rows), 5)
        self.assertEqual([r["rank"] for r in rows], [1, 2, 3, 4, 5])

    def test_reservation_does_not_invent_findings(self):
        """A reserved seat is filled from real findings or not at all."""
        groups = [self.group("Big 1", 1)]
        rows = build_top_intel(groups, limit=5, reserve_beyond_rank=100, reserve_slots=2)
        self.assertEqual(len(rows), 1)

    def test_unranked_report_type_is_unaffected(self):
        groups = [self.group("Firm A", None), self.group("Firm B", None)]
        rows = build_top_intel(groups, limit=5, reserve_beyond_rank=100, reserve_slots=2)
        self.assertEqual(len(rows), 2)


class NameAliases(unittest.TestCase):
    ALIASES = {"Kilroy Realty": ["Kilroy", "KRC"]}

    def test_variants_fold_onto_the_canonical_name(self):
        entries = [Entry(name="Kilroy", city="San Diego"), Entry(name="KRC", city="Bellevue")]
        out = canonicalize(entries, self.ALIASES)
        self.assertEqual([e.name for e in out], ["Kilroy Realty", "Kilroy Realty"])

    def test_matching_ignores_case_and_punctuation(self):
        out = canonicalize([Entry(name="kilroy!")], self.ALIASES)
        self.assertEqual(out[0].name, "Kilroy Realty")

    def test_roster_spelling_is_preserved_for_the_reference_page(self):
        out = canonicalize([Entry(name="Kilroy", city="San Diego")], self.ALIASES)
        self.assertEqual(out[0].raw_name, "Kilroy")

    def test_renaming_does_not_merge_two_markets(self):
        """One firm in two markets stays two entries - their relevance differs."""
        entries = [Entry(name="Kilroy", city="San Diego"), Entry(name="Kilroy Realty", city="Bellevue")]
        out = canonicalize(entries, self.ALIASES)
        self.assertEqual(len(out), 2)
        self.assertEqual({e.city for e in out}, {"San Diego", "Bellevue"})

    def test_unlisted_names_are_untouched(self):
        out = canonicalize([Entry(name="DivcoWest")], self.ALIASES)
        self.assertEqual(out[0].name, "DivcoWest")
        self.assertEqual(out[0].raw_name, "")

    def test_no_alias_map_is_a_no_op(self):
        entries = [Entry(name="Kilroy")]
        self.assertEqual(canonicalize(entries, None), entries)


if __name__ == "__main__":
    unittest.main()
