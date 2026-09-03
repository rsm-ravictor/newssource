"""Runner tests: the schedule, and the password in front of it.

These matter because the runner is no longer necessarily local. A schedule that
silently never fires, or a page that answers a stranger, are the two ways a
hosted deployment goes wrong quietly.

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import base64
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, time as dt_time, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Before importing serve: it loads .env, and a DATABASE_URL there would point the
# whole test module at the real store. A temp file is the only thing these tests
# are allowed to touch.
_TMP = tempfile.TemporaryDirectory()
os.environ["NEWS_DB"] = str(Path(_TMP.name) / "test.db")

import serve  # noqa: E402


def tearDownModule():  # noqa: N802
    _TMP.cleanup()


class ScheduleTimeTest(unittest.TestCase):
    def test_parses_a_wall_clock_time(self):
        self.assertEqual(serve.parse_daily_time("07:30"), dt_time(7, 30))
        self.assertEqual(serve.parse_daily_time(" 23:05 "), dt_time(23, 5))

    def test_rejects_what_it_cannot_parse(self):
        """None means main() refuses to start. A schedule that never fires is
        worse than no schedule, because nobody notices."""
        for bad in ("nonsense", "25:00", "7", "", "07:60"):
            self.assertIsNone(serve.parse_daily_time(bad), bad)

    def test_next_occurrence_is_later_today_when_it_has_not_passed(self):
        now = datetime(2026, 9, 3, 14, 0, tzinfo=timezone.utc)
        self.assertEqual(
            serve.next_occurrence(dt_time(18, 0), now),
            datetime(2026, 9, 3, 18, 0, tzinfo=timezone.utc),
        )

    def test_next_occurrence_rolls_to_tomorrow_once_it_has(self):
        now = datetime(2026, 9, 3, 14, 0, tzinfo=timezone.utc)
        self.assertEqual(
            serve.next_occurrence(dt_time(7, 30), now),
            datetime(2026, 9, 4, 7, 30, tzinfo=timezone.utc),
        )

    def test_the_exact_minute_counts_as_passed(self):
        """Otherwise a tick landing precisely on the hour would schedule itself
        for the same instant and spin."""
        now = datetime(2026, 9, 3, 7, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(
            serve.next_occurrence(dt_time(7, 0), now),
            datetime(2026, 9, 4, 7, 0, tzinfo=timezone.utc),
        )


class PasswordTest(unittest.TestCase):
    """A real server on a real socket. The auth check is worth exercising through
    the actual request path rather than by calling the method."""

    PASSWORD = "correct-horse-battery-staple"

    @classmethod
    def setUpClass(cls):
        cls.previous = serve.PASSWORD
        serve.PASSWORD = cls.PASSWORD
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        serve.PASSWORD = cls.previous

    def get(self, path="/", password=None):
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
        if password is not None:
            token = base64.b64encode(f"any:{password}".encode()).decode()
            request.add_header("Authorization", f"Basic {token}")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, dict(response.headers)
        except urllib.error.HTTPError as err:
            return err.code, dict(err.headers)

    def test_no_credentials_is_challenged(self):
        status, headers = self.get()
        self.assertEqual(status, 401)
        self.assertIn("Basic", headers.get("WWW-Authenticate", ""))

    def test_wrong_password_is_refused(self):
        self.assertEqual(self.get(password="hunter2")[0], 401)

    def test_right_password_is_let_through(self):
        self.assertEqual(self.get(password=self.PASSWORD)[0], 200)

    def test_the_api_is_behind_the_same_gate(self):
        """The page is not the sensitive part - the endpoints that spend money
        and return findings are."""
        for path in ("/api/resumable", "/api/status?id=x", "/reference"):
            self.assertEqual(self.get(path)[0], 401, path)

    def test_a_password_prefix_is_not_enough(self):
        """compare_digest, not startswith."""
        self.assertEqual(self.get(password=self.PASSWORD[:-1])[0], 401)


class NoPasswordTest(unittest.TestCase):
    def test_unset_password_means_open(self):
        """Local use is unchanged: no password, no prompt. main() is what refuses
        to pair that with a public interface."""
        previous = serve.PASSWORD
        serve.PASSWORD = None
        try:
            server = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as response:
                    self.assertEqual(response.status, 200)
            finally:
                server.shutdown()
                server.server_close()
        finally:
            serve.PASSWORD = previous


if __name__ == "__main__":
    unittest.main(verbosity=2)
