"""History-store tests. No network, no API key, temp file per test.

    python -m unittest discover -s tests -v

The two that matter are test_unseen_* and test_pending_*: cross-run dedupe is
what stops a URL being judged twice, and the unsent queue is what stops an
email-driven build resending a finding every run.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402


def article(url: str, title: str = "t") -> dict:
    """An article shaped the way search.py emits them."""
    import search

    return {
        "url_hash": search.url_hash(url),
        "source_url": url,
        "source_name": search.source_name(url),
        "title": title,
        "published_date": "2026-08-20",
    }


def alert(art: dict, **over) -> dict:
    base = {
        "category": "office_move",
        "priority": "high",
        "headline": "headline",
        "client_summary": "summary",
        "confidence": 0.9,
        "ceo_flag": False,
        "source_name": art["source_name"],
        "source_url": art["source_url"],
        "url_hash": art["url_hash"],
        "published_date": art["published_date"],
    }
    base.update(over)
    return base


ENTITY = {"display_name": "Acme Corp", "city": "San Diego", "rank": 3, "segment": "Office"}


class DbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = db.connect(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    # -- schema ------------------------------------------------------------

    def test_connect_is_rerunnable(self):
        """Opening an existing db must be a no-op, not an error."""
        again = db.connect(self.tmp.name)
        self.assertEqual(db.stats(again)["runs"], 0)
        again.close()

    def test_tables_exist(self):
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        names = {r["name"] for r in rows}
        self.assertTrue({"runs", "results", "alerts"} <= names, names)

    # -- dedupe ------------------------------------------------------------

    def test_unseen_passes_everything_on_an_empty_db(self):
        arts = [article("https://x.com/a"), article("https://x.com/b")]
        self.assertEqual(len(db.unseen(self.conn, "tenants", "Acme Corp", arts)), 2)

    def test_unseen_filters_urls_from_an_earlier_run(self):
        first = article("https://x.com/a")
        rid = db.start_run(self.conn, "tenants")
        db.record(self.conn, rid, "tenants", ENTITY, [first], [])

        second = article("https://x.com/b")
        keep = db.unseen(self.conn, "tenants", "Acme Corp", [first, second])
        self.assertEqual([a["source_url"] for a in keep], ["https://x.com/b"])

    def test_unseen_is_scoped_per_entity_and_report_type(self):
        art = article("https://x.com/a")
        rid = db.start_run(self.conn, "tenants")
        db.record(self.conn, rid, "tenants", ENTITY, [art], [])
        # same URL, different entity -> still worth judging
        self.assertEqual(len(db.unseen(self.conn, "tenants", "Other Co", [art])), 1)
        # same URL, other report type -> also still worth judging
        self.assertEqual(len(db.unseen(self.conn, "competitors", "Acme Corp", [art])), 1)

    def test_tracking_params_do_not_defeat_dedupe(self):
        """canonical_url strips utm_*, so the same story is one row."""
        plain = article("https://x.com/a")
        tagged = article("https://x.com/a?utm_source=news")
        rid = db.start_run(self.conn, "tenants")
        db.record(self.conn, rid, "tenants", ENTITY, [plain], [])
        self.assertEqual(db.unseen(self.conn, "tenants", "Acme Corp", [tagged]), [])

    def test_unseen_keeps_articles_with_no_hash(self):
        self.assertEqual(len(db.unseen(self.conn, "tenants", "Acme Corp", [{"title": "x"}])), 1)

    # -- recording ---------------------------------------------------------

    def test_record_stores_articles_and_alerts(self):
        art = article("https://x.com/a")
        rid = db.start_run(self.conn, "tenants")
        self.assertEqual(db.record(self.conn, rid, "tenants", ENTITY, [art], [alert(art)]), 1)
        s = db.stats(self.conn)
        self.assertEqual((s["articles_seen"], s["alerts"], s["unsent"]), (1, 1, 1))

    def test_record_marks_excluded_articles_not_relevant(self):
        kept, dropped = article("https://x.com/a"), article("https://x.com/b")
        rid = db.start_run(self.conn, "tenants")
        db.record(self.conn, rid, "tenants", ENTITY, [kept, dropped], [alert(kept)])
        rows = {r["source_url"]: r["is_relevant"] for r in
                self.conn.execute("SELECT source_url, is_relevant FROM results")}
        self.assertEqual(rows["https://x.com/a"], 1)
        self.assertEqual(rows["https://x.com/b"], 0)

    def test_record_twice_does_not_double_report(self):
        art = article("https://x.com/a")
        rid = db.start_run(self.conn, "tenants")
        db.record(self.conn, rid, "tenants", ENTITY, [art], [alert(art)])
        self.assertEqual(db.record(self.conn, rid, "tenants", ENTITY, [art], [alert(art)]), 0)
        self.assertEqual(db.stats(self.conn)["alerts"], 1)

    def test_ceo_flag_round_trips(self):
        art = article("https://x.com/a")
        rid = db.start_run(self.conn, "tenants")
        db.record(self.conn, rid, "tenants", ENTITY, [art], [alert(art, ceo_flag=True)])
        self.assertEqual(db.stats(self.conn)["ceo_flagged"], 1)

    def test_new_categories_are_accepted(self):
        """No CHECK constraint: a config-driven taxonomy must not need an ALTER."""
        rid = db.start_run(self.conn, "competitors")
        for i, cat in enumerate(("pricing_valuation_comps", "corporate_investor_events",
                                 "ma_ownership_change")):
            art = article(f"https://x.com/{i}")
            db.record(self.conn, rid, "competitors", ENTITY, [art], [alert(art, category=cat)])
        self.assertEqual(db.stats(self.conn)["alerts"], 3)

    def test_alerts_without_url_hash_are_still_distinct(self):
        """judge.py emits source_url but no url_hash; db must derive it or the
        UNIQUE constraint would collapse every finding for an entity into one."""
        a, b = article("https://x.com/a"), article("https://x.com/b")
        raw = [{k: v for k, v in alert(x).items() if k != "url_hash"} for x in (a, b)]
        rid = db.start_run(self.conn, "tenants")
        self.assertEqual(db.record(self.conn, rid, "tenants", ENTITY, [a, b], raw), 2)
        hashes = {r["url_hash"] for r in self.conn.execute("SELECT url_hash FROM alerts")}
        self.assertEqual(len(hashes), 2)
        self.assertNotIn("", hashes)

    # -- the send queue ----------------------------------------------------

    def test_pending_returns_unsent_then_mark_emailed_clears_it(self):
        rid = db.start_run(self.conn, "tenants")
        arts = [article(f"https://x.com/{i}") for i in range(3)]
        db.record(self.conn, rid, "tenants", ENTITY, arts, [alert(a) for a in arts])

        rows = db.pending(self.conn, priorities=("high",))
        self.assertEqual(len(rows), 3)
        db.mark_emailed(self.conn, [r["alert_id"] for r in rows])
        self.assertEqual(db.pending(self.conn, priorities=("high",)), [])
        self.assertEqual(db.stats(self.conn)["unsent"], 0)

    def test_pending_filters_by_priority(self):
        rid = db.start_run(self.conn, "tenants")
        hi, med = article("https://x.com/a"), article("https://x.com/b")
        db.record(self.conn, rid, "tenants", ENTITY, [hi, med],
                  [alert(hi), alert(med, priority="medium")])
        self.assertEqual(len(db.pending(self.conn, priorities=("high",))), 1)
        self.assertEqual(len(db.pending(self.conn, priorities=("high", "medium"))), 2)

    def test_pending_ceo_only(self):
        rid = db.start_run(self.conn, "tenants")
        a, b = article("https://x.com/a"), article("https://x.com/b")
        db.record(self.conn, rid, "tenants", ENTITY, [a, b],
                  [alert(a, ceo_flag=True), alert(b)])
        self.assertEqual(len(db.pending(self.conn, ceo_only=True)), 1)

    def test_pending_orders_high_before_medium(self):
        rid = db.start_run(self.conn, "tenants")
        med, hi = article("https://x.com/a"), article("https://x.com/b")
        db.record(self.conn, rid, "tenants", ENTITY, [med, hi],
                  [alert(med, priority="medium"), alert(hi)])
        self.assertEqual([r["priority"] for r in
                          db.pending(self.conn, priorities=("high", "medium"))],
                         ["high", "medium"])

    # -- runs --------------------------------------------------------------

    def test_finish_run_stamps_counters(self):
        rid = db.start_run(self.conn, "tenants", lookback_days=7, model="m")
        db.finish_run(self.conn, rid, kept=4, dropped=11, entities_searched=2)
        row = db.recent_runs(self.conn)[0]
        self.assertEqual((row["kept"], row["dropped"], row["status"]), (4, 11, "complete"))
        self.assertIsNotNone(row["finished_at"])

    def test_finish_run_rejects_unknown_counters(self):
        rid = db.start_run(self.conn, "tenants")
        with self.assertRaises(ValueError):
            db.finish_run(self.conn, rid, bogus=1)

    def test_sweep_interrupted_closes_only_open_runs(self):
        done = db.start_run(self.conn, "tenants")
        db.finish_run(self.conn, done, kept=1)
        db.start_run(self.conn, "competitors")

        self.assertEqual(db.sweep_interrupted(self.conn), 1)
        rows = {r["run_id"]: r for r in db.recent_runs(self.conn)}
        self.assertEqual(rows[done]["status"], "complete")
        stale = [r for r in rows.values() if r["run_id"] != done][0]
        self.assertEqual(stale["status"], "interrupted")
        self.assertIsNotNone(stale["finished_at"])
        # Nothing left open, so a second sweep is a no-op.
        self.assertEqual(db.sweep_interrupted(self.conn), 0)

    # -- checkpoint / resume ------------------------------------------------

    def test_checkpoint_round_trips_the_judged_payload(self):
        rid = db.start_run(self.conn, "tenants")
        payload = {"display_name": "Acme", "alerts": [{"headline": "Acme signs 40k sq ft"}]}
        db.checkpoint(self.conn, rid, "Acme", payload=payload, articles=6, skipped=2, kept=1, dropped=3)

        saved = db.checkpoints(self.conn, rid)["Acme"]
        self.assertEqual(saved["payload"], payload)
        self.assertEqual((saved["articles"], saved["skipped"], saved["kept"], saved["dropped"]),
                         (6, 2, 1, 3))

    def test_checkpoint_records_an_entity_that_found_nothing(self):
        """Screened-and-empty is a finished entity. Storing it is what stops a
        resume paying to search the same dead ends again."""
        rid = db.start_run(self.conn, "tenants")
        db.checkpoint(self.conn, rid, "Quiet Co", articles=0)
        saved = db.checkpoints(self.conn, rid)
        self.assertIn("Quiet Co", saved)
        self.assertIsNone(saved["Quiet Co"]["payload"])

    def test_checkpoint_is_idempotent(self):
        rid = db.start_run(self.conn, "tenants")
        db.checkpoint(self.conn, rid, "Acme", payload={"display_name": "Acme"}, kept=1)
        db.checkpoint(self.conn, rid, "Acme", payload={"display_name": "Acme"}, kept=1)
        self.assertEqual(len(db.checkpoints(self.conn, rid)), 1)

    def test_checkpoints_are_scoped_to_their_run(self):
        first = db.start_run(self.conn, "tenants")
        second = db.start_run(self.conn, "tenants")
        db.checkpoint(self.conn, first, "Acme", kept=1)
        db.checkpoint(self.conn, second, "Beta", kept=1)
        self.assertEqual(list(db.checkpoints(self.conn, first)), ["Acme"])
        self.assertEqual(list(db.checkpoints(self.conn, second)), ["Beta"])

    def test_resumable_lists_only_interrupted_runs_with_progress(self):
        done = db.start_run(self.conn, "tenants")
        db.checkpoint(self.conn, done, "Acme", kept=1)
        db.finish_run(self.conn, done, kept=1)

        bare = db.start_run(self.conn, "competitors")  # died before any entity
        dead = db.start_run(self.conn, "tenants")
        db.checkpoint(self.conn, dead, "Beta", kept=2)
        db.sweep_interrupted(self.conn)

        ids = [r["run_id"] for r in db.resumable(self.conn)]
        self.assertEqual(ids, [dead])
        self.assertNotIn(done, ids)   # finished: nothing to resume
        self.assertNotIn(bare, ids)   # no entity screened: a fresh run is the same thing

    def test_resumable_ignores_a_paused_run(self):
        """A pause ended in the briefing the reviewer asked for. Resuming it would
        be continuing something that did not stop."""
        rid = db.start_run(self.conn, "tenants")
        db.checkpoint(self.conn, rid, "Acme", kept=1)
        db.finish_run(self.conn, rid, status="paused", kept=1)
        self.assertEqual(db.resumable(self.conn), [])

    def test_resumable_forgets_stale_runs(self):
        rid = db.start_run(self.conn, "tenants")
        db.checkpoint(self.conn, rid, "Acme", kept=1)
        db.sweep_interrupted(self.conn)
        self.conn.execute("UPDATE runs SET started_at = ? WHERE run_id = ?",
                          ("2020-01-01T00:00:00+00:00", rid))
        self.conn.commit()
        self.assertEqual(db.resumable(self.conn), [])

    def test_reopen_run_keeps_the_original_start(self):
        rid = db.start_run(self.conn, "tenants")
        started = db.run_row(self.conn, rid)["started_at"]
        db.finish_run(self.conn, rid, status="interrupted")

        self.assertTrue(db.reopen_run(self.conn, rid))
        row = db.run_row(self.conn, rid)
        self.assertEqual(row["status"], "running")
        self.assertIsNone(row["finished_at"])
        self.assertEqual(row["started_at"], started)

    def test_reopen_run_reports_a_missing_row(self):
        self.assertFalse(db.reopen_run(self.conn, "nope"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
