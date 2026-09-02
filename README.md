# Tenant & Competitor Intelligence — Digest UI Prototype

A working prototype of the **email digests** from the Tenant Intelligence Pipeline
([`CONTEXT.md`](CONTEXT.md)): what lands in the team's inbox each week, in **two report types**
(Tenants and Competitors), each previewable in **desktop and mobile**.

The pipeline now runs end to end: roster → Tavily search → LLM judging → rendered digest, with a
SQLite history store behind it so a story is never judged twice. The **static preview** still runs
off JSON fixtures, so it needs no database, no search credits, and no API key — what needs keys is
a *live* run.

**Preview:** <https://rsm-ravictor.github.io/newssource/> — or open `docs/index.html` locally,
it needs no server.

**Setting this up on another machine?** Start at [`SETUP.md`](SETUP.md) — it covers what git does
and doesn't carry, the keys, the rosters you have to supply, and the loopback-only rule.

---

## What's here

| Path | What it is |
|---|---|
| **`config/report_types.yaml`** | **Start here.** Defines both report types — categories, judging criteria, query templates, wording, file paths. Neither the template nor the scripts hardcode either taxonomy. |
| **`tenant-list.md` / `competitor-list.md`** | **The watchlists.** Hand-maintained Markdown rosters at the repo root; the tenant `#` column is the priority rank. Drives live runs. |
| **`serve.py`** | **The live runner.** Local web app: pick a date range, click Run, real pipeline executes. |
| `search.py` | Search stage — Tavily news per entity, URL canonicalization, dedupe. The privacy surface. |
| `judge.py` | Judge stage — one LLM call per entity, schema built from config, JSON repair. |
| `watchlist.py` | Roster parsing — Markdown tables read by column name, rank/category/ticker, name cleaning. |
| `render.py` | Render stage — alerts → digest context → HTML. Shared by the static build and live runs. Owns the severity → rank → source-tier sort. |
| `sources.py` | Source policy — which tier a domain is, whether it may be cited, and the entity's-own-domain rule. |
| `export_rubrics.py` | Regenerates the two `*-intelligence-rubric.md` docs from the config. `--check` fails if they are stale. |
| `db.py` | The history store — SQLite at `data/history.db`. Run records, every URL ever seen, judged alerts. Cross-run dedupe lives here. |
| `ingest.py` | Converts the AAT Excel workbooks into `data/watchlists/*.txt`. Optional — the root rosters are the reference. |
| `templates/digest.html` | **The email.** One Jinja2 template serving both report types; email-safe table HTML. Placeholder for the client's real template. |
| `templates/preview.html` | The static preview harness (published to Pages). No Run button — it can't have one. |
| `templates/runner.html` | The live runner UI — manual push button, workflow status bar. Shares `_canvas_css.html` with the preview so they can't drift. |
| `templates/reference.html` | The review page at `/reference` — rosters, meaningfulness guidelines, notes box. Never emailed. |
| `build_preview.py` | Renders every report type into `docs/` from fixtures. No network, no key. |
| `generate_summaries.py` | Judges the article *fixtures* offline — iterate on criteria without spending credits. |
| `smoke_test.py` | Proves the TritonAI connection once your key is in `.env`. |
| `tests/` | 110 tests, no network and no key. Includes the assertions that only name + city can reach a query. |
| [`SETUP.md`](SETUP.md) | Handoff guide — install, keys, rosters, and running this on another machine. |
| `utils/connect.py` | The single LLM entry point, verbatim from `TRITONAI_SETUP.md`. |
| `data/mock_articles_*.json` | Fictional search hits per report type, input to the offline judge. Include decoys that should be rejected. |
| `data/sample_alerts_*.json` | Judged findings per report type, input to the emails. What the published page shows. |

Pipeline: `search.py` → `judge.py` → `render.py` → `templates/digest.html`, with `db.py` recording
each stage as it goes. Both the live runner and the static build go through the same three
modules, so a live run and a published snapshot can't diverge.

## Email format — AAT Intel Briefing

`templates/digest.html` follows the layout described in [`Email Template.md`](Email%20Template.md):

