"""Local live runner - pick a date range, click Run, watch a real pipeline execute.

    python serve.py                 # http://127.0.0.1:8765
    python serve.py --port 9000 --no-browser

Two pages:
    /            the runner - Push run now, Pause & build email, a status bar for
                 the five workflow stages, and the rendered emails in desktop or
                 phone chrome
    /reference   review-only: both rosters, the guidelines that define "meaningful",
                 and an editable notes box that feeds the next run

Search (Tavily) -> judge (TritonAI) -> render, over the real watchlists, with
per-entity progress streamed to the page. Both API keys are read from .env and stay
in this process; nothing secret is ever sent to the browser.

WHY THIS IS LOCAL-ONLY: GitHub Pages serves static files and cannot hold API keys or
make outbound calls, and a public Run button would let anyone spend your Tavily
credits. Use "Save as snapshot" to freeze a completed run into data/ + docs/, then
commit and push to publish it to the Pages link.

Binds to 127.0.0.1 by default: reachable from this machine only, not the network.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import traceback
import uuid
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv

load_dotenv()  # before utils.connect is imported anywhere

import db  # noqa: E402
import search as search_mod  # noqa: E402
from judge import judge_entities  # noqa: E402
from prose import structure  # noqa: E402
from usage import Meter  # noqa: E402
from sources import citable_limit, source_tiers  # noqa: E402
from render import (  # noqa: E402
    DOCS,
    ROOT,
    build_context,
    default_model_note,
    get_env,
    load_config,
    read_entries,
    render_digest,
    report_meta,
)

DEFAULT_PRIORITIES = ["high", "medium"]

# Cross-run dedupe is correct for a scheduled email build - never alert the same URL
# twice - but it makes a second demo run over the same roster look empty, because
# Tavily returns the same stories inside one lookback window. So history is always
# WRITTEN and the filter is opt-in: the page behaves identically every time it is
# driven, and the email build turns this on.
DEDUPE = os.environ.get("NEWS_DEDUPE", "").strip().lower() in {"1", "true", "yes", "on"}

# The workflow the status bar shows, in order. Keys are what the run reports; the
# labels are what the page prints. Deliberately defined here rather than in the
# template: the server is what actually knows which stage it is in.
STEPS: list[tuple[str, str]] = [
    ("list", "Reading List"),
    ("search", "Searching News Sources (Tavily)"),
    ("review", "Reviewing/Prioritizing Meaningful News (Claude)"),
    ("curate", "Curating Email"),
    ("done", "Done"),
]

# Reviewer notes from the reference page. Kept out of the repo: they are written
# about real tenants and competitors.
NOTES_DIR = ROOT / "data" / "notes"

# run_id -> mutable state dict, guarded by RUNS_LOCK.
RUNS: dict[str, dict] = {}
RUNS_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Reviewer notes
# ---------------------------------------------------------------------------


def notes_path(key: str) -> Path:
    """One notes file per report type. The key is validated against the config."""
    return NOTES_DIR / f"{key}.md"


def read_notes(key: str) -> str:
    path = notes_path(key)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def write_notes(key: str, text: str) -> None:
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    notes_path(key).write_text(text.replace("\r\n", "\n"), encoding="utf-8")


def default_days(config: dict) -> int:
    """The lookback preset the page loads with.

    Read from ``lookback_default`` rather than taken as the first preset, so the
    cheap 1-day test window can sit at the front of the bar without becoming what a
    run uses when nobody picked a range. Falls back to the first preset, and to it
    again if the configured default is not one of the offered windows.
    """
    presets = config.get("lookback_presets", [7, 30, 90])
    value = config.get("lookback_default")
    return value if value in presets else presets[0]


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------


def execute_run(run_id: str, days: int, limit: int) -> None:
    """Search -> judge -> render for every report type. Runs on a worker thread.

    Pausable: /api/stop sets ``stop`` on the run, and the entity loop checks it
    before starting each entity. A pause is therefore never a kill - the run stops
    retrieving, then walks the rest of the way through curate and render with
    whatever it already has, so the emails are built from real retrieved findings
    rather than being thrown away. There is no resume: the next run starts over.
    """
    config = load_config()
    specs = config["report_types"]
    search_cfg = config.get("search", {})
    env = get_env()
    from utils.connect import DEFAULT_MODEL as model

    # Evidence policy for this run, read once. The window is the same range the
    # user picked: a finding has to be about something that happened inside it,
    # not merely written about inside it.
    standard = config.get("evidence_standard", "")
    tiers = source_tiers(config)
    citable_max = citable_limit(config)
    window_start = (datetime.now(timezone.utc).date() - timedelta(days=days)) if days else None

    # Measured API usage for this run. Shared by both report types, because one
    # press of the button is one bill.
    meter = Meter()

    def publish_usage() -> None:
        with RUNS_LOCK:
            RUNS[run_id]["usage"] = meter.totals()

    def log(msg: str) -> None:
        with RUNS_LOCK:
            RUNS[run_id]["log"].append(msg)

    def bump() -> None:
        with RUNS_LOCK:
            RUNS[run_id]["done"] += 1

    def paused() -> bool:
        """Has the reviewer pressed Pause? Checked between entities, never mid-call."""
        with RUNS_LOCK:
            return RUNS[run_id]["stop"]

    def stage(key: str, note: str = "", *, done: bool = False) -> None:
        """Move the status bar.

        A stage is recorded as reached the moment it starts, because search and
        review alternate per entity: while entity 5 is being searched, review has
        genuinely finished for entities 1-4 and should read that way rather than
        dropping back to pending. ``done`` additionally retires every earlier
        stage, which covers the ones a short run never lingers in.
        """
        order = [k for k, _ in STEPS]
        with RUNS_LOCK:
            run = RUNS[run_id]
            run["step"] = key
            if note:
                run["step_notes"][key] = note
            reached = order[: order.index(key) + 1] if done else [key]
            for k in reached:
                if k not in run["steps_done"]:
                    run["steps_done"].append(k)

    try:
        stage("list")
        # Count the work up front so the progress bar is honest.
        planned: dict[str, list] = {}
        for key, spec in specs.items():
            entries = read_entries(ROOT / spec["watchlist"], spec.get("name_aliases"))
            planned[key] = entries[:limit] if limit else entries
        with RUNS_LOCK:
            RUNS[run_id]["total"] = sum(len(v) for v in planned.values())
        stage(
            "list",
            ", ".join(f"{len(v)} {specs[k]['entity_noun_plural']}" for k, v in planned.items()),
            done=True,
        )

        # Both clients pass through the meter, so what the page reports is what
        # the providers reported - not arithmetic over the roster. connect.py is
        # verbatim-locked and discards resp.usage, but it accepts a client, and
        # that injection point is where the real token counts are captured.
        tavily = meter.wrap_tavily(search_mod.get_client())
        from utils.connect import get_client as llm_client

        llm = meter.wrap_llm(llm_client())

        # History belongs to the pipeline, not to this page: an email-only build that
        # drops the UI keeps the same store by calling the same functions. Opened in
        # the run thread because sqlite3 connections are not shared across threads.
        history = None
        try:
            history = db.connect()
        except Exception as exc:  # noqa: BLE001
            log(f"  ! history store unavailable ({type(exc).__name__}) - run continues without it")

        def remember(what: str, fn, *a, **kw):
            """Best-effort store call: a history failure is logged, never fatal.
            Losing the audit trail must not lose the briefing."""
            if history is None:
                return None
            try:
                return fn(history, *a, **kw)
            except Exception as exc:  # noqa: BLE001
                log(f"  ! history {what} failed: {type(exc).__name__}: {str(exc)[:80]}")
                return None

        period = search_mod.period_label(days)
        run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        results: dict[str, dict] = {}
        skipped: list[str] = []

        for key, spec in specs.items():
            entries = planned[key]

            # Paused before this report type began: nothing was retrieved for it, so
            # there is nothing honest to put in an email. Say so rather than sending
            # an empty briefing that reads like "we looked and found nothing".
            if paused():
                skipped.append(spec["label"])
                log(f"[{spec['label']}] not started — paused before it began")
                continue

            log(f"[{spec['label']}] {len(entries)} {spec['entity_noun_plural']}, {period}")

            # Whatever the reviewer typed on the reference page sharpens the criteria
            # for this run only; nothing in config/report_types.yaml is rewritten.
            notes = read_notes(key)
            if notes.strip():
                log(f"  applying reference-page notes ({len(notes.split())} words)")

            # Rank is counted within a roster segment ("#12 of 271 Office tenants"),
            # so the denominator has to be per segment, not per file.
            segment_totals: dict[str, int] = {}
            for entry in entries:
                segment_totals[entry.segment] = segment_totals.get(entry.segment, 0) + 1

            hist_run = remember(
                "start_run", db.start_run, key,
                lookback_days=days, model=model, run_id=f"{run_id}:{key}",
            )

            # Meter reading as this type starts, so its own usage is the delta.
            before = meter.totals()

            judged: list[dict] = []
            kept = dropped = 0
            found_total = skipped_seen = 0
            processed = 0
            failed: list[str] = []

            # Search and judge one entity at a time. Interleaving keeps the progress
            # bar honest (one step per entity, start to finish) and preserves the
            # one-entity-at-a-time shape the privacy model wants. The status bar
            # flips between Searching and Reviewing with it, which is what is
            # actually happening.
            for entry in entries:
                # Checked here, at the top of the entity, so an entity is never left
                # half-done: whatever was searched has also been judged.
                if paused():
                    log(f"  paused after {processed} of {len(entries)} "
                        f"{spec['entity_noun_plural']} — curating what was retrieved")
                    break

                name, city = entry.name, entry.city
                rank_tag = f" (#{entry.rank})" if entry.rank else ""
                stage("search", f"{name}{rank_tag}")
                log(f"searching {name}")
                articles = search_mod.search_entity(
                    tavily,
                    name,
                    city,
                    templates=spec["query_templates"],
                    days=days,
                    max_results=search_cfg.get("max_results_per_query", 5),
                    search_depth=search_cfg.get("search_depth", "basic"),
                    sleep=search_cfg.get("sleep_between_calls", 0.5),
                    on_error=lambda m: log(f"  ! {m}"),
                )
                log(f"  {name}: {len(articles)} articles found")
                found_total += len(articles)

                if DEDUPE:
                    fresh = remember("unseen", db.unseen, key, name, articles)
                    if fresh is not None and len(fresh) < len(articles):
                        skipped_seen += len(articles) - len(fresh)
                        log(f"  {name}: {len(articles) - len(fresh)} seen in an earlier run, not re-judged")
                        articles = fresh

                stage("review", f"{name}{rank_tag} · {len(articles)} articles")
                one, k, d, f = judge_entities(
                    [{
                        "display_name": name,
                        "city": city,
                        # Only sent for a ranked report type; judge.py omits the
                        # line entirely when rank is absent.
                        "rank": entry.rank if spec.get("use_rank") else None,
                        "rank_of": segment_totals.get(entry.segment),
                        "segment": entry.segment,
                        "articles": articles,
                    }],
                    spec,
                    model=model,
                    client=llm,
                    progress=log,
                    notes=notes,
                    standard=standard,
                    tiers=tiers,
                    citable_max=citable_max,
                    window_start=window_start,
                )

                # An event already reported in an earlier briefing is not news this
                # week, whichever outlet carried it this time. Runs without the
                # history store skip this and may repeat themselves - logged, not
                # silent.
                if one and history is not None:
                    new, repeats = db.fresh_events(history, key, name, one[0]["alerts"])
                    if repeats:
                        for r in repeats:
                            log(f"    - {r['headline'][:52]} (already reported: {r['event_key']})")
                        k -= len(repeats)
                        dropped += len(repeats)
                        one[0]["alerts"] = new
                        if not new:
                            one = []

                judged += one
                kept += k
                dropped += d
                failed += f
                processed += 1

                # Per entity rather than per run: a paused or crashed run still has
                # everything it finished judging.
                remember(
                    "record", db.record, hist_run, key,
                    {
                        "display_name": name,
                        "city": city,
                        "rank": entry.rank if spec.get("use_rank") else None,
                        "segment": entry.segment,
                    },
                    articles,
                    one[0]["alerts"] if one else [],
                )
                publish_usage()
                bump()

            cut_short = processed < len(entries)
            used = meter.totals()
            remember(
                "finish_run", db.finish_run, hist_run,
                status="paused" if cut_short else "complete",
                entities_searched=processed, articles_found=found_total,
                articles_skipped=skipped_seen, kept=kept, dropped=dropped,
                # Delta since this report type began: one press of the button
                # writes two run rows, and each should own its share of the bill
                # rather than both reporting the run total.
                tavily_queries=used["tavily_queries"] - before["tavily_queries"],
                llm_calls=used["llm_calls"] - before["llm_calls"],
                input_tokens=used["input_tokens"] - before["input_tokens"],
                output_tokens=used["output_tokens"] - before["output_tokens"],
            )
            log(f"[{spec['label']}] used "
                f"{used['tavily_queries'] - before['tavily_queries']} Tavily requests, "
                f"{used['llm_calls'] - before['llm_calls']} LLM calls, "
                f"{used['input_tokens'] - before['input_tokens']:,} in / "
                f"{used['output_tokens'] - before['output_tokens']:,} out tokens")
            publish_usage()
            if cut_short and not processed:
                skipped.append(spec["label"])
                log(f"[{spec['label']}] paused before any {spec['entity_noun']} was "
                    "retrieved — no email rendered")
                continue

            log(f"{spec['label']}: {kept} findings kept, {dropped} excluded"
                + (f", {len(failed)} skipped" if failed else ""))
            # Not retired yet: the next report type searches and reviews too, and the
            # bar should say so rather than claiming those stages are finished.
            stage("review", f"{kept} kept, {dropped} excluded")
            stage("curate", f"{spec['label']} — {kept} findings"
                            + (" (paused)" if cut_short else ""))

            # A paused run screened fewer entities than the roster holds, and the
            # email says how many it screened. Both numbers have to reflect the
            # pause or the briefing overstates its own coverage.
            monitored = processed
            period_label = period
            if cut_short:
                period_label = (f"{period} · paused after {processed} of {len(entries)} "
                                f"{spec['entity_noun_plural']}")
            context = build_context(
                spec,
                config,
                entities=judged,
                priorities=DEFAULT_PRIORITIES,
                monitored=monitored,
                run_date=run_date,
                period_label=period_label,
            )
            email_html = render_digest(env, context)
            meta = report_meta(key, spec, context, email_html, f"digest-{key}.html")

            results[key] = {
                "email_html": email_html,
                "subject": meta["subject"],
                "preheader": meta["preheader"],
                "sent_label": meta["sent_label"],
                "total_alerts": context["total_alerts"],
                "total_companies": context["total_companies"],
                # Kept server-side for the snapshot writer.
                "_alerts": judged,
                "_run_date": run_date,
                "_period": period_label,
                "_monitored": monitored,
            }

        was_paused = paused()
        if skipped:
            log("not rendered (paused first): " + ", ".join(skipped))

        findings = sum(r["total_alerts"] for r in results.values())
        stage("curate", f"{len(results)} {'email' if len(results) == 1 else 'emails'} rendered", done=True)
        stage(
            "done",
            f"{findings} {'finding' if findings == 1 else 'findings'} in {len(results)} briefings"
            + (" — paused early" if was_paused else ""),
            done=True,
        )

        with RUNS_LOCK:
            RUNS[run_id]["reports"] = results
            RUNS[run_id]["days"] = days
            RUNS[run_id]["stopped"] = was_paused
            RUNS[run_id]["state"] = "done"
        log("paused — email built from what was retrieved" if was_paused else "done")

    except Exception as exc:  # noqa: BLE001 - surface it to the page, don't kill the server
        traceback.print_exc()
        with RUNS_LOCK:
            RUNS[run_id]["state"] = "error"
            RUNS[run_id]["error"] = f"{type(exc).__name__}: {exc}"
            # Leaves "step" pointing at the stage that broke, so the bar marks it.
            RUNS[run_id]["failed_step"] = RUNS[run_id].get("step")


def step_states(run: dict) -> list[dict]:
    """The status bar's view of one run: every step with its state and note.

    "active" beats "done" on purpose. Search and review interleave per entity and
    repeat for the second report type, so a step that finished for tenants can be
    running again for competitors - saying so is more honest than a bar that only
    ever moves forward.
    """
    current = run.get("step")
    failed = run.get("failed_step")
    out = []
    for key, label in STEPS:
        if key == failed:
            state = "failed"
        elif key == current and run.get("state") == "running":
            state = "active"
        elif key in run.get("steps_done", []):
            state = "done"
        else:
            state = "pending"
        out.append({"key": key, "label": label, "state": state,
                    "note": run.get("step_notes", {}).get(key, "")})
    return out


def write_snapshot(run_id: str) -> list[str]:
    """Freeze a finished run into data/*.json, then rebuild docs/ from it."""
    with RUNS_LOCK:
        run = RUNS.get(run_id)
        if not run or run.get("state") != "done":
            raise ValueError("run is not finished")
        reports = run["reports"]
    if not reports:
        raise ValueError("this run was paused before it retrieved anything - nothing to save")

    specs = load_config()["report_types"]
    written = []
    for key, result in reports.items():
        spec = specs[key]
        payload = {
            "_note": (
                f"LIVE run snapshot via serve.py on {result['_run_date']} "
                f"({result['_period']}). Real search results, not fixtures."
            ),
            "run_date": result["_run_date"],
            "period_label": result["_period"],
            "portfolio_size": result["_monitored"],
            "companies": result["_alerts"],
        }
        path = ROOT / spec["alerts"]
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        written.append(spec["alerts"])

    # Rebuild the published preview through the normal static path so docs/ and
    # data/ can never disagree.
    import subprocess
    import sys as _sys

    proc = subprocess.run(
        [_sys.executable, str(ROOT / "build_preview.py")],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"build_preview.py failed: {proc.stderr[-300:]}")
    written.append("docs/")
    return written


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "TenantIntelRunner/1.0"

    def log_message(self, fmt, *args):  # quieter console
        if "api/status" not in (args[0] if args else ""):
            super().log_message(fmt, *args)

    # -- helpers ----------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

    def _html(self, text: str) -> None:
        self._send(200, text.encode("utf-8"), "text/html; charset=utf-8")

    # -- routes -----------------------------------------------------------
    def do_GET(self):  # noqa: N802
        route = urlparse(self.path)
        path = route.path.rstrip("/") or "/"

        if path == "/":
            return self._html(render_page())

        # Review-only page: the rosters, the guidelines behind "meaningful", and the
        # notes box. Nothing here is ever part of an email.
        if path == "/reference":
            return self._html(render_reference())

        if path == "/api/notes":
            key = (parse_qs(route.query).get("key") or [""])[0]
            if key not in load_config()["report_types"]:
                return self._json({"error": "unknown report type"}, 404)
            return self._json({"key": key, "notes": read_notes(key)})

        if path == "/api/status":
            run_id = (parse_qs(route.query).get("id") or [""])[0]
            with RUNS_LOCK:
                run = RUNS.get(run_id)
                if not run:
                    return self._json({"error": "unknown run id"}, 404)
                payload = {
                    "state": run["state"],
                    "log": list(run["log"]),
                    "done": run["done"],
                    "total": run["total"],
                    "days": run.get("days"),
                    "error": run.get("error"),
                    "steps": step_states(run),
                    # Pause requested but the entity in flight is still finishing.
                    "pausing": bool(run.get("stop")) and run["state"] == "running",
                    # What the providers actually reported, not an estimate.
                    "usage": run.get("usage") or {},
                    "paused": bool(run.get("stopped")),
                }
                if run["state"] == "done":
                    # Strip the private "_"-prefixed keys before they reach the page.
                    payload["reports"] = {
                        k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")}
                        for k, v in run["reports"].items()
                    }
            return self._json(payload)

        # Serve the built raw emails so "Open raw" works during a session.
        if path.startswith("/digest-") and path.endswith(".html"):
            candidate = (DOCS / Path(path).name).resolve()
            if candidate.is_file() and candidate.parent == DOCS.resolve():
                return self._html(candidate.read_text(encoding="utf-8"))
            return self._json({"error": "not built yet"}, 404)

        return self._json({"error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        route = urlparse(self.path)
        path = route.path.rstrip("/") or "/"

        if path == "/api/run":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad JSON body"}, 400)

            config = load_config()
            presets = config.get("lookback_presets", [7, 30, 90])
            try:
                days = int(body.get("days", default_days(config)))
            except (TypeError, ValueError):
                return self._json({"error": "days must be an integer"}, 400)
            if days not in presets:
                return self._json({"error": f"days must be one of {presets}"}, 400)
            try:
                limit = max(0, int(body.get("limit", 0)))
            except (TypeError, ValueError):
                limit = 0

            with RUNS_LOCK:
                if any(r["state"] == "running" for r in RUNS.values()):
                    return self._json({"error": "a run is already in progress"}, 409)
                run_id = uuid.uuid4().hex[:12]
                RUNS[run_id] = {
                    "state": "running", "log": [], "done": 0, "total": 0, "reports": {},
                    "step": "list", "steps_done": [], "step_notes": {},
                    # Pause flag, and whether the finished run actually honoured one.
                    "stop": False, "stopped": False,
                    # Measured provider usage, replaced after every entity.
                    "usage": Meter().totals(),
                }

            threading.Thread(target=execute_run, args=(run_id, days, limit), daemon=True).start()
            return self._json({"run_id": run_id})

        # Stop retrieving. The worker notices between entities and then finishes the
        # run normally - review, curate, render - on what it already has. One-way:
        # a paused run cannot be resumed, the next run starts from the top.
        if path == "/api/stop":
            run_id = (parse_qs(route.query).get("id") or [""])[0]
            with RUNS_LOCK:
                run = RUNS.get(run_id)
                if not run:
                    return self._json({"error": "unknown run id"}, 404)
                if run["state"] != "running":
                    return self._json({"error": "run is not in progress"}, 409)
                run["stop"] = True
                run["log"].append("pause requested — finishing the entity in flight")
            return self._json({"pausing": True})

        if path == "/api/notes":
            key = (parse_qs(route.query).get("key") or [""])[0]
            if key not in load_config()["report_types"]:
                return self._json({"error": "unknown report type"}, 404)
            length = int(self.headers.get("Content-Length") or 0)
            if length > 200_000:
                return self._json({"error": "notes too long"}, 413)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad JSON body"}, 400)
            text = body.get("notes")
            if not isinstance(text, str):
                return self._json({"error": "notes must be a string"}, 400)
            write_notes(key, text)
            rel = notes_path(key).relative_to(ROOT).as_posix()
            return self._json({"key": key, "saved": True, "path": rel})

        if path == "/api/snapshot":
            run_id = (parse_qs(route.query).get("id") or [""])[0]
            try:
                written = write_snapshot(run_id)
            except Exception as exc:  # noqa: BLE001
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)
            return self._json({"written": written})

        return self._json({"error": "not found"}, 404)


def render_page() -> str:
    """Render runner.html with placeholder chrome plus live counts and cost."""
    config = load_config()
    specs = config["report_types"]
    env = get_env()

    reports = []
    entity_counts = {}
    watchlist_paths = {}
    for key, spec in specs.items():
        entries = read_entries(ROOT / spec["watchlist"], spec.get("name_aliases"))
        entity_counts[key] = len(entries)
        watchlist_paths[key] = spec["watchlist"]

        # Empty context: the frame shows a "no run yet" digest until Run is clicked.
        context = build_context(
            spec,
            config,
            entities=[],
            priorities=DEFAULT_PRIORITIES,
            monitored=len(entries),
            run_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            period_label="no run yet — pick a range and click Push run now",
            empty=True,
        )
        reports.append(report_meta(key, spec, context, render_digest(env, context), f"digest-{key}.html"))

    queries = max((len(s.get("query_templates") or []) for s in specs.values()), default=2)
    return env.get_template("runner.html").render(
        reports=reports,
        default_type=reports[0]["key"],
        presets=config.get("lookback_presets", [7, 30, 90]),
        default_days=default_days(config),
        entity_counts=entity_counts,
        queries_per_entity=queries,
        watchlist_paths=watchlist_paths,
        # Rendered pending; /api/status replaces the states as the run moves.
        steps=[{"key": k, "label": lab, "state": "pending", "note": ""} for k, lab in STEPS],
        model_note=f"default model {default_model_note()}",
        has_tavily=bool(os.environ.get("TAVILY_API_KEY")),
        has_tritonai=bool(os.environ.get("TRITONAI_API_KEY")),
    )


def render_reference() -> str:
    """The review page: one tab per report type, rosters plus the meaningfulness rules.

    Everything here is read from the same places a live run reads - the rosters and
    config/report_types.yaml - so what a reviewer approves is what the run uses. The
    notes box is the one writable surface, and judge.py appends it to the criteria.
    """
    config = load_config()
    env = get_env()
    tabs = []

    for key, spec in config["report_types"].items():
        entries = read_entries(ROOT / spec["watchlist"], spec.get("name_aliases"))
        ranked = bool(spec.get("use_rank"))

        # Rank is counted within a segment, so the roster is grouped by it. Files
        # with no sections land in a single unnamed group.
        sections: list[dict] = []
        by_section: dict[str, list] = {}
        for entry in entries:
            by_section.setdefault(entry.segment, []).append(entry)
        for name, rows in by_section.items():
            if ranked:
                rows = sorted(rows, key=lambda e: (e.rank is None, e.rank or 0))
            sections.append({"name": name, "count": len(rows), "rows": rows})

        source = ROOT / spec["watchlist"]
        tabs.append({
            "key": key,
            "label": spec["label"],
            "brandmark": spec["brandmark"],
            "entity_noun": spec["entity_noun"],
            "entity_noun_plural": spec["entity_noun_plural"],
            "ranked": ranked,
            "count": len(entries),
            "source": spec["watchlist"],
            "source_exists": source.exists(),
            "sections": sections,
            "categories": spec["categories"],
            # Structured rather than raw: the page renders paragraphs and lists,
            # not the YAML file's line breaks. prose.py does the reflow, and the
            # words are untouched.
            "criteria": structure(spec["criteria"]),
            # Escalation is rank-based on the tenants side and tier-based on the
            # competitors side. Both keys are always present so the template can
            # branch on them under StrictUndefined.
            "rank_note": structure(spec.get("rank_note") or ""),
            "tier_note": structure(spec.get("tier_note") or ""),
            "query_templates": spec["query_templates"],
            "privacy_note": spec["privacy_note"],
            "notes": read_notes(key),
            "notes_path": str(notes_path(key).relative_to(ROOT)).replace("\\", "/"),
        })

    # Source tiers are shared by both report types, so they ride alongside `tabs`
    # rather than inside one. Same contract as the rubrics: the page shows the list
    # the run actually sorts by, read from the same config on the same request.
    tier_block = config.get("source_tiers") or {}

    return env.get_template("reference.html").render(
        tabs=tabs,
        source_tiers=tier_block.get("order") or [],
        default_tier_position=tier_block.get("default_position"),
        # The evidence standard and the citation line are shared by both types, and
        # both are shown verbatim: the page's whole promise is that it prints what
        # the run actually applies, not a description of it.
        evidence_standard=structure(config.get("evidence_standard") or ""),
        citable_max_position=tier_block.get("citable_max_position"),
        # _canvas_css.html generates its per-type visibility rules from `reports`.
        reports=tabs,
        default_type=tabs[0]["key"],
        priorities=config["priorities"],
        model_note=default_model_note(),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1", help="default is loopback only, on purpose")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    missing = [k for k in ("TAVILY_API_KEY", "TRITONAI_API_KEY") if not os.environ.get(k)]
    if missing:
        print(f"warning: {', '.join(missing)} not set in .env - the page will say so too")

    url = f"http://{args.host}:{args.port}/"
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"live runner on {url}   (Ctrl+C to stop)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
