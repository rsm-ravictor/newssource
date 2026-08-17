# Tenant & Competitor Intelligence — Digest UI Prototype

A working prototype of the **email digests** from the Tenant Intelligence Pipeline
([`CONTEXT.md`](CONTEXT.md)): what lands in the team's inbox each week, in **two report types**
(Tenants and Competitors), each previewable in **desktop and mobile**.

This is the UI slice only. Ingest, the SQLite portfolio database, and Tavily search are
specified in `CONTEXT.md` but not built here — the preview runs off JSON fixtures, so it
needs no database, no search credits, and no API key.

**Preview:** <https://rsm-ravictor.github.io/newssource/> — or open `docs/index.html` locally,
it needs no server.

---

## What's here

| Path | What it is |
|---|---|
| **`config/report_types.yaml`** | **Start here.** Defines both report types — categories, judging criteria, query templates, wording, file paths. Neither the template nor the scripts hardcode either taxonomy. |
| **`data/watchlists/*.txt`** | **Paste your lists here**, one name per line (`Name` or `Name, City`). Drives live runs. |
| **`serve.py`** | **The live runner.** Local web app: pick a date range, click Run, real pipeline executes. |
| `search.py` | Search stage — Tavily news per entity, URL canonicalization, dedupe. The privacy surface. |
| `judge.py` | Judge stage — one LLM call per entity, schema built from config, JSON repair. |
| `render.py` | Render stage — alerts → digest context → HTML. Shared by the static build and live runs. |
| `templates/digest.html` | **The email.** One Jinja2 template serving both report types; email-safe table HTML. Placeholder for the client's real template. |
| `templates/preview.html` | The static preview harness (published to Pages). No Run button — it can't have one. |
| `templates/runner.html` | The live runner UI. Shares `_canvas_css.html` with the preview so they can't drift. |
| `build_preview.py` | Renders every report type into `docs/` from fixtures. No network, no key. |
| `generate_summaries.py` | Judges the article *fixtures* offline — iterate on criteria without spending credits. |
| `smoke_test.py` | Proves the TritonAI connection once your key is in `.env`. |
| `tests/test_search.py` | 17 tests, no network. Includes the assertions that only name + city can reach a query. |
| `utils/connect.py` | The single LLM entry point, verbatim from `TRITONAI_SETUP.md`. |
| `data/mock_articles_*.json` | Fictional search hits per report type, input to the offline judge. Include decoys that should be rejected. |
| `data/sample_alerts_*.json` | Judged findings per report type, input to the emails. What the published page shows. |

Pipeline: `search.py` → `judge.py` → `render.py` → `templates/digest.html`. Both the live runner
and the static build go through the same three modules, so a live run and a published snapshot
can't diverge.

## The two report types

|  | Tenants | Competitors |
|---|---|---|
| Question | Will this tenant pay, grow, or leave? | What is a rival landlord doing to us? |
| Categories | financial distress, expansion, office move, leadership change | acquisition, disposition, development, leasing, capital, leadership |
| Goes to | asset management | acquisitions |
| Raw email | `docs/digest-tenants.html` | `docs/digest-competitors.html` |

They are **separate standalone emails**, so they can go to different recipients on different
schedules. Both render from the same template and the same code path.