1. **Banner** — dark bar, `AAT Intel Briefing` + report type on the left, date on the right.
2. **Two-column summary** (58% / 38%, stacking below 680px):
   - **Top Intel** — up to 5 events, at most 2 per company, most severe first, each a clickable
     headline over its category and company.
   - **Who's in the News** — one pill per company, anchored to its detail card (`id="aat-index"`).
   - **Trending Now** — purple box, vertical ticker; company names in 33px rows, a 132px window
     (4 visible), list duplicated so the 12s `translateY` loop is seamless. Company count below.
3. **Divider.**
4. **Full Briefing** — one card per company in index order, each with a `↑ Back to index` link and
   one block per event: severity badge, category, headline, summary, `Read source →`.
5. **Footer** — privacy line, then briefing name · generated automatically · timestamp.

Severity labels are now **Urgent / Watch / Informational** (red / amber / blue). The underlying
keys stay `high` / `medium` / `low`, because that's what the judge returns and what
`--priorities` filters on — only the display wording changed. Rename in `config/report_types.yaml`.

### Where this deviates from the description, and why

- **Footer says "Generated automatically", not "by n8n".** This pipeline is Python, not n8n;
  claiming otherwise would be false provenance. Change the string in the template if you're
  sending from the n8n workflow.
- **Top Intel links the headline, not the category.** The description uses the category as the
  clickable title because its event objects have no headline field; ours do, and a headline is
  strictly more informative. The category still shows, as a colored label beneath.
- **Anchor links degrade in Gmail.** Gmail strips `id` attributes, so the index pills and
  `Back to index` links are inert there. They work in Apple Mail and most webmail. Same caveat the
  description already notes for the ticker animation, which Gmail and Outlook also strip — the
  ticker then reads as a static stacked list.
- **Banner keeps the report type** under the briefing name, since two digests share the template
  and would otherwise be indistinguishable at a glance.

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

### The watchlists

The two rosters at the repo root are the point of reference:

| File | Shape | Rank |
|---|---|---|
| `tenant-list.md` | `\| # \| Tenant Name \| City \|`, one table per segment (Office, Retail & MU) | **Yes** — the `#` column |
| `competitor-list.md` | `\| Company \| Category \| Ticker \| Notes \|`, one table per market under a `## City` heading | No |

`watchlist.py` reads Markdown tables **by column name**, so the meaning of a field comes from its
header rather than its position, and a table with no `City` column inherits the city from its
`## Heading`. Prose, headings, blank lines and table separators are skipped, so the files stay
readable documents. Duplicate rows collapse to one entity keeping the best rank — the tenant
roster lists a company once per lease, so 638 rows are 626 entities.

Names are normalized for searching: `LPL HOLDINGS, INC` becomes `LPL Holdings`. A name that
already carries mixed case was typed that way deliberately and is left alone, so `DivcoWest`
survives. The `/reference` page shows both spellings.

Only the **name and city** ever leave the machine. Rank, category, ticker and notes stay local.

**These two files are gitignored** — they are the real portfolio and competitor lists, and this
repo is public.

1. Edit the rosters, then check the `criteria` for each type in `config/report_types.yaml` still
   describes the signals you care about — or add adjustments in the notes box on `/reference`.
2. `python build_preview.py` — the "monitored" count comes from the rosters.

Until a live run replaces them, the article fixtures in `data/mock_articles_*.json` are what
`generate_summaries.py` judges.

### How rank drives prioritization

Rank is the one real difference between the two sides, and it is used twice:

1. **In judging.** The tenant prompt carries `Portfolio rank: 12 of 271 (Office roster)` plus the
   `rank_note` rubric from the config: a top-25 tenant can have a medium signal escalated to high,
   an outside-the-top-150 tenant has to clear a higher bar. Rank never makes an irrelevant article
   relevant. The competitor prompt never mentions rank at all; that side escalates on the
   `tier_note` rubric instead, from a tier the model infers about the firm — direct submarket
   competitor, public REIT peer, or institutional capital entrant.
2. **In reading order.** Within a severity, the briefing leads with the highest-ranked entity, so
   one urgent item at the #3 tenant outranks three items at #400.

