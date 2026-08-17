# Tenant Intelligence Pipeline (`~/tenant-intel`)

## Context

The company manages commercial tenants (rent roll in Excel: tenant name, building, sum of monthly rent, property, city). We need an automated pipeline that (1) keeps that portfolio in a private local SQL database, (2) searches the web per tenant via Tavily for *meaningful* developments — financial distress, expansion, office real-estate moves, leadership changes affecting real-estate strategy — (3) has Claude judge relevance/priority and write client-ready summaries, and (4) emails a digest of high-priority findings. Privacy is a hard constraint: only the company name (+ optionally city) ever leaves the machine; rents, addresses, lease terms, and the full portfolio list stay in local SQLite.

**User decisions (confirmed):** SQLite · Gmail SMTP single digest · names-only privacy model · weekly-or-daily run with high-priority-only alerts (no spam) · I build a placeholder HTML template (user swaps in their real one later) · user has a Tavily key, needs an Anthropic key (setup step included).

## Project layout

```
~/tenant-intel/
├── pyproject.toml              # uv-managed; [project.scripts] tenant-intel = "tenant_intel.cli:main"
├── .env / .env.example         # secrets (gitignored): TAVILY_API_KEY, ANTHROPIC_API_KEY,
│                               # GMAIL_ADDRESS, GMAIL_APP_PASSWORD, EMAIL_RECIPIENTS
├── config/config.yaml          # tuning: lookback_days=7, max_results=5, query_templates,
│                               # model=claude-opus-5, email_priorities=[high], column_map
├── templates/digest.html       # placeholder Jinja2 digest (user's template drops in later)
├── data/                       # tenant_intel.db, digest_preview.html (gitignored)
├── logs/                       # local-only logs (gitignored)
├── scripts/
│   ├── setup_database.py       # "script 1" — thin wrapper → cli init-db
│   └── ingest_tenants.py       # "script 2" — thin wrapper → cli ingest <xlsx>
├── tenant_intel/
│   ├── config.py               # dotenv + yaml → frozen Settings; per-command key validation
│   ├── db.py                   # connection factory, idempotent DDL, upserts, query helpers
│   ├── ingest.py               # Excel → companies/properties/leases (idempotent)
│   ├── search.py               # Tavily per-company search + URL canonicalization + dedupe
│   ├── judge.py                # Claude judgment (messages.parse + Pydantic)
│   ├── digest.py               # Jinja2 render + Gmail SMTP send
│   └── cli.py                  # argparse subcommands
└── tests/                      # test_ingest.py, test_dedupe.py, fixtures/sample_rentroll.xlsx
```

Deps: `pandas openpyxl tavily-python anthropic pydantic python-dotenv pyyaml jinja2`; dev: `pytest ruff`.

## Database schema (db.py — all `IF NOT EXISTS`, re-runnable)

- **companies**: `company_id` (deterministic slug of normalized name — lowercase, legal suffixes like LLC/Inc stripped; stable across re-ingests and DB rebuilds; collision guard appends short hash), `display_name`, `normalized_name UNIQUE`, `city`, timestamps.
- **properties**: `property_id` slug, `building`, `property`, `city`, `UNIQUE(building, property)`.
- **leases**: `lease_id = "{company_id}@{property_id}"`, FKs to both, `monthly_rent` (never leaves DB), `active` flag, `UNIQUE(company_id, property_id)` = idempotency guarantee. Rows absent from latest ingest get `active=0`.
- **search_runs**: run metadata + counters + `input_tokens`/`output_tokens` for cost tracking.
- **search_results** (staging/dedupe surface): every result ever fetched, `url_hash = sha256(canonical_url)[:16]`, `UNIQUE(company_id, url_hash)` — a URL seen once is never re-judged or re-alerted. Fuzzy title dedupe (difflib ratio > 0.90, same company, 30-day window) catches syndicated copies.
- **alerts**: judged-relevant findings — `category CHECK IN (financial_distress, expansion, office_move, leadership_change)`, `priority CHECK IN (high, medium, low)`, `headline`, `client_summary`, `confidence`, `emailed_at`, `UNIQUE(company_id, url_hash)` as final backstop.
- Indexes on leases(company/property), alerts(company, priority). Per-company and per-property queries via joins `alerts → companies → leases → properties`.

## Pipeline stages

**Ingest (ingest.py):** `pd.read_excel` with config-driven `column_map` (case/space-insensitive header matching; fail loudly listing found vs expected). Pure `normalize_company_name()` (unit-tested) underpins ID stability. Upserts via `ON CONFLICT ... DO UPDATE`; re-running the same file reports 0 new. Duplicate company×property rows in one file: sum rents, warn locally.

