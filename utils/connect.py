"""Unified client for TritonAI-proxied LLMs.

Every supported model is reached through TritonAI's OpenAI-compatible proxy, so
a single ``OpenAI`` client with ``base_url=BASE_URL`` works for all of them. To
switch models, change the ``model=`` argument to ``ask()`` — no other edits.

Example
-------
    from utils.connect import ask
    print(ask("Explain a p-value in one sentence."))

    # Swap models by changing one argument:
    print(ask("Explain a p-value in one sentence.", model="gemini-3-flash"))
"""

from __future__ import annotations

import os
import sys
from typing import Any

from openai import OpenAI

BASE_URL = "https://tritonai-api.ucsd.edu/v1"

# Cheap, On-Prem Instructional models — use these for experimentation.
CHEAP_MODELS: list[str] = [
    "api-llama-4-scout",
    "gemini-3-flash",
    "api-gpt-oss-120b",
]

# Pricier AWS Instructional models — reach for these only when the cheap models aren't enough.
EXPENSIVE_MODELS: list[str] = [
    "gpt-5.4",
    "gemini-3-pro-preview",
    "claude-opus-4-6-v1",
]

# Reserved "model" ids that are NOT sent to TritonAI — they route through the
# OAuth path in ``utils.oauth_gpt`` instead. Set ``MODEL = "oauth-gpt"`` to use
# your personal ChatGPT/Codex subscription via OAuth (no API key needed).
OAUTH_MODELS: list[str] = ["oauth-gpt"]

# >>> SET THIS FROM THE Q1 ANSWER — the only line an agent may edit. <<<
DEFAULT_MODEL = "claude-opus-4-6-v1"
DEFAULT_SYSTEM = "You are a helpful assistant. Be concise."


def get_client(api_key: str | None = None) -> OpenAI:
    """Return an :class:`openai.OpenAI` client pointed at TritonAI.

    Parameters
    ----------
    api_key:
        Explicit API key. When ``None`` (the default) the value of the
        ``TRITONAI_API_KEY`` environment variable is used. Raises
        :class:`ValueError` if no key is available.
    """
    key = api_key or os.environ.get("TRITONAI_API_KEY", "")
    if not key:
        raise ValueError(
            "TRITONAI_API_KEY is not set. Copy .env.example to .env and fill in your key."
        )
    return OpenAI(api_key=key, base_url=BASE_URL)


def describe_model(model: str) -> str:
    """Return a one-line human description of where a model call will go."""
    if model in OAUTH_MODELS:
        return f"{model!r} → OAuth Codex backend ({OAUTH_BACKEND_MODEL})"
    return f"{model!r} → TritonAI ({BASE_URL})"


def ask(
    prompt: str | list[dict[str, str]],
    model: str = DEFAULT_MODEL,
    system: str = DEFAULT_SYSTEM,
    temperature: float = 0.4,
    max_tokens: int = 4000,
    md: bool = False,
    verbose: bool = False,
    client: OpenAI | None = None,
) -> str | None:
    """Send ``prompt`` to ``model`` and return the assistant's reply.

    **No fallbacks.** If ``model`` is unknown, unauthorized, or the endpoint
    returns an error, the exception propagates. This function never silently
    picks a different model for you. The only "fallback" is on OAuth
    credentials loading (see ``utils.oauth_gpt.ensure_openai_codex_credentials``):
    if the oauth_gpt cache is missing the Codex CLI login at ``~/.codex/auth.json``
    is tried — same subscription, same account, just a different file.

    Parameters
    ----------
    prompt:
        A user prompt as a plain string (wrapped into a system+user messages
        pair) or a pre-built list of ``{"role": ..., "content": ...}`` dicts
        (for multi-turn conversations).
    model:
        Model id as shown on https://tritonai-api.ucsd.edu/ui/?page=models.
        Common choices: ``api-llama-4-scout``, ``gemini-3-flash``, ``gpt-5.4``.
    system:
        System message used when ``prompt`` is a string. Ignored when
        ``prompt`` is already a messages list.
    temperature:
        Sampling temperature. ``0`` is deterministic; higher values are more
        creative. Default ``0.4``. **Note:** the OAuth route (``model="oauth-gpt"``)
        does not accept a temperature — the Codex responses endpoint controls
        randomness via its own ``reasoning_effort`` / ``text_verbosity`` knobs.
        The value is accepted here for API symmetry but has no effect on OAuth.
    max_tokens:
        Maximum tokens in the response.
    md:
        When ``True``, render the response as Markdown via IPython and return
        ``None`` (useful inside Jupyter). When ``False``, return the text.
    verbose:
        When ``True``, print a one-line route summary to stderr before the call
        (which endpoint, which model) and the server-reported model after the
        call. Useful for confirming that the model you *think* you're using is
        the one actually hit.
    client:
        Optional pre-built :class:`openai.OpenAI` client. Defaults to one
        created from ``TRITONAI_API_KEY``.
    """
    if verbose:
        _log(f"[ask] {describe_model(model)}")

    if model in OAUTH_MODELS:
        text = _ask_via_oauth(
            prompt,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if verbose:
            _log("[ask] response received via OAuth (server does not echo model id)")
    else:
        if isinstance(prompt, str):
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ]
        else:
            messages = prompt

        resp = (client or get_client()).chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        text = resp.choices[0].message.content or ""
        if verbose:
            server_model = getattr(resp, "model", None)
            if server_model:
                _log(f"[ask] server reported model: {server_model!r}")

    if md:
        _display_markdown(text)
        return None
    return text