Give the competitor side a rank by adding a `#` column to its tables and setting
`use_rank: true` under `report_types.competitors`.

### Source reliability does two jobs

`source_tiers` in the config lists outlets best-first — wire services and SEC filings, then the CRE
trade press, then company-issued releases, then anything unlisted, then social, AI answer engines
and user-posted. A finding's domain decides where it sits. Subdomains inherit their parent
(`m.facebook.com` → `facebook.com`); a domain matching nothing lands at `default_position`.

**Reading order.** Tier is the third sort key: the full sort is **severity → roster rank → source
tier**, so it only orders findings that already tie on both. Severity and rank stay the rubric's
axes.

**Citation.** A finding is only emitted if it can be cited at or above `citable_max_position`
(currently 3 — wire, trade, and company-issued). Weaker sources are still searched and still drive
*discovery*; they just can't be the link the briefing hands you. Where several outlets carry one
event, the best-tiered one is cited automatically, so a story a Facebook post surfaced still ships
if Bisnow also has it. An event whose whole cluster sits below the line is dropped and the run log
names the outlets — a gated drop never looks like "nothing happened."

One exception, and it matters: **the entity's own domain is always citable.** An IR release or
company newsroom is the primary record of a company's own announcement, so
`investors.autodesk.com` qualifies for Autodesk even though no tier list could enumerate it. The
match is by name against whole host labels, with generic words (`realty`, `properties`, `trust`)
and short fragments ignored, so "Irvine Company" can't claim `citywatchla.com`.

With no `source_tiers` block, every row shares one flat tier and nothing is gated — exactly the
pre-tier behavior. The `/reference` page renders the tier table with the citation line marked.

## Live runner — clickable date range

```bash
python serve.py            # http://127.0.0.1:8765
```

Pick **Last 1 / 7 / 30 / 90 days**, click **Push run now**, and it really executes the pipeline:
Tavily news search per entity → judging via `utils/connect.py` → the same `digest.html`
template. Nothing runs on a schedule and nothing runs on page load — a run only ever happens
because someone pressed that button, which is what makes it demoable.

**1 day** is the cheap test window: a full-roster run over a single day returns few enough
articles to judge the whole list without spending a week's worth of credits and tokens. The
window that loads by default is `lookback_default` (7), kept separate from the preset order in
`config/report_types.yaml` so the cheapest option can sit at the front of the bar without
quietly becoming what an unattended production run uses.

The **status bar** under the run bar tracks the five stages, driven by the server rather than a
timer:

    Reading List → Searching News Sources (Tavily) → Reviewing/Prioritizing Meaningful News
    (Claude) → Curating Email → Done

Searching and Reviewing alternate per entity, so the bar moves between them as the run works
through the list; the note under each stage says which entity and how many articles. A stage that
throws is marked in red. The bar is page chrome, above and outside the email frames — it is not in
`digest.html`, so it never appears in either the desktop or the mobile email, or in Copy HTML.

Per-entity progress also streams into the log below it, including which articles were kept and
which were thrown out with the reason.

- **Pause & build email** appears only while a run is live. It stops the retrieval loop at the
  next entity boundary — never mid-entity, so nothing is left searched-but-unjudged — and then
  lets the run finish the rest of the way: review, curate, render. You get a real email built
  from whatever came back, not a cancelled run. It is one-way; there is no resume, the next run
  starts at the top of the roster. A paused briefing is labelled honestly: its period line reads
  `... · paused after 24 of 626 tenants` and its "monitored" count is the number actually
  screened, not the roster size. A report type the pause landed before is not rendered at all
  rather than sent as a misleading empty briefing.
- **Cap entities** limits how many names per list get run — use it to keep demos fast and cheap.
  The cost line shows the credit estimate before you click.
- **Reference** opens `/reference` — see below.
- **Copy HTML** copies the active report's email.
- **Save as snapshot** freezes the finished run into `data/sample_alerts_*.json` and rebuilds
  `docs/`, so you can commit and push it to the public Pages link. Note that saving a *capped*
  run writes a partial snapshot — its "monitored" count is the capped number, not your full list.
  The same applies to a *paused* run, and a pause can also leave one report type's snapshot
  untouched while it rewrites the other's. Leave the cap at 0 and let the run finish for a
  snapshot you intend to publish.

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

