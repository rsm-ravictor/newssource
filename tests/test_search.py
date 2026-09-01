"""Search-stage tests. No network, no API key required.

    python -m unittest discover -s tests -v

The privacy tests are the important ones: they assert that a built query contains
the entity name and city and NOTHING else from the portfolio. If someone later adds
a placeholder to query_templates in config/report_types.yaml, these fail.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from search import (  # noqa: E402
    build_queries,
    canonical_url,
    format_published,
    period_label,
    source_name,
    url_hash,
)

CONFIG = yaml.safe_load((ROOT / "config" / "report_types.yaml").read_text(encoding="utf-8"))

# Values that exist in a rent roll and must never reach a query.
FORBIDDEN = [
    "12500",          # monthly rent
    "$",
    "Suite 400",
    "1200 Prospect St",
    "lease",          # only as a rent-roll field name; see note in test
]


class TestQueryPrivacy(unittest.TestCase):
    def test_templates_use_only_name_and_city(self):
        """No template may reference any placeholder other than name/city."""
        import string

        for key, spec in CONFIG["report_types"].items():
            for tpl in spec["query_templates"]:
                fields = {f for _, f, _, _ in string.Formatter().parse(tpl) if f}
                self.assertTrue(
                    fields <= {"name", "city"},
                    f"{key}: template exposes extra placeholders {fields - {'name', 'city'}}",
                )

    def test_built_query_contains_only_supplied_values(self):
        """A query is name + city + fixed keywords; nothing else is interpolated."""
        for key, spec in CONFIG["report_types"].items():
            queries = build_queries("Meridian Health Partners", "San Diego", spec["query_templates"])
            self.assertEqual(len(queries), len(spec["query_templates"]))
            for q in queries:
                self.assertIn("Meridian Health Partners", q)
                self.assertIn("San Diego", q)
                for bad in ("12500", "Suite 400", "1200 Prospect St"):
                    self.assertNotIn(bad, q, f"{key}: leaked {bad!r}")

    def test_city_can_be_withheld(self):
        spec = CONFIG["report_types"]["tenants"]
        queries = build_queries("Calyx Biosciences", "La Jolla", spec["query_templates"], include_city=False)
        for q in queries:
            self.assertIn("Calyx Biosciences", q)
            self.assertNotIn("La Jolla", q)

    def test_no_double_spaces_when_city_missing(self):
        spec = CONFIG["report_types"]["tenants"]
        for q in build_queries("Acme Co", "", spec["query_templates"]):
            self.assertNotIn("  ", q)
            self.assertFalse(q.endswith(" "))


class TestCanonicalUrl(unittest.TestCase):
    def test_strips_tracking_params(self):
        dirty = "https://www.Example.com/story?utm_source=x&utm_medium=y&id=42&fbclid=abc#top"
        self.assertEqual(canonical_url(dirty), "https://example.com/story?id=42")

    def test_strips_fragment_and_trailing_slash(self):
        self.assertEqual(canonical_url("https://example.com/a/b/#section"), "https://example.com/a/b")

    def test_same_story_different_tracking_hashes_equal(self):
        a = "https://example.com/news/deal?utm_campaign=news"
        b = "https://www.example.com/news/deal/?gclid=999"
        self.assertEqual(url_hash(a), url_hash(b))

    def test_different_stories_differ(self):
        self.assertNotEqual(
            url_hash("https://example.com/a"),
            url_hash("https://example.com/b"),
        )

    def test_hash_is_short_and_stable(self):
        h = url_hash("https://example.com/x")
        self.assertEqual(len(h), 16)
        self.assertEqual(h, url_hash("https://example.com/x"))

    def test_keeps_meaningful_query(self):
        self.assertIn("id=7", canonical_url("https://example.com/p?id=7&utm_term=z"))


class TestSourceName(unittest.TestCase):
    def test_known_domain_fixups(self):
        self.assertEqual(source_name("https://www.wsj.com/articles/x"), "WSJ")
        self.assertEqual(source_name("https://therealdeal.com/x"), "The Real Deal")

    def test_unknown_domain_titlecased(self):
        self.assertEqual(source_name("https://coastalchronicle.com/x"), "Coastalchronicle")

    def test_strips_news_subdomain(self):
        self.assertEqual(source_name("https://news.example.com/x"), "Example")


class TestPublishedDate(unittest.TestCase):
    def test_rfc1123(self):
        self.assertEqual(format_published("Tue, 11 Aug 2026 20:53:03 GMT"), "Aug 11, 2026")

    def test_iso(self):
        self.assertEqual(format_published("2026-08-14T10:00:00Z"), "Aug 14, 2026")

    def test_empty_and_garbage(self):
        self.assertEqual(format_published(None), "")
        self.assertEqual(format_published(""), "")
        self.assertEqual(format_published("not a date"), "not a date")


class TestPeriodLabel(unittest.TestCase):
    def test_mentions_window(self):
        self.assertTrue(period_label(7).startswith("Last 7 days ("))
        self.assertIn("-", period_label(30))

    def test_one_day_reads_as_hours(self):
        # "Last 1 days" would ship in the email subject line.
        self.assertTrue(period_label(1).startswith("Last 24 hours ("))


if __name__ == "__main__":
    unittest.main(verbosity=2)