> ⚠️ **The `competitors` block assumes rival landlords / owner-operators** — firms you compete
> with to win and keep tenants. If your competitor list is a different kind of firm (your
> *tenants'* competitors, or brokerages), edit `categories` and `criteria` under `competitors:` in
> `config/report_types.yaml`. Nothing else needs to change — not the template, not the scripts.

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

python build_preview.py --open
```

That builds and opens the preview from the committed fixture — no key required.

### Preview controls

Two independent toggles, both persisted across reloads:

- **Report:** Tenants / Competitors, or press <kbd>1</kbd> / <kbd>2</kbd>.
- **View:** Desktop / Mobile, or press <kbd>D</kbd> / <kbd>M</kbd>.
- **Copy HTML** puts the *active* report's raw email on your clipboard, for pasting into a mail
  client or an inbox-rendering service.
- **Open raw** opens the active report's email exactly as it would be sent.

Switching either toggle is a CSS class change — one iframe per report type sits in the DOM and
nothing reloads, so scroll position survives the switch.

Desktop mode renders the email at 600px inside a 760px viewport; mobile renders it at ~370px, which
trips the template's own `max-width: 620px` breakpoint — so you're seeing the real responsive
behavior, not a scaled-down screenshot.

### Build variants

```bash
python build_preview.py                        # both types, high + medium
python build_preview.py --type competitors     # one type only
python build_preview.py --priorities high      # production default per CONTEXT.md
python build_preview.py --empty                # the no-findings state
```

### Adding your lists

1. Paste names into `data/watchlists/tenants.txt` and `data/watchlists/competitors.txt`.
2. Check the `criteria` for each type in `config/report_types.yaml` still describes the signals
   you care about.
3. `python build_preview.py` — the "monitored" count comes from the watchlists, and the build
   warns on stderr if a fixture entity isn't on its list.

Until the search stage exists, the article fixtures in `data/mock_articles_*.json` are what gets
judged; the watchlists drive counts and will drive the Tavily queries once `search.py` lands.

## Live runner — clickable date range

```bash
python serve.py            # http://127.0.0.1:8765
```

Pick **Last 7 / 30 / 90 days**, click **Run live**, and it really executes the pipeline:
Tavily news search per entity → judging via `utils/connect.py` → the same `digest.html`
template. Per-entity progress streams into the page while it works, including which articles
were kept and which were thrown out with the reason.

- **Cap entities** limits how many names per list get run — use it to keep demos fast and cheap.
  The cost line shows the credit estimate before you click.
- **Copy HTML** copies the active report's email.
- **Save as snapshot** freezes the finished run into `data/sample_alerts_*.json` and rebuilds
  `docs/`, so you can commit and push it to the public Pages link. Note that saving a *capped*
  run writes a partial snapshot — its "monitored" count is the capped number, not your full list.
  Leave the cap at 0 for a snapshot you intend to publish.

### Why the live run is local-only

GitHub Pages serves static files. It cannot hold API keys or make outbound calls, and a public
Run button would let anyone spend your Tavily credits. So `serve.py` binds to `127.0.0.1`
(this machine only), reads both keys from `.env`, and never sends anything secret to the browser.
The published page (`docs/index.html`) is the static preview and has no Run button.

Cost per run: **2 Tavily credits per entity** (two queries each) plus one LLM call per entity.
A 70-name list is roughly 140 credits, so cap entities while iterating.

### What a live run actually looks like

Real news is mostly noise, and the criteria are built to say no. In testing, Illumina returned
10 articles over 30 days and **all 10 were excluded**; Qualcomm returned 8 and **4 were kept**.
An empty digest is a correct result, not a broken one — `--empty` renders that state on purpose.

## Connecting TritonAI

The LLM is only needed to *regenerate* the digest content; the preview itself is static.

1. Get a key at <https://tritonai-api.ucsd.edu/> with your UCSD login.
2. Paste it into `.env` (already created and gitignored):
   ```
   TRITONAI_API_KEY=your-key-here
   ```
3. Verify, then regenerate:
   ```bash
   python smoke_test.py                 # lists models, tests ask() and ask_json()
   python generate_summaries.py --verbose      # both report types
   python build_preview.py --open
   ```

`generate_summaries.py` makes **one call per entity** covering all of that entity's articles,
returning a validated Pydantic object via `ask_json(schema=...)`. It prints which articles it kept
and which it excluded, so you can see the criteria working. Both fixtures deliberately include
decoys that should be rejected — a "Top Workplace" award, a product launch and routine associate
hires on the tenants side; an ESG report, a conference panel, an industry award and market-wide
vacancy commentary on the competitors side.

The committed `data/sample_alerts_*.json` files are real `claude-sonnet-4-6` output from that
script: 7 findings across 5 tenants (3 decoys rejected) and 7 across 5 competitors (4 rejected).

### Model

The project default is **`claude-sonnet-4-6`**, set at `utils/connect.py:46`.

> **The model table in `TRITONAI_SETUP.md` is out of date.** Of the seven ids it lists, only
> `api-gpt-oss-120b` is still live — `claude-opus-4-6-v1`, `api-llama-4-scout`, `gemini-3-flash`,
> `gpt-5.4`, and `gemini-3-pro-preview` all return `403 team_model_access_denied` or aren't served.
> The setup file says its own models page is the source of truth when an id has been retired, and
> that's what happened here. `claude-sonnet-4-6` is the closest live equivalent to Opus 4.6.

Live ids as of Aug 17, 2026 (`python smoke_test.py` re-lists them):

```
claude-sonnet-4-6         claude-sonnet-4-6-aws     api-gpt-oss-120b
api-deepseek-v4-flash     api-gemma-4-26b           api-gemma-4-31b
api-glm-5.2               minimax.minimax-m2        moonshotai.kimi-k2.5
mistral.mistral-large-3-675b-instruct
us.amazon.nova-2-lite-v1:0    us.amazon.nova-premier-v1:0
api-lightonocr-1b (ocr)       api-tgpt-embeddings (embeddings)
```

Switch per run, or change the one `DEFAULT_MODEL` line — the only edit `connect.py` is meant to
receive. All calls route through it; there's no second client anywhere.

```bash
python generate_summaries.py --model api-gpt-oss-120b     # cheapest live option
```

TritonAI may require the campus network or VPN.

### Structured output on this proxy

TritonAI **accepts** `response_format={"type": "json_object"}` but doesn't enforce it, so
`ask_json(schema=...)` intermittently receives ```json fences, a bare array instead of the
`{"judgments": [...]}` envelope, or a trailing "hope that helps" paragraph — each of which fails
Pydantic validation inside `connect.py`.

Since `connect.py` is verbatim-locked, `generate_summaries.py` handles it at the call site:

1. The system prompt states the exact envelope and forbids fences and prose.
2. `judge_company()` calls `ask_json()` first, as the standing rules require.
3. On `ValidationError` it retries once through `ask()` and repairs the response —
   `_unfence()` brace-matches the first complete JSON object *or* array, and
   `_coerce_envelope()` normalizes bare arrays, single bare objects, and wrong-key wrappers.

Same model, same client, same prompt — a parsing repair, not a model fallback. A company that
still fails is skipped and reported on stderr rather than silently shrinking the digest.

## Tests

```bash
python -m unittest discover -s tests
```

17 tests, no network and no key needed. The important ones assert that a built Tavily query
contains the entity name and city and **nothing else** — if someone adds a placeholder to
`query_templates`, they fail. The rest cover URL canonicalization (two tracking-param variants of
one story must hash identically), source-name derivation, and date parsing.

## Privacy model

Carried over from `CONTEXT.md`, where it is a hard constraint:

- `search.py:build_queries` is the **entire** network privacy surface, and `query_templates` in
  the config may only reference `{name}` and `{city}`. Enforced by tests.
- The only entity data in the search/LLM prompt is the **name and city**
  (`generate_summaries.py:build_prompt`).
- The email template's context contains **no** rent, lease terms, addresses, or portfolio list.
  `portfolio_size` is a bare count.
- Article provenance (outlet, URL, date) is attached from the local record after the model
  responds, never taken from model output.
- Summaries are rendered with Jinja2 autoescaping on, since LLM output is untrusted text.

## Sample data

Every tenant, competitor, outlet, headline, and URL in `data/` is **fictional**, invented to exercise the
layout. Source links point at `example.com`. The preview labels itself "Sample data" so a hosted
copy can't be mistaken for real reporting.

## Hosting on GitHub Pages

`build_preview.py` writes to `docs/` (with `.nojekyll`), which Pages serves directly. Already live at
<https://rsm-ravictor.github.io/newssource/>. To set it up on a fresh repo:

**Settings → Pages → Source: Deploy from a branch → Branch: `main`, folder: `/docs`**

Rebuild and commit `docs/` whenever the template or fixture changes.

## Not built yet

From `CONTEXT.md`, still to come: SQLite schema and `init-db`, Excel rent-roll ingest with
deterministic company IDs, Tavily search with URL canonicalization and cross-run dedupe, the
`alerts` table that guarantees a story is emailed at most once, Gmail SMTP send, and the
`tenant-intel` CLI. The judge stage exists here in prototype form and moves to `judge.py`; the
email template moves in as-is.
