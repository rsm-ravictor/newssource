"""Turn an authored config block into structured blocks for display.

The `criteria`, `evidence_standard`, `rank_note` and `tier_note` values in
config/report_types.yaml are authored as YAML block scalars, so they carry the
line wraps and hanging indents of a text file the model reads. Dropping that
straight into a `<pre>` shows the reader the *file's* line breaks rather than
their own screen's: text double-wraps, and a bullet's continuation lines sit two
spaces in, which reads as stray tabbing.

So the text is unwrapped here and rendered as real paragraphs and lists. Nothing
is reworded and nothing is dropped - the page's whole promise is that it prints
what the model is told - only the line breaks change, which belonged to the file
and not to the content.

Shared by the reference page (HTML) and export_rubrics.py (markdown) so the two
renderings of one rubric cannot drift apart.
"""

from __future__ import annotations

import re

# "- financial_distress: covenant breaches, ..." and "1. IS IT ABOUT ...".
BULLET = re.compile(r"^\s*-\s+(.*)$")
NUMBERED = re.compile(r"^\s*(\d+)\.\s+(.*)$")

# A SHOUTED lead-in that labels what follows: "GEOGRAPHIC SCOPING: determine ...",
# "IS IT ACTUALLY ABOUT THIS ENTITY? You are given ...". Two letters minimum and a
# terminator, so an acronym mid-sentence ("MEANINGFUL development") is not a label.
LEAD_IN = re.compile(r"^([A-Z][A-Z0-9][A-Z0-9 ,/&'()’-]{2,60}?)\s*([:.?])\s+(?=\S)")

# A category key labelling its own bullet: "- financial_distress: covenant ...".
KEY_IN = re.compile(r"^([a-z][a-z0-9_]{2,40})\s*(:)\s+(?=\S)")

# A short line that introduces the list under it: "EXCLUDE (set is_relevant=false):".
# Rendered as a label with no body rather than as a stubby paragraph.
STANDALONE = re.compile(r"^([A-Z][^.!?]{0,78}:)$")


def unwrap(text: str) -> str:
    """Collapse a hard-wrapped run of lines into one flowing line."""
    return re.sub(r"\s+", " ", text).strip()


def split_lead(text: str) -> tuple[str, str]:
    """Separate a SHOUTED label from the sentence that follows it.

    Returns ``(label, rest)``; the label is empty when the text does not open
    with one. The terminator stays on the label, because "PRICE?" and "PRICE:"
    are punctuated deliberately.
    """
    if STANDALONE.match(text):
        return text.strip(), ""
    match = LEAD_IN.match(text) or KEY_IN.match(text)
    if not match:
        return "", text
    return match.group(1).strip() + match.group(2), text[match.end():].strip()


def structure(text: str) -> list[dict]:
    """Authored block -> a list of {kind, ...} blocks ready to render.

    Kinds are ``p`` (with ``label`` and ``text``), ``ul`` and ``ol`` (with
    ``items``, each also a label/text pair). Blank lines separate blocks; a line
    opening with ``-`` or ``N.`` starts an item and its indented continuations
    belong to it.
    """
    blocks: list[dict] = []
    para: list[str] = []          # lines of the paragraph being accumulated
    items: list[list[str]] = []   # lines of each item in the current list
    kind = ""                     # "ul" or "ol" while a list is open

    def flush_para() -> None:
        if para:
            label, body = split_lead(unwrap(" ".join(para)))
            blocks.append({"kind": "p", "label": label, "text": body})
            para.clear()

    def flush_list() -> None:
        nonlocal kind
        if items:
            rendered = []
            for item in items:
                label, body = split_lead(unwrap(" ".join(item)))
                rendered.append({"label": label, "text": body})
            blocks.append({"kind": kind or "ul", "items": rendered})
            items.clear()
        kind = ""

    for raw in text.splitlines():
        if not raw.strip():
            # A blank line ends a paragraph, but NOT necessarily a list: the
            # numbered items in the evidence standard are separated by blank
            # lines, and an item's own second paragraph is indented under it.
            # Whether the list continues is decided by the next line, so that
            # decision is deferred rather than made here.
            flush_para()
            continue

        bullet = BULLET.match(raw)
        numbered = NUMBERED.match(raw)
        if bullet or numbered:
            flush_para()
            wanted = "ol" if numbered else "ul"
            if kind and kind != wanted:
                flush_list()
            kind = wanted
            items.append([(numbered.group(2) if numbered else bullet.group(1))])
            continue

        indented = raw[:1].isspace()
        if items and indented:
            # A continuation of the item above, possibly after a blank line.
            items[-1].append(raw.strip())
        else:
            # Back at the left margin: whatever list was open has ended.
            flush_list()
            para.append(raw.strip())

    flush_para()
    flush_list()
    return blocks


def to_markdown(text: str) -> str:
    """The same structure as markdown, for the generated rubric docs."""
    out: list[str] = []
    for block in structure(text):
        if block["kind"] == "p":
            lead = f"**{block['label']}** " if block["label"] else ""
            out.append(f"{lead}{block['text']}".strip())
        else:
            # Numbered for real rather than a repeated "1.": the docs get read in
            # a plain editor as often as in a markdown renderer.
            for i, item in enumerate(block["items"], start=1):
                marker = f"{i}." if block["kind"] == "ol" else "-"
                lead = f"**{item['label']}** " if item["label"] else ""
                out.append(f"{marker} {lead}{item['text']}".strip())
        out.append("")
    return "\n".join(out).strip()
