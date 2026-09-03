"""Measured API usage.

Two things are being pinned here.

The first is arithmetic: totals add up, and a per-report-type delta is the
difference between two readings.

The second is the part that would silently break. The meter works by wrapping
the clients that get injected into ``utils.connect``, which is verbatim-locked -
so the wrapper has to satisfy the exact call shape that file uses,
``client.chat.completions.create(...)``, and pass everything else through
untouched. ``CallShape`` is the test that fails if connect.py's seam ever moves,
and the pass-through tests are what stop a wrapper from quietly narrowing the
client. Metering must also never be the reason a run dies, so the defensive
paths are tested too.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from usage import Meter  # noqa: E402


class FakeUsage:
    def __init__(self, prompt=100, completion=20, cached=0):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.prompt_tokens_details = type("D", (), {"cached_tokens": cached})()


class FakeResponse:
    def __init__(self, usage=None):
        self.usage = usage
        self.choices = [type("C", (), {"message": type("M", (), {"content": "{}"})()})()]


class FakeCompletions:
    def __init__(self, response=None, raises=None):
        self.response = response
        self.raises = raises
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises:
            raise self.raises
        return self.response


class FakeLLM:
    """Shaped like the OpenAI client: .chat.completions.create(...)."""

    def __init__(self, completions):
        self.chat = type("Chat", (), {"completions": completions})()
        self.api_key = "sk-test"

    def other_method(self):
        return "passed through"


class FakeTavily:
    def __init__(self, results=3, raises=None):
        self.results = results
        self.raises = raises
        self.calls = []

    def search(self, query, **kwargs):
        self.calls.append((query, kwargs))
        if self.raises:
            raise self.raises
        return {"results": [{"url": f"https://x/{i}"} for i in range(self.results)]}


class Totals(unittest.TestCase):
    def test_a_fresh_meter_is_all_zeroes(self):
        totals = Meter().totals()
        self.assertEqual(totals["llm_calls"], 0)
        self.assertEqual(totals["total_tokens"], 0)

    def test_tokens_accumulate_across_calls(self):
        meter = Meter()
        meter.record_llm(FakeResponse(FakeUsage(100, 20)))
        meter.record_llm(FakeResponse(FakeUsage(300, 50)))
        totals = meter.totals()
        self.assertEqual(totals["llm_calls"], 2)
        self.assertEqual(totals["input_tokens"], 400)
        self.assertEqual(totals["output_tokens"], 70)
        self.assertEqual(totals["total_tokens"], 470)

    def test_cached_tokens_are_reported_separately(self):
        meter = Meter()
        meter.record_llm(FakeResponse(FakeUsage(100, 20, cached=80)))
        self.assertEqual(meter.totals()["cached_tokens"], 80)

    def test_a_delta_is_one_report_types_share(self):
        """How serve.py splits one button press across two run rows."""
        meter = Meter()
        meter.record_llm(FakeResponse(FakeUsage(100, 10)))
        before = meter.totals()
        meter.record_llm(FakeResponse(FakeUsage(700, 90)))
        after = meter.totals()
        self.assertEqual(after["input_tokens"] - before["input_tokens"], 700)
        self.assertEqual(after["llm_calls"] - before["llm_calls"], 1)


class Robustness(unittest.TestCase):
    """Metering must never be the reason a run fails."""

    def test_a_response_with_no_usage_still_counts_the_call(self):
        meter = Meter()
        meter.record_llm(FakeResponse(None))
        self.assertEqual(meter.totals()["llm_calls"], 1)
        self.assertEqual(meter.totals()["total_tokens"], 0)

    def test_a_partial_usage_object_is_read_field_by_field(self):
        """TritonAI proxies several backends; not all report every field."""
        meter = Meter()
        partial = type("U", (), {"prompt_tokens": 50})()
        meter.record_llm(FakeResponse(partial))
        self.assertEqual(meter.totals()["input_tokens"], 50)
        self.assertEqual(meter.totals()["output_tokens"], 0)

    def test_a_none_usage_field_does_not_crash(self):
        meter = Meter()
        meter.record_llm(FakeResponse(type("U", (), {"prompt_tokens": None})()))
        self.assertEqual(meter.totals()["input_tokens"], 0)

    def test_a_failing_llm_call_is_counted_and_re_raised(self):
        meter = Meter()
        client = meter.wrap_llm(FakeLLM(FakeCompletions(raises=RuntimeError("boom"))))
        with self.assertRaises(RuntimeError):
            client.chat.completions.create(model="m", messages=[])
        self.assertEqual(meter.totals()["llm_failed"], 1)
        self.assertEqual(meter.totals()["llm_calls"], 1)

    def test_a_failing_search_is_counted_apart_from_billed_ones(self):
        meter = Meter()
        client = meter.wrap_tavily(FakeTavily(raises=RuntimeError("nope")))
        with self.assertRaises(RuntimeError):
            client.search("q")
        totals = meter.totals()
        self.assertEqual(totals["tavily_failed"], 1)
        self.assertEqual(totals["tavily_queries"], 0, "a raised request is not a billed one")


class CallShape(unittest.TestCase):
    """The wrapper must fit the call connect.py actually makes."""

    def test_the_locked_call_path_works_through_the_wrapper(self):
        completions = FakeCompletions(FakeResponse(FakeUsage(120, 34)))
        meter = Meter()
        client = meter.wrap_llm(FakeLLM(completions))

        # Verbatim the shape used in utils/connect.py.
        resp = client.chat.completions.create(
            model="claude-sonnet-4-6",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.2,
        )

        self.assertIs(resp.usage.prompt_tokens, 120)
        self.assertEqual(meter.totals()["input_tokens"], 120)
        self.assertEqual(meter.totals()["output_tokens"], 34)
        # The call reached the real client, arguments intact.
        self.assertEqual(completions.calls[0]["model"], "claude-sonnet-4-6")
        self.assertEqual(completions.calls[0]["temperature"], 0.2)

    def test_connect_py_still_calls_through_this_seam(self):
        """If connect.py stops taking a client, the meter is silently bypassed."""
        source = (ROOT / "utils" / "connect.py").read_text(encoding="utf-8")
        self.assertIn("(client or get_client()).chat.completions.create", source)

    def test_unrelated_attributes_pass_through(self):
        meter = Meter()
        client = meter.wrap_llm(FakeLLM(FakeCompletions(FakeResponse())))
        self.assertEqual(client.api_key, "sk-test")
        self.assertEqual(client.other_method(), "passed through")

    def test_the_response_object_is_returned_unchanged(self):
        original = FakeResponse(FakeUsage())
        meter = Meter()
        client = meter.wrap_llm(FakeLLM(FakeCompletions(original)))
        self.assertIs(client.chat.completions.create(model="m", messages=[]), original)


class Searches(unittest.TestCase):
    def test_each_request_is_one_billed_credit(self):
        """Tavily bills per request, so the request count IS the credit count."""
        meter = Meter()
        client = meter.wrap_tavily(FakeTavily(results=5))
        for _ in range(4):
            client.search("q", topic="news", days=7)
        self.assertEqual(meter.totals()["tavily_queries"], 4)

    def test_articles_returned_are_counted(self):
        meter = Meter()
        client = meter.wrap_tavily(FakeTavily(results=5))
        client.search("q")
        client.search("q2")
        self.assertEqual(meter.totals()["articles_returned"], 10)

    def test_the_window_does_not_change_the_credit_count(self):
        """The question behind the whole feature: 1 day and 90 days cost the same."""
        for days in (1, 7, 30, 90):
            meter = Meter()
            client = meter.wrap_tavily(FakeTavily())
            client.search("q", days=days)
            client.search("q2", days=days)
            self.assertEqual(meter.totals()["tavily_queries"], 2, f"days={days}")

    def test_search_arguments_reach_the_client(self):
        inner = FakeTavily()
        client = Meter().wrap_tavily(inner)
        client.search("acme layoffs", topic="news", days=30, max_results=5)
        self.assertEqual(inner.calls[0][0], "acme layoffs")
        self.assertEqual(inner.calls[0][1]["days"], 30)

    def test_an_odd_response_shape_is_ignored_not_fatal(self):
        meter = Meter()
        meter.record_search("not a dict")
        self.assertEqual(meter.totals()["tavily_queries"], 1)
        self.assertEqual(meter.totals()["articles_returned"], 0)


if __name__ == "__main__":
    unittest.main()