## Reference page — review only

```
http://127.0.0.1:8765/reference        (the Reference button in the runner toolbar)
```

A tab per side, and nothing on it is ever part of an email. Each tab shows, read from the same
places a run reads:

- **The list.** The full roster, grouped by section, filterable. The tenant tab shows the `#` rank
  and highlights the top 25; the competitor tab shows Category, Ticker and Notes instead, and
  says plainly that it has no rank column. Where a name was normalized, the roster spelling is shown under it.
- **What counts as meaningful.** The `criteria` block from `config/report_types.yaml` verbatim —
  the categories, the exclusions, the geographic/market scoping rule, the priority rubric and
  the CEO-flag test — which is exactly what the model is sent as its system prompt.
- **Escalation**, one card per side: **Ranking → prioritization** on the tenant tab (the
  `rank_note` rubric, and how rank is used) and **Competitor tiers → escalation** on the
  competitor tab (the `tier_note` rubric). Both only move priority; neither can suppress a
  finding.
- **Queries sent per entity**, with `{name}` and `{city}` marked, and the credit count for the
  whole list.
- **Notes & changes.** A writable box per side, saved to `data/notes/<key>.md`. This is not a
  scratchpad: `judge.py` appends it to that side's criteria on the next run, under a header telling
  the model to treat it as guidance that refines the standing criteria but cannot change the output
  format. So a note like *"treat a sublease listing as high for anyone in the top 25"* actually
  changes the next run's judging. `Ctrl/Cmd+S` saves; leaving with unsaved text warns.

`data/notes/` is gitignored — the notes are written about real tenants and competitors.

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

110 tests, no network and no key needed.

`test_search.py` (18) — the important ones assert that a built Tavily query contains the entity name
and city and **nothing else**; if someone adds a placeholder to `query_templates`, they fail. The
rest cover URL canonicalization (two tracking-param variants of one story must hash identically),
source-name derivation, and date parsing.

`test_watchlist.py` (22) — roster parsing: rank and city read from their own columns, an empty middle
column that must not shift the rest, prose above a table that must not become an entity, city
inherited from a `## Market` heading, dedupe keeping the best rank, and name cleaning that titles
`LPL HOLDINGS, INC` while leaving `DivcoWest` alone. Also that two markets sharing a firm name get
distinct email anchors, and that rank orders entities within a severity. The tests against the real
rosters skip themselves when those gitignored files are absent.

`test_db.py` (19) — the history store: schema creation is re-runnable, `unseen` filters URLs from
an earlier run but is scoped per entity *and* report type, two tracking-param variants of one
story dedupe to each other, recording the same run twice doesn't double-report, and `pending` /
`mark_emailed` guarantee a story is handed to the send stage at most once.

`test_source_tiers.py` (12) — reading order: tiers rank in the order they're written, an unlisted
domain takes `default_position` and still beats social, a subdomain matches its parent, and — the
ones that matter — priority and roster rank both still outrank tier, nothing is ever dropped for
its source, and passing no tiers reproduces the old order exactly.

> A green run on a fresh clone does **not** mean your rosters parse — the roster tests skip
> themselves when `tenant-list.md` / `competitor-list.md` are absent, which they are until you
> add your own. Re-run the suite once they're in place.

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

### Where the rubric actually lives

`config/report_types.yaml` **is** the rubric: the `criteria`, `evidence_standard`, `rank_note` and
`tier_note` blocks are verbatim what the model receives as its system prompt. The two
`*-intelligence-rubric.md` files at the repo root are for people — they're what gets read in a
review meeting — and **nothing reads them at runtime**.

That split is useful right up until they disagree, at which point someone edits a document and
their change never reaches the model. So they're generated, not maintained:

```bash
python export_rubrics.py           # rewrite both docs from the config
python export_rubrics.py --check   # exit 1 if either is stale (worth a pre-commit hook)
```

Edit the YAML, re-run, commit both. For an adjustment you want to try on the next run without
touching the config, use the notes box on `/reference` — `judge.py` appends it to that side's
criteria.

## The evidence standard