def ask_json(
    prompt: str | list[dict[str, str]],
    schema: type | None = None,
    model: str = DEFAULT_MODEL,
    system: str = DEFAULT_SYSTEM,
    temperature: float = 0.4,
    max_tokens: int = 4000,
    verbose: bool = False,
    client: OpenAI | None = None,
) -> Any:
    """Send ``prompt`` and return the response parsed as JSON.

    If ``schema`` is a :class:`pydantic.BaseModel` subclass, the response is
    validated and returned as an instance of that model. Otherwise, the raw
    parsed ``dict`` is returned.

    **No fallbacks.** See :func:`ask` — an unknown or unauthorized model raises.
    Set ``verbose=True`` to print the route and server-reported model to stderr.
    """
    if verbose:
        _log(f"[ask_json] {describe_model(model)}")

    schema_hint = ""
    if schema is not None:
        try:
            schema_hint = (
                "\n\nReturn ONLY a JSON object that matches this schema:\n"
                f"{schema.model_json_schema()}"
            )
        except AttributeError:
            schema_hint = ""

    if model in OAUTH_MODELS:
        # OAuth Codex endpoint has no response_format flag — rely on the schema
        # hint plus an explicit "JSON only" instruction.
        json_prompt = prompt
        if isinstance(json_prompt, str):
            json_prompt = json_prompt + schema_hint
        else:
            json_prompt = list(json_prompt)
            if schema_hint and json_prompt:
                last = dict(json_prompt[-1])
                last["content"] = str(last.get("content", "")) + schema_hint
                json_prompt[-1] = last
        json_system = (
            (system or DEFAULT_SYSTEM)
            + "\nReturn ONLY a valid JSON object. No prose, no code fences."
        )
        text = _ask_via_oauth(
            json_prompt,
            system=json_system,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    else:
        if isinstance(prompt, str):
            user_content = prompt + schema_hint
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ]
        else:
            messages = list(prompt)
            if schema_hint and messages:
                last = dict(messages[-1])
                last["content"] = str(last.get("content", "")) + schema_hint
                messages[-1] = last

        resp = (client or get_client()).chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
        text = resp.choices[0].message.content or "{}"
        if verbose:
            server_model = getattr(resp, "model", None)
            if server_model:
                _log(f"[ask_json] server reported model: {server_model!r}")

    if not text:
        text = "{}"

    if schema is not None and hasattr(schema, "model_validate_json"):
        return schema.model_validate_json(text)

    import json as _json

    return _json.loads(text)


def list_models(client: OpenAI | None = None) -> list[dict[str, str]]:
    """List the models available through TritonAI.

    Returns a sorted list of dicts with ``id`` and ``type`` keys. ``type`` is
    a coarse category inferred from the model id (``chat``, ``embeddings``,
    ``ocr``).
    """
    c = client or get_client()
    raw = c.models.list()
    out = []
    for m in sorted(raw.data, key=lambda x: x.id):
        label = "embeddings" if "embed" in m.id else "ocr" if "ocr" in m.id else "chat"
        out.append({"id": m.id, "type": label})
    return out


def md(x: str) -> None:
    """Render a string as Markdown inside a Jupyter notebook."""
    _display_markdown(x)


def _log(msg: str) -> None:
    """Write one line to stderr — used by ``verbose=True``."""
    print(msg, file=sys.stderr, flush=True)


def _display_markdown(x: str) -> None:
    """Thin wrapper around IPython's Markdown display (easier to mock in tests)."""
    from IPython.display import Markdown, display

    display(Markdown(x))


# ---------------------------------------------------------------------------
# OAuth route (ChatGPT/Codex subscription instead of TritonAI API key)
# ---------------------------------------------------------------------------
#
# The OAuth Codex endpoint is single-shot (one prompt + instructions, no
# messages list). We flatten a multi-turn messages list into a transcript with
# role labels and pull system messages out into ``instructions``.
#
# Student code shouldn't need to know any of this — they just set
# ``MODEL = "oauth-gpt"`` in ``ask(...)``.

OAUTH_BACKEND_MODEL = "gpt-5.4"   # actual model asked of the Codex endpoint


def _ask_via_oauth(
    prompt: str | list[dict[str, str]],
    *,
    system: str = DEFAULT_SYSTEM,
    temperature: float = 0.4,     # accepted for API symmetry; see note below
    max_tokens: int = 4000,       # unused — OAuth endpoint has no explicit cap
) -> str:
    """Send ``prompt`` through the OpenAI Codex OAuth route and return text.

    ``temperature`` is accepted but **not forwarded** — the Codex responses
    endpoint doesn't expose a temperature parameter (it uses ``reasoning_effort``
    / ``text_verbosity`` instead). We accept it here so the signature of
    ``ask()`` and ``ask_json()`` is identical across model paths.
    """
    _ = temperature  # intentionally unused — see docstring

    from utils.oauth_gpt import ask_gpt_with_oauth

    if isinstance(prompt, str):
        instructions = system
        user_text = prompt
    else:
        system_msgs = [m["content"] for m in prompt if m.get("role") == "system"]
        instructions = "\n\n".join(system_msgs) if system_msgs else system
        lines = []
        for m in prompt:
            role = m.get("role", "user")
            if role == "system":
                continue
            lines.append(f"{role.upper()}: {m.get('content', '')}")
        user_text = "\n\n".join(lines)

    return ask_gpt_with_oauth(
        user_text,
        model=OAUTH_BACKEND_MODEL,
        instructions=instructions,
    )
