"""Dialect-layer tests: the handful of places SQLite and Postgres differ.

No server needed. The Postgres side is checked by handing the helpers a fake
connection that records the SQL it is given, which is enough to pin the things
that actually break a port - placeholder style, conflict clauses, and the column
check behind migrate().

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.rowcount = 1

    def __iter__(self):
        return iter(self.rows)


class FakePg:
    """A connection that answers "postgres" and remembers every statement."""

    dialect = "postgres"

    def __init__(self, rows=()):
        self.sql: list[str] = []
        self.args: list[tuple] = []
        self._rows = rows

    def execute(self, sql, args=()):
        self.sql.append(sql)
        self.args.append(tuple(args))
        return FakeCursor(self._rows)


class QmarkTest(unittest.TestCase):
    def test_placeholders_are_translated(self):
        self.assertEqual(
            db.qmarks_to_percent("SELECT * FROM t WHERE a = ? AND b = ?"),
            "SELECT * FROM t WHERE a = %s AND b = %s",
        )

    def test_a_question_mark_inside_a_string_is_left_alone(self):
        """The literal is data, not a parameter. Translating it would change the
        value the query compares against."""
        self.assertEqual(
            db.qmarks_to_percent("SELECT * FROM t WHERE title = 'what? really' AND x = ?"),
            "SELECT * FROM t WHERE title = 'what? really' AND x = %s",
        )

    def test_percent_is_doubled(self):
        """psycopg reads % as its own escape, wherever it appears."""
        self.assertEqual(
            db.qmarks_to_percent("SELECT * FROM t WHERE n LIKE '%x%' AND y = ?"),
            "SELECT * FROM t WHERE n LIKE '%%x%%' AND y = %s",
        )


class InsertTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_sqlite_update_overwrites_the_row(self):
        for report_type in ("tenants", "competitors"):
            db.insert(self.conn, "runs", {
                "run_id": "r1", "report_type": report_type,
                "started_at": db.now(), "status": "running",
            }, on_conflict="update", key=("run_id",))
        self.assertEqual(db.run_row(self.conn, "r1")["report_type"], "competitors")
        self.assertEqual(len(db.recent_runs(self.conn)), 1)

    def test_sqlite_ignore_does_not_raise_on_a_repeat(self):
        row = {"report_type": "tenants", "entity_name": "Acme", "url_hash": "h1",
               "source_url": "https://x", "first_seen_at": db.now()}
        self.assertEqual(db.insert(self.conn, "results", row), 1)
        self.assertEqual(db.insert(self.conn, "results", row), 0)

    def test_postgres_ignore_becomes_a_trailing_clause(self):
        conn = FakePg()
        db.insert(conn, "results", {"a": 1, "b": 2})
        sql = conn.sql[0]
        self.assertIn("INSERT INTO results (a, b) VALUES (?, ?)", sql)
        self.assertTrue(sql.endswith("ON CONFLICT DO NOTHING"))
        self.assertNotIn("INSERT OR", sql)

    def test_postgres_update_names_the_conflicting_columns(self):
        conn = FakePg()
        db.insert(conn, "run_progress", {"run_id": "r1", "entity_name": "Acme", "kept": 2},
                  on_conflict="update", key=("run_id", "entity_name"))
        sql = conn.sql[0]
        self.assertIn("ON CONFLICT (run_id, entity_name) DO UPDATE SET", sql)
        # The key identifies the row; overwriting it with itself is noise.
        self.assertIn("kept = EXCLUDED.kept", sql)
        self.assertNotIn("run_id = EXCLUDED.run_id", sql)


class SchemaTest(unittest.TestCase):
    def test_postgres_schema_is_the_sqlite_one_retyped(self):
        """Derived, not copied: same shape, one line different. If this fails,
        the two stores have started to drift."""
        sqlite_lines = db.SQLITE_SCHEMA.splitlines()
        pg_lines = db.pg_schema().splitlines()
        self.assertEqual(len(sqlite_lines), len(pg_lines))
        differing = [(a, b) for a, b in zip(sqlite_lines, pg_lines) if a != b]
        self.assertEqual(len(differing), 1)
        self.assertIn("AUTOINCREMENT", differing[0][0])
        self.assertIn("IDENTITY", differing[0][1])

    def test_postgres_schema_has_no_sqlite_only_syntax(self):
        self.assertNotIn("AUTOINCREMENT", db.pg_schema())


class ColumnsOfTest(unittest.TestCase):
    def test_postgres_reads_information_schema(self):
        conn = FakePg(rows=[{"name": "kept"}, {"name": "dropped"}])
        self.assertEqual(db.columns_of(conn, "runs"), {"kept", "dropped"})
        self.assertIn("information_schema.columns", conn.sql[0])
        self.assertEqual(conn.args[0], ("runs",))

    def test_sqlite_reads_pragma(self):
        tmp = tempfile.TemporaryDirectory()
        conn = db.connect(Path(tmp.name) / "t.db")
        self.assertIn("run_id", db.columns_of(conn, "runs"))
        self.assertEqual(db.columns_of(conn, "no_such_table"), set())
        conn.close()
        tmp.cleanup()


class RoutingTest(unittest.TestCase):
    """Which store a connection actually opens. The test suite must never be one
    stray environment variable away from writing to the real history."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = os.environ.get("DATABASE_URL")

    def tearDown(self):
        if self.saved is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self.saved
        self.tmp.cleanup()

    def test_an_explicit_path_beats_the_environment(self):
        os.environ["DATABASE_URL"] = "postgresql://nobody@nowhere/db"
        conn = db.connect(Path(self.tmp.name) / "t.db")
        self.assertEqual(conn.dialect, "sqlite")
        conn.close()

    def test_news_db_outranks_a_connection_string(self):
        """Someone pointing NEWS_DB at a scratch file means that file, whatever
        .env holds. Silently using Postgres instead would write test rows into
        the real store."""
        os.environ["DATABASE_URL"] = "postgresql://nobody@nowhere/db"
        os.environ["NEWS_DB"] = str(Path(self.tmp.name) / "scratch.db")
        try:
            conn = db.connect()
            self.assertEqual(conn.dialect, "sqlite")
            conn.close()
        finally:
            os.environ.pop("NEWS_DB", None)

    def test_news_db_still_selects_a_file(self):
        os.environ.pop("DATABASE_URL", None)
        os.environ["NEWS_DB"] = str(Path(self.tmp.name) / "env.db")
        try:
            conn = db.connect()
            self.assertEqual(conn.dialect, "sqlite")
            self.assertTrue(Path(os.environ["NEWS_DB"]).exists())
            conn.close()
        finally:
            os.environ.pop("NEWS_DB", None)

    def test_a_missing_driver_is_explained(self):
        """The message has to say what to do; "No module named psycopg" from
        inside a run does not."""
        import builtins

        real = builtins.__import__

        def blocked(name, *a, **kw):
            if name == "psycopg":
                raise ModuleNotFoundError("No module named 'psycopg'")
            return real(name, *a, **kw)

        builtins.__import__ = blocked
        try:
            with self.assertRaises(RuntimeError) as caught:
                db.connect(url="postgresql://nobody@nowhere/db")
            self.assertIn("psycopg[binary]", str(caught.exception))
        finally:
            builtins.__import__ = real


class DescribeTest(unittest.TestCase):
    def test_the_password_never_reaches_the_terminal(self):
        conn = FakePg()
        conn.dsn = "postgresql://user:hunter2@ep-x.aws.neon.tech/neondb?sslmode=require"
        line = db.describe(conn)
        self.assertNotIn("hunter2", line)
        self.assertIn("ep-x.aws.neon.tech", line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
