"""Source tier ordering: reliable outlets lead, nothing is ever dropped.

The rule these tests pin is that tier is the THIRD sort key, never the first.
Severity and roster rank stay the rubric's axes; tier only settles a tie inside
them. A test that lets tier outrank priority is testing the wrong product.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from render import (  # noqa: E402
    FLAT_TIER,
    build_groups,
    build_top_intel,
    load_config,
    source_tier,
    source_tiers,
)

CFG = load_config()
TIERS = source_tiers(CFG)


def alert(url, priority="high", headline="h"):
    return {
        "category": "office_move", "priority": priority, "headline": headline,
        "client_summary": "s", "confidence": 0.9, "ceo_flag": False,
        "source_name": "n", "source_url": url, "published_date": "2026-08-25",
    }


def entity(name, alerts, rank=None):
    return {"display_name": name, "city": "San Diego", "rank": rank,
            "segment": "", "alerts": alerts}


class TierLookup(unittest.TestCase):
    def test_config_block_parses(self):
        lookup, default = TIERS
        self.assertTrue(lookup, "config/report_types.yaml has no source_tiers domains")
        self.assertEqual(default, CFG["source_tiers"]["default_position"])

    def test_known_domains_rank_in_written_order(self):
        lookup, default = TIERS
        wire = source_tier("https://reuters.com/x", lookup, default)
        trade = source_tier("https://bisnow.com/x", lookup, default)
        social = source_tier("https://facebook.com/x", lookup, default)
        self.assertLess(wire, trade)
        self.assertLess(trade, social)

    def test_unlisted_domain_gets_default_and_beats_social(self):
        lookup, default = TIERS
        unlisted = source_tier("https://some-local-paper.com/x", lookup, default)
        self.assertEqual(unlisted, default)
        self.assertLess(unlisted, source_tier("https://facebook.com/x", lookup, default))

    def test_subdomain_matches_parent(self):
        lookup, default = TIERS
        parent = source_tier("https://facebook.com/x", lookup, default)
        for host in ("https://m.facebook.com/x", "https://web.facebook.com/x",
                     "https://www.facebook.com/x"):
            self.assertEqual(source_tier(host, lookup, default), parent, host)

    def test_no_config_means_flat(self):
        self.assertEqual(source_tier("https://facebook.com/x", {}, 4), FLAT_TIER)


class Ordering(unittest.TestCase):
    def test_tier_orders_within_one_priority(self):
        e = entity("Acme", [alert("https://facebook.com/p", headline="social"),
                            alert("https://reuters.com/p", headline="wire")])
        groups = build_groups([e], ["high"], tiers=TIERS)
        self.assertEqual([a["headline"] for a in groups[0]["alerts"]], ["wire", "social"])

    def test_priority_still_outranks_tier(self):
        """An urgent social post leads a routine wire story. The whole point."""
        e = entity("Acme", [alert("https://reuters.com/p", "medium", "wire-medium"),
                            alert("https://facebook.com/p", "high", "social-high")])
        groups = build_groups([e], ["high", "medium"], tiers=TIERS)
        self.assertEqual([a["headline"] for a in groups[0]["alerts"]],
                         ["social-high", "wire-medium"])

    def test_roster_rank_still_outranks_tier(self):
        top = entity("Top", [alert("https://facebook.com/p", headline="rank3-social")], rank=3)
        low = entity("Low", [alert("https://reuters.com/p", headline="rank400-wire")], rank=400)
        groups = build_groups([low, top], ["high"], tiers=TIERS)
        self.assertEqual([g["display_name"] for g in groups], ["Top", "Low"])

    def test_tier_orders_unranked_entities(self):
        a = entity("SocialFirm", [alert("https://facebook.com/p")])
        b = entity("WireFirm", [alert("https://reuters.com/p")])
        groups = build_groups([a, b], ["high"], tiers=TIERS)
        self.assertEqual([g["display_name"] for g in groups], ["WireFirm", "SocialFirm"])

    def test_nothing_is_dropped_for_its_source(self):
        e = entity("Acme", [alert("https://facebook.com/1", headline="s1"),
                            alert("https://facebook.com/2", headline="s2")])
        groups = build_groups([e], ["high"], tiers=TIERS)
        self.assertEqual(len(groups[0]["alerts"]), 2)

    def test_top_intel_prefers_reliable_within_a_band(self):
        a = entity("A", [alert("https://facebook.com/p", headline="social")])
        b = entity("B", [alert("https://costar.com/p", headline="trade")])
        rows = build_top_intel(build_groups([a, b], ["high"], tiers=TIERS), tiers=TIERS)
        self.assertEqual(rows[0]["headline"], "trade")

    def test_default_tiers_none_preserves_old_order(self):
        """No tiers passed -> byte-identical to the pre-tier behaviour."""
        e = entity("Acme", [alert("https://facebook.com/p", headline="first"),
                            alert("https://reuters.com/p", headline="second")])
        groups = build_groups([e], ["high"])
        self.assertEqual([a["headline"] for a in groups[0]["alerts"]], ["first", "second"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