Everything above decides whether a development *matters*. `evidence_standard` in the config — one
block, prepended to both report types' criteria — decides whether a finding is trustworthy and new
at all, and it is applied first. An article failing any of it is excluded however interesting its
subject.

| Rule | What it catches |
|---|---|
| **Is it about this entity?** | The model judges a title and a snippet, never the page. Search matches stray mentions, so a law firm's attorney bio listing a past client, or a consulting write-up of another company, arrives looking like news. `about_entity=false` excludes it. |
| **When did the event happen?** | `event_date` is the date of the *development*, not of publication. A 2026 article about 2025 layoffs is a 2025 event. Undated means excluded: recency is the point of the briefing, and publication inside the window is not evidence the event is recent. |
| **One event, one item.** | Every judgment carries an `event_key`. Judgments sharing one collapse to a single finding, keeping the cluster's most severe priority and CEO flag, so consolidating never quietly downgrades. |
| **Can it be cited?** | See the citation line above. |
| **Is it new?** | An event reported in an earlier briefing is not reported again, whichever outlet carried it this time. |

The last three are enforced in code as well as prompt, in `judge.consolidate()` and
`db.fresh_events()` — the model sees one article at a time and knows nothing about the domain
policy or what shipped last week, so asking it to enforce them alone would be asking it to guess.
`--verbose` and the runner log print every drop with its reason.

Findings also carry `deal_metrics` — price, $/SF or $/unit, and cap rate as disclosed — which the
competitor rubric requires for acquisitions, dispositions and pricing comps, and which the email
renders under the summary.

### Reserved seats for smaller tenants

Sorting strictly by severity then rank fills Top Intel with the same large names every week, and a
real event at tenant #430 never gets read. `top_intel_reserve` on the tenants side holds `slots: 2`
of the box for entities ranked worse than `beyond_rank: 100`.

This changes who is *eligible* for the last seats, never the standard a finding must meet. Nothing
is padded: with no qualifying lower-ranked finding the seats return to the general pool, and a
reserved row has passed exactly the same relevance bar as every other. The rubric side of the same
problem is in `rank_note` — top-25 escalation now requires a *concrete* event, since the usual
cause of a big-tenant-only briefing is escalating large names on thin material.

## The history store

`db.py` keeps a SQLite database at `data/history.db`, created on first use and re-runnable —
`python db.py` makes it if absent and prints its state (counts, then the last five runs).

- **`runs`** — one row per run: report type, lookback, model, status, and the kept/dropped counters
  stamped by `finish_run`.
- **`results`** — every article ever fetched, keyed by a hash of its canonicalized URL. This is
  what `unseen()` reads: an article already seen for that entity and report type is never judged
  again, so re-running a window costs nothing in tokens and can't resurface an old story. Dedupe is
  scoped per entity *and* report type, and stripping tracking params happens before hashing, so two
  links to the same story collapse.
- **`alerts`** — the judged-relevant findings, with `priority`, the CEO flag, `event_date`,
  `event_key`, `deal_metrics`, and `emailed_at`. `fresh_events()` reads `event_key` to stop one
  development being reported twice from two URLs.
  `pending()` / `mark_emailed()` are the backstop that will let the send stage email a story at
  most once, ever — which makes daily-vs-weekly a pure scheduling choice.

The database is **machine-local and gitignored**. It holds real findings about real tenants, and
copying it between machines mostly copies confusion — a new install should start empty.

## Not built yet

From `CONTEXT.md`, still to come:

- **Gmail SMTP send.** `pending()` and `mark_emailed()` are ready and the `.env` keys are
  reserved; nothing calls them yet, so digests are read in the browser or copied out by hand.
- **Excel rent-roll ingest with deterministic company IDs.** `ingest.py` converts the AAT
  workbooks into watchlists, but the rent-roll → `companies`/`properties`/`leases` schema in
  `CONTEXT.md` — with rents that never leave the database — isn't built. No rent or lease data
  is stored anywhere in this repo today.
- **The `tenant-intel` CLI.** Stages are driven by `serve.py` and the individual scripts rather
  than by one command with subcommands.
- **Scheduling.** Every run is a button press, on purpose.
