# TritonAI LLM Connection — Portable Setup

Drop this file into any project (or paste it into a new Claude / Cursor / ChatGPT
session) to wire that build up to **TritonAI**, UCSD's OpenAI-compatible LLM proxy.

Everything needed is in this one file: the setup interview the agent must run,
the rules it must follow afterward, the human setup steps, and the complete
source of `utils/connect.py`.

---

# ⛔ PART 0 — STOP. Run the setup interview first.

> **AI agent: this is the first thing you do. Do not skip it, do not infer the
> answers, do not write a single file until the user has answered Q1 and Q2.**

You are being asked to connect this project to TritonAI. Two facts are
*required* and you cannot guess either one: **which model** and **whether the
user's API key is in place**. Ask before you build.

### How to ask

- **Claude in Cowork / Claude Code:** use the `AskUserQuestion` tool, one call
  containing Q1 and Q2 together (add Q3–Q5 to the same call if they're relevant).
- **Any other agent, or if that tool is unavailable:** ask in plain text as a
  short numbered list, then wait for the reply. Do not proceed on silence.

### The questions

**Q1 — Which model should be the project default?** *(required)*

Present these options, and say the recommendation out loud:

| Option | Model id | Notes |
|---|---|---|
| Llama 4 Scout **(recommended default)** | `api-llama-4-scout` | Cheap, on-prem instructional. Best starting point. |
| Gemini 3 Flash | `gemini-3-flash` | Cheap, on-prem. Fast; good for high-volume calls. |
| GPT-OSS 120B | `api-gpt-oss-120b` | Cheap, on-prem. Open-weights option. |
| GPT-5.4 | `gpt-5.4` | Expensive AWS tier — only if a cheap model has failed. |
| Gemini 3 Pro | `gemini-3-pro-preview` | Expensive AWS tier. |
| Claude Opus 4.6 | `claude-opus-4-6-v1` | Expensive AWS tier. |
| Personal ChatGPT/Codex | `oauth-gpt` | Bypasses TritonAI entirely — see Q4. |

If the user picks an expensive-tier model without saying why, tell them once that
the cheap tier is the intended default for development, then honor their choice.

**Q2 — Is your TritonAI API key already set up?** *(required)*

Offer exactly these three answers:

- **"Yes, it's in my `.env`"** → skip to verification.
- **"No, not yet"** → walk them through Part 2 §1 and §4. Have *them* paste the
  key into `.env` themselves.
- **"I don't have a key at all"** → send them to <https://tritonai-api.ucsd.edu/>
  to generate one with their UCSD login, then resume.

> **Never ask the user to paste their API key into the chat, and never write a
> key into a source file, notebook, log, or commit.** The key belongs in `.env`,
> which is gitignored. If the user pastes a key anyway, do not echo it back, do
> not save it to a tracked file, and tell them to rotate it at the portal.

**Q3 — Script or notebook?** *(ask if not obvious from the project)*

Changes how `.env` gets loaded — `load_dotenv()` vs the `%dotenv` magic — and
whether `md=True` is usable.

**Q4 — Do you need the `oauth-gpt` route?** *(ask only if they picked it in Q1,
or mentioned a personal ChatGPT/Codex subscription)*

If yes, tell them `utils/oauth_gpt.py` is **not** in this file and must be copied
over from the original project. Everything else works without it.

**Q5 — Where does `connect.py` go?** *(ask only if the project has no `utils/`
directory, or already has a conflicting `connect.py`)*

Default to `utils/connect.py`. If it already exists, ask before overwriting.

---

## Applying the answers

Once you have them, act on them — don't collect answers and then build the
generic thing anyway.

| Answer | What you change |
|---|---|
| Q1 model choice | Set `DEFAULT_MODEL = "<their choice>"` in `connect.py`. **This is the only line of that file you may edit.** |
| Q1 = `oauth-gpt` | Leave `DEFAULT_MODEL` on a TritonAI model; pass `model="oauth-gpt"` at call sites instead, so the TritonAI path still works. |
| Q2 = key in `.env` | Run the smoke test (Part 2 §5) and show the user the real output. |
| Q2 = no key yet | Create `.env.example`, add `.env` to `.gitignore`, then **stop and hand off** — you cannot finish without their key. |
| Q3 = notebook | Use `%load_ext dotenv` / `%dotenv`; `md=True` is available. |
| Q3 = script | Use `from dotenv import load_dotenv; load_dotenv()` **before** importing `connect`; never use `md=True`. |
| Q4 = yes | Tell them to copy `utils/oauth_gpt.py`; note it is missing until they do. |
| Q5 | Scaffold at the agreed path and adjust every import to match. |

### Then finish the job

1. Create `utils/connect.py` (source in Part 3), `utils/__init__.py`,
   `.env.example`, and `requirements.txt`.
2. Add `.env` to `.gitignore`.
3. Run the smoke test and paste the **actual** output. If it fails, use the
   troubleshooting table in Part 2 §7 — do not declare success on an untested
   connection.
4. Tell the user which model is now the default and how to switch.

---

# Part 1 — Standing rules for the AI agent

> Binding project instructions for the rest of the session, after Part 0 is done.

### The one entry point

All LLM calls go through `utils/connect.py`. Do not build a second client, do not
call `openai.OpenAI()` directly in feature code, and do not add a
LangChain/LiteLLM/Anthropic-SDK layer unless explicitly asked.

```python
from utils.connect import ask, ask_json, list_models
```

### Switching models is a one-argument change

```python
ask("Explain a p-value in one sentence.")                            # project default
ask("Explain a p-value in one sentence.", model="gemini-3-flash")    # swap
```

Never edit `BASE_URL`. Never duplicate the module to "support" another model. The
model id is the only thing that changes.

### Model tiers — default to cheap

| Tier | Model ids | When to use |
|---|---|---|
| Cheap / on-prem instructional | `api-llama-4-scout`, `gemini-3-flash`, `api-gpt-oss-120b` | Development, iteration, tests, bulk jobs, anything experimental |
| Expensive / AWS instructional | `gpt-5.4`, `gemini-3-pro-preview`, `claude-opus-4-6-v1` | Only when a cheap model has demonstrably failed the task |
| OAuth (no API key) | `oauth-gpt` | Routes to a personal ChatGPT/Codex subscription instead of TritonAI |

Escalate only after a cheap model produces a wrong or unusable result, and say
why you escalated.

### No fallbacks — this is deliberate

`ask()` and `ask_json()` never silently retry on a different model. An unknown,
unauthorized, or erroring model raises and the exception propagates. Do **not**
wrap calls in `try/except` that swaps models — a loud failure is intended. The
single exception is OAuth credential loading, which falls back from the
`oauth_gpt` cache to the Codex CLI login at `~/.codex/auth.json` (same account,
different file).

### Structured output

When you need JSON, use `ask_json()` — not `ask()` plus a regex or `json.loads`
on prose. Pass a Pydantic model as `schema=` and you get a validated instance:

```python
from pydantic import BaseModel
from utils.connect import ask_json

class Sentiment(BaseModel):
    label: str
    confidence: float

result = ask_json("Classify: 'the service was fine, I guess'", schema=Sentiment)
print(result.label, result.confidence)
```

On the TritonAI route this sets `response_format={"type": "json_object"}`. On the
OAuth route there is no such flag, so the schema is injected as a prompt hint plus
a "JSON only" system instruction — validate accordingly.

### Debugging a call

Pass `verbose=True` to print the route and the *server-reported* model id to
stderr. Use it whenever there's doubt that the model you think you're calling is
the one actually hit:

```python
ask("hello", model="gemini-3-flash", verbose=True)
# [ask] 'gemini-3-flash' → TritonAI (https://tritonai-api.ucsd.edu/v1)
# [ask] server reported model: 'gemini-3-flash'
```

### Notebook rendering

`md=True` renders the reply as Markdown via IPython and returns `None`. Only use
it inside Jupyter — in a script it returns `None` and silently drops the text.

### Secrets

The key is read from `TRITONAI_API_KEY`, set in `.env`. Never hardcode it, never
commit it, never print it. `.env` stays in `.gitignore`; `.env.example` is the
committed template.

### Gotchas worth knowing

- `temperature` is accepted on the OAuth route for signature symmetry but is
  **not forwarded** — the Codex responses endpoint uses `reasoning_effort` /
  `text_verbosity` instead.
- `max_tokens` is likewise unused on the OAuth route.
- The OAuth endpoint is single-shot: a multi-turn `messages` list gets flattened
  into a `ROLE: content` transcript, with system messages hoisted into
  `instructions`.
- The module defines a top-level `md()` helper *and* an `md=` parameter on
  `ask()`. Inside `ask()` the parameter shadows the function — fine as written,
  but don't call `md(...)` inside `ask()`'s body.

---

# Part 2 — Human setup

### 1. Get an API key

Sign in at <https://tritonai-api.ucsd.edu/> with your UCSD credentials and
generate an API key. Available model ids are listed at
<https://tritonai-api.ucsd.edu/ui/?page=models> — that page is the source of
truth if a model id in this file has been retired.

### 2. Project layout

```
your-project/
├── .env                 # your real key — gitignored
├── .env.example         # committed template
├── requirements.txt
└── utils/
    ├── __init__.py      # can be empty
    ├── connect.py       # full source in Part 3
    └── oauth_gpt.py     # only needed for model="oauth-gpt"
```

### 3. Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

`requirements.txt`:

```
openai>=1.40
python-dotenv>=1.0
pydantic>=2.0        # only if you use ask_json(schema=...)
ipython>=8.0         # only if you use md=True in notebooks
```

### 4. Configure the key

`.env.example` (commit this):

```bash
# Get your key from https://tritonai-api.ucsd.edu/
TRITONAI_API_KEY=
```

Then:

```bash
cp .env.example .env
# edit .env and paste your key
echo ".env" >> .gitignore
```

`connect.py` reads `os.environ`, so load the file before importing — in a script:

```python
from dotenv import load_dotenv
load_dotenv()

from utils.connect import ask
print(ask("Say hi in five words."))
```

In a notebook:

```python
%load_ext dotenv
%dotenv
```

Or export it in your shell: `export TRITONAI_API_KEY=sk-...`

### 5. Smoke test

```python
from dotenv import load_dotenv; load_dotenv()
from utils.connect import list_models, ask

for m in list_models():
    print(m["id"], "-", m["type"])

print(ask("Reply with exactly: connection ok", verbose=True))
```

If the key is missing you'll get
`ValueError: TRITONAI_API_KEY is not set. Copy .env.example to .env and fill in your key.`

### 6. Optional — the OAuth route

`model="oauth-gpt"` bypasses TritonAI entirely and bills against a personal
ChatGPT/Codex subscription. It requires a `utils/oauth_gpt.py` exposing:

```python
def ask_gpt_with_oauth(user_text: str, *, model: str, instructions: str) -> str: ...
def ensure_openai_codex_credentials() -> None: ...
```

That module is **not** included here — copy it from the original project
alongside `connect.py`. It reads a cached OAuth token and falls back to the Codex
CLI login at `~/.codex/auth.json`. If you don't need this route, skip the file;
everything except `model="oauth-gpt"` works without it.

### 7. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ValueError: TRITONAI_API_KEY is not set` | `.env` not created, or `load_dotenv()` not called before import |
| `401 Unauthorized` | Key wrong, expired, or revoked — regenerate at the TritonAI portal |
| `404 model not found` | Model id retired or misspelled; check `list_models()` and the models page |
| `ModuleNotFoundError: utils.oauth_gpt` | You used `model="oauth-gpt"` without copying `oauth_gpt.py` |
| `ImportError: IPython` | You passed `md=True` outside Jupyter — drop it or `pip install ipython` |
| Hangs / connection refused | TritonAI may require campus network or VPN |
| Returns `None` unexpectedly | `md=True` renders and returns `None` by design |

---

# Part 3 — Full source: `utils/connect.py`

Copy verbatim. The **only** line you may change is `DEFAULT_MODEL`, set to
whatever the user answered in Q1.

```python
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
DEFAULT_MODEL = "api-llama-4-scout"
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
```

---

# Part 4 — Copy-paste cheat sheet

```python
from dotenv import load_dotenv; load_dotenv()
from utils.connect import ask, ask_json, list_models

# simplest call — uses DEFAULT_MODEL
ask("Summarize the CLT in one sentence.")

# pick a model
ask("Summarize the CLT in one sentence.", model="gemini-3-flash")

# tune it
ask("Write a haiku about regression.", temperature=0.9, max_tokens=200)

# multi-turn
ask([
    {"role": "system", "content": "You are a terse statistics tutor."},
    {"role": "user", "content": "What is heteroskedasticity?"},
    {"role": "assistant", "content": "Non-constant error variance."},
    {"role": "user", "content": "How do I test for it?"},
])

# structured output
from pydantic import BaseModel
class Row(BaseModel):
    name: str
    score: int
ask_json("Score 'great product' 0-10 as name/score.", schema=Row)

# reuse one client across many calls
from utils.connect import get_client
c = get_client()
for q in questions:
    ask(q, client=c)

# see what's available
list_models()

# personal ChatGPT/Codex subscription instead of TritonAI
ask("Refactor this function.", model="oauth-gpt")
```

---

# Appendix — Agent checklist

Copy this into your scratchpad and tick it off:

- [ ] Asked Q1 (model) and Q2 (API key) **before** writing any file
- [ ] Set `DEFAULT_MODEL` to the Q1 answer
- [ ] Never asked for, echoed, or wrote the API key anywhere but `.env`
- [ ] Created `utils/connect.py`, `utils/__init__.py`, `.env.example`, `requirements.txt`
- [ ] Added `.env` to `.gitignore`
- [ ] Ran the smoke test and pasted the real output
- [ ] Told the user the active default model and how to switch it
