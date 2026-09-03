"""Guards for the runner page's script.

WHY THIS EXISTS
    The picker shipped with its element lookups missing - `var pickBtn = ...`
    never made it into the file - so `pickBtn.addEventListener(...)` threw a
    ReferenceError and killed every listener after it. The date buttons, Run,
    Pause and resume were all dead in the browser, and the whole Python suite
    passed anyway, because nothing here had ever executed the page's JavaScript.

    These tests cannot run a browser, but they can catch the specific shape of
    that bug: a name used as an element without ever being looked up. That is
    cheap, and it is the failure that actually happened.

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = tempfile.TemporaryDirectory()
os.environ["NEWS_DB"] = str(Path(_TMP.name) / "test.db")

import serve  # noqa: E402


def tearDownModule():  # noqa: N802
    _TMP.cleanup()


# Names the browser provides. Everything else has to be declared in the page.
BUILT_IN = {"document", "window", "navigator", "console", "location", "history",
            "localStorage", "sessionStorage", "this", "el", "frame", "doc"}


def page_script() -> str:
    """Every <script> block of the rendered runner page, concatenated."""
    html = serve.render_page()
    return "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))


class ScriptTest(unittest.TestCase):
    def setUp(self):
        self.js = page_script()

    def declared(self) -> set[str]:
        """Every name the script brings into scope: vars, named functions, and
        function parameters - a callback's `function (b)` is as real a
        declaration as a var, and forgetting it would make this test cry wolf."""
        names = set(re.findall(r"\bvar\s+([A-Za-z_$][\w$]*)", self.js))
        names |= set(re.findall(r"\bfunction\s+([A-Za-z_$][\w$]*)", self.js))
        for params in re.findall(r"\bfunction\s*[A-Za-z_$\w]*\s*\(([^)]*)\)", self.js):
            names |= {p.strip() for p in params.split(",") if p.strip()}
        # `catch (e)` and `for (var x ...)` bind names too.
        names |= set(re.findall(r"\bcatch\s*\(\s*([A-Za-z_$][\w$]*)", self.js))
        return names

    def test_every_element_listener_has_a_lookup(self):
        """`pickBtn.addEventListener(...)` with no `var pickBtn` is a dead page.

        The ReferenceError aborts the enclosing script, so this does not break
        one control - it breaks every control declared after it.
        """
        declared = self.declared()
        used = set(re.findall(r"\b([A-Za-z_$][\w$]*)\.addEventListener\b", self.js))
        missing = sorted(name for name in used - declared if name not in BUILT_IN)
        self.assertEqual(missing, [], f"used as elements but never looked up: {missing}")

    # A "was every called function defined?" check was tried here and removed.
    # Scope in JavaScript cannot be settled with a regex: window.setTheme = fn is
    # a definition, words inside strings and comments look like calls, and the
    # test reported eight names that were all fine. A guard that cries wolf gets
    # ignored, which is worse than not having it. The listener check above is
    # narrow enough to be trustworthy, and it is the failure that actually shipped.

    def test_the_picker_controls_are_present_in_the_markup(self):
        """The script looks these up by id; if the markup loses one, every lookup
        after it returns null and the same collapse happens at the first use."""
        html = serve.render_page()
        for element_id in ("pick-btn", "pick-label", "picker", "pick-cols",
                           "pick-search", "pick-tally", "pick-clear",
                           "run-btn", "pause-btn", "resume-btn"):
            self.assertIn(f'id="{element_id}"', html, element_id)

    def test_every_id_looked_up_exists_in_the_markup(self):
        """The general form of the test above: no getElementById for something
        the page does not render."""
        html = serve.render_page()
        wanted = set(re.findall(r"getElementById\('([^']+)'\)", self.js))
        missing = sorted(i for i in wanted if f'id="{i}"' not in html)
        self.assertEqual(missing, [], f"looked up but not in the markup: {missing}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
