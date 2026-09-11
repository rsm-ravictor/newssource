"""Pick a model the key can actually reach, whatever key that is.

WHY THIS EXISTS
    ``utils/connect.py`` is verbatim-locked and pins one DEFAULT_MODEL. That is
    fine until the key changes: a TritonAI key carries a team, the team decides
    which models it may use, and the proxy answers 403 team_model_access_denied
    for anything outside that set. A key reissued under a different team turns
    every judge call into an error that looks like a code fault and is not one.

    So the model is resolved at run time from what the key can list, and the
    configured default is treated as a preference rather than a promise. Give the
    key Claude again and it goes back to Claude on the next run, with nothing to
    edit.

NO QUALITY CLAIM IS MADE HERE
    The preference order is about capability class, not about which model judges
    better. A frontier model is preferred because the rubric was written against
    one; beyond that this module has no opinion, and picking a model it has never
    seen is expected rather than exceptional.
"""

from __future__ import annotations

import os
import re

# Families in descending order of "how close to what the rubric was written
# against". Matched as substrings against the model id, so a version bump does
# not need a code change.
PREFERRED = (
    # Frontier families first: the rubric was written against one, so if a key
    # ever regains that tier the run should go back to it without an edit.
    "claude-opus", "claude-sonnet", "claude",
    "gpt-5", "gpt-4", "gpt-oss", "gpt",
    "gemini-3-pro", "gemini-3", "gemini",
    "llama-4", "llama",
    # Below here the order is MEASURED on this proxy rather than assumed, judging
    # one 6-article entity under the real rubric at a 16000-token ceiling:
    #   muse-glimmer-30b  valid, 2,196 out tokens, 14.8s
    #   gemma-4-31b       invalid, 4,154 tokens
    #   deepseek-v4-flash invalid, hit the 16000 cap, 66.8s
    #   glm-5.3           invalid, hit the 16000 cap, 329.1s
    # The two that failed are reasoning models that spend the whole allowance
    # before answering. That is a fact about this job - a long rubric and a
    # strict JSON envelope - not a general ranking of these models.
    "muse-glimmer", "muse",
    "gemma", "deepseek", "glm", "qwen", "mistral",
)

# Models that cannot judge anything, whatever else they are good at. Matched as
# substrings; an id naming one of these is never a candidate.
NOT_A_JUDGE = (
    "embed", "embedding", "ocr", "transcribe", "whisper", "tts", "speech",
    "rerank", "vision-only", "moderation", "image", "diffusion",
)


def usable(model_id: str) -> bool:
    """Could this model plausibly return a judged JSON envelope?"""
    lowered = model_id.lower()
    return not any(bad in lowered for bad in NOT_A_JUDGE)


def rank(model_id: str) -> int:
    """Position in PREFERRED, or one past the end for anything unrecognised.

    Unknown models sort last but are still eligible - a proxy that adds a model
    tomorrow should be usable today without an edit here.
    """
    lowered = model_id.lower()
    for i, family in enumerate(PREFERRED):
        if family in lowered:
            return i
    return len(PREFERRED)


def available(client) -> list[str]:
    """Every model id the key can list. Empty when the proxy will not say."""
    try:
        return sorted(m.id for m in client.models.list().data)
    except Exception:  # noqa: BLE001 - listing is a convenience, never a blocker
        return []


def resolve(client, preferred: str, *, note=None) -> str:
    """The model this run should use.

    The configured default wins whenever the key can reach it. Otherwise the best
    usable alternative is chosen and said out loud, because silently judging on a
    different model than the one recorded in the run row would make the history
    lie about how a finding was reached.

    TRITONAI_MODEL overrides everything, for pinning one model without editing
    the locked file.
    """
    forced = (os.environ.get("TRITONAI_MODEL") or "").strip()
    if forced:
        if note:
            note(f"model pinned by TRITONAI_MODEL: {forced}")
        return forced

    ids = available(client)
    if not ids:
        # The proxy would not list. Try the configured default and let the call
        # itself fail with a real error rather than guessing from nothing.
        return preferred

    if preferred in ids:
        return preferred

    # A measurement of this key's own models beats the static order below, which
    # is only ever a guess made somewhere else. calibrate.py writes it, and it is
    # ignored automatically once the key's access set changes.
    try:
        import calibrate

        measured = calibrate.best_for(client)
    except Exception:  # noqa: BLE001 - calibration is an optimisation, never a gate
        measured = None
    if measured and measured in ids:
        if note:
            note(f"{preferred} is not available to this key; judging with {measured} "
                 "(measured best by calibrate.py)")
        return measured

    candidates = [m for m in ids if usable(m)]
    if not candidates:
        return preferred

    choice = sorted(candidates, key=lambda m: (rank(m), m))[0]
    if note:
        note(f"{preferred} is not available to this key; judging with {choice}")
    return choice


def describe(client, preferred: str) -> str:
    """One line for a log or a startup banner."""
    ids = available(client)
    if not ids:
        return f"{preferred} (the proxy would not list its models)"
    if preferred in ids:
        return f"{preferred}"
    return f"{resolve(client, preferred)} ({preferred} not available to this key)"
