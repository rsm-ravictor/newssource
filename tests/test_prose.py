"""Reflowing authored config text for display.

The rule this pins: the transformation may drop the FILE's line breaks and
nothing else. The reference page's whole promise is that it prints what the
model is told, so a formatter that reworded, reordered or swallowed a clause
would quietly turn the page into a paraphrase. `test_no_words_are_lost` is the
one that matters most here.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from prose import structure, to_markdown, unwrap  # noqa: E402

CONFIG = yaml.safe_load((ROOT / "config" / "report_types.yaml").read_text(encoding="utf-8"))


class Unwrapping(unittest.TestCase):
    def test_hard_wrapped_lines_become_one(self):
        self.assertEqual(unwrap("a line\nwrapped here"), "a line wrapped here")

    def test_indentation_does_not_survive(self):
        """The 2-space hanging indent under a bullet is what read as tabbing."""
        self.assertEqual(unwrap("first\n  continued\n  further"), "first continued further")


class Structure(unittest.TestCase):
    def test_paragraphs_split_on_blank_lines(self):
        blocks = structure("one\ntwo\n\nthree")
        self.assertEqual([b["kind"] for b in blocks], ["p", "p"])
        self.assertEqual(blocks[0]["text"], "one two")

    def test_bullets_become_a_list(self):
        blocks = structure("- first item\n- second item")
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["kind"], "ul")
        self.assertEqual([i["text"] for i in blocks[0]["items"]], ["first item", "second item"])

    def test_bullet_continuations_join_their_item(self):
        blocks = structure("- first item\n  continued here\n- second")
        self.assertEqual(blocks[0]["items"][0]["text"], "first item continued here")

    def test_numbered_items_separated_by_blank_lines_stay_one_list(self):
        """The evidence standard is written this way; it used to fragment."""
        blocks = structure("1. one\n\n2. two\n\n3. three")
        self.assertEqual([b["kind"] for b in blocks], ["ol"])
        self.assertEqual(len(blocks[0]["items"]), 3)

    def test_an_indented_paragraph_after_a_blank_line_stays_in_its_item(self):
        text = "1. first thing\n\n   still the first thing\n\n2. second"
        blocks = structure(text)
        self.assertEqual(len(blocks), 1)
        self.assertIn("still the first thing", blocks[0]["items"][0]["text"])

    def test_a_left_margin_paragraph_closes_the_list(self):
        blocks = structure("- item\n\nBack at the margin.")
        self.assertEqual([b["kind"] for b in blocks], ["ul", "p"])

    def test_shouted_lead_in_becomes_a_label(self):
        blocks = structure("GEOGRAPHIC SCOPING: determine whether the event is local.")
        self.assertEqual(blocks[0]["label"], "GEOGRAPHIC SCOPING:")
        self.assertEqual(blocks[0]["text"], "determine whether the event is local.")

    def test_a_question_lead_in_keeps_its_mark(self):
        blocks = structure("1. WHEN DID IT HAPPEN? Report the date of the development.")
        self.assertEqual(blocks[0]["items"][0]["label"], "WHEN DID IT HAPPEN?")

    def test_category_keys_label_their_bullet(self):
        blocks = structure("- financial_distress: covenant breaches and defaults.")
        self.assertEqual(blocks[0]["items"][0]["label"], "financial_distress:")

    def test_standalone_label_has_no_body(self):
        """"EXCLUDE (...):" introduces the list under it; it is not a paragraph."""
        blocks = structure("EXCLUDE (set is_relevant=false):\n- routine news")
        self.assertEqual(blocks[0]["label"], "EXCLUDE (set is_relevant=false):")
        self.assertEqual(blocks[0]["text"], "")

    def test_an_acronym_mid_sentence_is_not_a_label(self):
        text = "Decide whether it is a MEANINGFUL development that a team would act on."
        self.assertEqual(structure(text)[0]["label"], "")

    def test_empty_text_is_no_blocks(self):
        self.assertEqual(structure(""), [])
        self.assertEqual(structure("\n\n  \n"), [])


class NothingIsLost(unittest.TestCase):
    """Every authored block in the real config, checked word for word."""

    @staticmethod
    def words(text: str) -> list[str]:
        """Content words, ignoring list markers.

        A leading "1." or "- " is structure: it becomes the <ol>/<ul> marker the
        browser draws, so it is not expected to survive as text. Every other word
        must.
        """
        stripped = re.sub(r"(?m)^\s*(?:-|\d+\.)\s+", "", text)
        return re.findall(r"[A-Za-z0-9_]+", stripped)

    def blocks_text(self, blocks) -> str:
        out = []
        for block in blocks:
            if block["kind"] == "p":
                out.append(block["label"] + " " + block["text"])
            else:
                for item in block["items"]:
                    out.append(item["label"] + " " + item["text"])
        return " ".join(out)

    def authored(self):
        yield "evidence_standard", CONFIG["evidence_standard"]
        for key, spec in CONFIG["report_types"].items():
            yield f"{key}.criteria", spec["criteria"]
            for field in ("rank_note", "tier_note"):
                if spec.get(field):
                    yield f"{key}.{field}", spec[field]

    def test_no_words_are_lost(self):
        for name, text in self.authored():
            with self.subTest(block=name):
                self.assertEqual(
                    self.words(self.blocks_text(structure(text))),
                    self.words(text),
                    f"{name}: reflow changed the words",
                )

    def test_no_words_are_lost_in_markdown_either(self):
        for name, text in self.authored():
            with self.subTest(block=name):
                # ** and list markers are formatting, not content.
                self.assertEqual(self.words(to_markdown(text)), self.words(text), name)

    def test_the_real_blocks_produce_lists(self):
        """A config that structured into one long paragraph would mean a bug."""
        for name, text in self.authored():
            with self.subTest(block=name):
                kinds = {b["kind"] for b in structure(text)}
                self.assertTrue(kinds, name)


class Markdown(unittest.TestCase):
    def test_numbered_items_are_numbered_in_order(self):
        md = to_markdown("1. one\n\n2. two\n\n3. three")
        self.assertEqual([line.split(".")[0] for line in md.splitlines() if line], ["1", "2", "3"])

    def test_labels_become_bold(self):
        self.assertIn("**SOURCING.**", to_markdown("SOURCING. Use a wire service."))

    def test_bullets_keep_their_marker(self):
        self.assertTrue(to_markdown("- one\n- two").startswith("- one"))


if __name__ == "__main__":
    unittest.main()