**Search (search.py):** Reuse Tavily idioms from `~/gramercy-workstream-1/research_agent.py:333-358` (client init, per-query try/except-and-continue, key-unset guard). **Two templated queries per company** (distress family / moves+leadership family) built from **only** `display_name` (+ city if enabled) — this function is the entire Tavily privacy surface and gets a dedicated unit test. `tavily.search(q, topic="news", days=lookback, max_results=5, search_depth="basic")` (1 credit/query — free tier covers ~120 tenants weekly), 0.5s sleep between calls, one company at a time. Canonicalize URLs (strip utm_*/fragments), `INSERT OR IGNORE` into search_results.

**Judge (judge.py):** **One Claude call per company** covering all its new unjudged results (cost-efficient, preserves one-company-at-a-time privacy). `client.messages.parse(model="claude-opus-5", max_tokens=4096, output_format=CompanyJudgment, system=[{...criteria prompt, cache_control: ephemeral}])` — Pydantic schema: `is_relevant`, `category` enum, `priority` (high/medium/low with concrete rubric: high = act-this-week signal), `headline` (≤90 chars), `client_summary` (2-3 sentences, client-ready, factual), `confidence`. System prompt encodes the criteria verbatim including explicit exclusions (routine news, product launches, marketing, minor personnel changes). No `thinking` param (adaptive by default). Handle `RateLimitError`, `APIStatusError` (skip company, note in run), and `stop_reason == "refusal"` before reading output. Persist judgments; relevant → `INSERT OR IGNORE` into alerts; accumulate token usage into search_runs.

**Digest (digest.py):** Jinja2 template with named placeholders (`run_date`, `alert_groups` by company → headline/category/summary/source link) so the user's real template drops in later. Template context contains **no** rent/lease/address data. Select `priority IN email_priorities` (default high-only) with `emailed_at IS NULL`; if empty → log "no significant findings", set run status, send nothing. Send via stdlib `smtplib.SMTP_SSL("smtp.gmail.com", 465)` + `EmailMessage` (HTML + plaintext fallback). On success stamp `emailed_at` (each story emailed at most once, ever — makes daily vs weekly a pure scheduling choice). `--dry-run` writes `data/digest_preview.html`, touches nothing.

## CLI

`tenant-intel init-db` · `ingest <path.xlsx>` · `run [--lookback D] [--limit N] [--dry-run]` (full pipeline) · `search`/`judge`/`send-report` (stage-by-stage) · `query companies` / `query alerts [--company X] [--property Y] [--priority P]` · `status` (recent runs + token spend). The two `scripts/*.py` wrappers honor the "two scripts" framing.

Scheduling: manual `tenant-intel run`, or cron `0 8 * * 1 cd ~/tenant-intel && .venv/bin/tenant-intel run >> logs/cron.log 2>&1` (README notes launchd alternative for macOS sleep behavior).

## Setup steps

1. `rsm-new-project --venv ~/tenant-intel` (user's direnv+uv convention), `cd`, `uv add` deps.
2. **Anthropic key**: console.anthropic.com → billing (add ~$5) → create key → `.env`. Estimated cost ≈ **$2–4 per weekly run at 50 tenants** (~$0.05–0.08/company; steady-state runs with few new articles cost pennies).
3. Tavily key (already held) → `.env`.
4. Gmail: 2-Step Verification → App passwords → Mail → `.env`.
5. `git init` with `.gitignore` covering `.env`, `data/`, `logs/`.

## Build order

config.py + db.py → init-db → ingest.py (+ fixture xlsx + tests) → search.py → judge.py → digest.py → cli.py glue → README/cron docs.

## Verification (no email spam, minimal spend)

1. `init-db` twice → second run no-op; `.schema` shows all tables.
2. `ingest` fixture twice → second run 0 new; `pytest` green (name normalization, upsert idempotency, query-builder privacy test).
3. `run --limit 2 --dry-run` → 2 companies through Tavily+Claude, preview HTML written, no email; inspect alerts + token counts via `status`.
4. Repeat step 3 → 0 new judged results (cross-run dedupe verified).
5. `send-report` with recipients = user's own address → one test email; second `send-report` sends nothing (`emailed_at` stamped).
6. Ingest real rent roll (user supplies path) → full run → check `status` for cost.

## Reference code to reuse

- `~/gramercy-workstream-1/research_agent.py:333-358, 752-815` — Tavily client/search/error patterns.
- `~/personal_git/Brainstorm tsenta/applyloop` — layout conventions (pyproject, config yaml, .env.example, SQLite).
