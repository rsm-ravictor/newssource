"""Model resolution tests - the part that keeps a new key from breaking a run.

A TritonAI key carries a team, and the team decides which models it may use. A
key reissued under a different team is the single most likely thing to break this
pipeline without any code changing, so what happens then is worth pinning down.

No network: the client is a stub that lists whatever the test says it lists.

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import model_choice  # noqa: E402


class FakeModels:
    def __init__(self, ids, raises=False):
        self.ids = ids
        self.raises = raises

    def list(self):
        if self.raises:
            raise RuntimeError("proxy will not say")
        return type("Resp", (), {"data": [type("M", (), {"id": i}) for i in self.ids]})


class FakeClient:
    def __init__(self, ids, raises=False):
        self.models = FakeModels(ids, raises)


ON_PREM = ["api-cohere-transcribe", "api-deepseek-v4-flash", "api-gemma-4-31b",
           "api-glm-5.3", "api-lightonocr-1b", "api-muse-glimmer-30b", "api-tgpt-embeddings"]


class ResolveTest(unittest.TestCase):
    def setUp(self):
        self.saved = os.environ.pop("TRITONAI_MODEL", None)

    def tearDown(self):
        os.environ.pop("TRITONAI_MODEL", None)
        if self.saved is not None:
            os.environ["TRITONAI_MODEL"] = self.saved

    def test_the_configured_model_wins_when_the_key_has_it(self):
        client = FakeClient(ON_PREM)
        self.assertEqual(
            model_choice.resolve(client, "api-muse-glimmer-30b"), "api-muse-glimmer-30b")

    def test_a_key_without_the_configured_model_still_runs(self):
        """The failure this module exists for: the key is fine, the model is not
        on its team, and every judge call would 403."""
        client = FakeClient(ON_PREM)
        said = []
        choice = model_choice.resolve(client, "claude-sonnet-4-6", note=said.append)
        self.assertIn(choice, ON_PREM)
        self.assertTrue(said, "a substituted model must be announced, not silent")
        self.assertIn("claude-sonnet-4-6", said[0])

    def test_it_goes_back_to_the_better_model_when_the_key_regains_it(self):
        """Restoring access must not need a code change."""
        client = FakeClient(ON_PREM + ["claude-sonnet-4-6"])
        self.assertEqual(
            model_choice.resolve(client, "claude-sonnet-4-6"), "claude-sonnet-4-6")

    def test_it_prefers_a_frontier_model_over_an_on_prem_one(self):
        client = FakeClient(ON_PREM + ["claude-opus-4-6-v1"])
        self.assertEqual(
            model_choice.resolve(client, "not-a-real-model"), "claude-opus-4-6-v1")

    def test_it_never_picks_a_model_that_cannot_judge(self):
        """Embeddings, OCR and transcription are on this key's list and would
        fail in a way that looks like a judging bug."""
        client = FakeClient(["api-tgpt-embeddings", "api-lightonocr-1b",
                             "api-cohere-transcribe", "api-muse-glimmer-30b"])
        self.assertEqual(
            model_choice.resolve(client, "nothing-here"), "api-muse-glimmer-30b")

    def test_an_unknown_model_is_still_eligible(self):
        """A proxy that adds a model tomorrow should be usable today."""
        client = FakeClient(["some-brand-new-model"])
        self.assertEqual(
            model_choice.resolve(client, "nothing-here"), "some-brand-new-model")

    def test_it_falls_back_to_the_configured_model_when_listing_fails(self):
        """Better a real error from the call than a guess made from nothing."""
        client = FakeClient([], raises=True)
        self.assertEqual(model_choice.resolve(client, "api-muse-glimmer-30b"),
                         "api-muse-glimmer-30b")

    def test_the_environment_can_pin_a_model(self):
        os.environ["TRITONAI_MODEL"] = "api-glm-5.3"
        client = FakeClient(ON_PREM + ["claude-sonnet-4-6"])
        self.assertEqual(model_choice.resolve(client, "claude-sonnet-4-6"), "api-glm-5.3")

    def test_measured_ordering_prefers_what_actually_worked(self):
        """Of the models this key can reach, only muse-glimmer returned a valid
        envelope when measured. The ordering has to reflect that, or a key losing
        its default would land on one of the three that failed."""
        client = FakeClient(ON_PREM)
        self.assertEqual(model_choice.resolve(client, "nothing-here"), "api-muse-glimmer-30b")


if __name__ == "__main__":
    unittest.main(verbosity=2)
