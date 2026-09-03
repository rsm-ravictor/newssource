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

LOCAL BY DEFAULT, HOSTED ON PURPOSE
    Binds 127.0.0.1 unless told otherwise: reachable from this machine only. To
    put it on a host, set HOST=0.0.0.0 (or pass --host) and RUNNER_PASSWORD - the
    server refuses to listen on a public interface without one, because a Run
    button anyone can reach is a Tavily bill anyone can run up. Every route is
    behind HTTP Basic; there is no unauthenticated surface to get wrong.

    It has to be a long-lived process, not a serverless function: a run over the
    full roster takes hours, where the ceiling on a serverless request is minutes.
    See render.yaml.

    GitHub Pages is still the place for a *published* briefing - it serves static
    files, holds no keys, and makes no outbound calls. "Save as snapshot" freezes
    a finished run into data/ + docs/ for exactly that.

DAILY RUNS
    Set DAILY_RUN_AT=HH:MM (UTC) and one run starts itself each day, through the
    same code path as the button. Unset, nothing is scheduled - a deployment that
    began spending money on its own would be a nasty surprise.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from datetime import datetime, timedelta, timezone
from datetime import time as dt_time
import base64
import binascii
import hmac
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
    """Notes from the history store, falling back to the local file.

    The store is authoritative because a hosted runner's filesystem does not
    survive a redeploy. The file stays as the fallback so a machine with no store
    still works, and so notes written by an older build are not stranded.
    """
    try:
        return db.get_note(db.connect(), key) or _notes_file(key)
    except Exception:  # noqa: BLE001 - notes are an input, never a reason to stop
        return _notes_file(key)


def write_notes(key: str, text: str) -> None:
    """Write to the store, and to the file when there is one to write to.

    Both, not either: the store is what survives a redeploy, and the file is what
    a person can still read when the store is unreachable.
    """
    body = text.replace("\r\n", "\n")
    try:
        db.set_note(db.connect(), key, body)
    except Exception as exc:  # noqa: BLE001
        print(f"warning: could not save notes to the store: {type(exc).__name__}: {exc}")
    try:
        NOTES_DIR.mkdir(parents=True, exist_ok=True)
        notes_path(key).write_text(body, encoding="utf-8")
    except OSError:
        pass  # read-only filesystem on a hosted box; the store already has it


def _notes_file(key: str) -> str:
    path = notes_path(key)
    return path.read_text(encoding="utf-8") if path.exists() else ""


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


def execute_run(run_id: str, days: int, limit: int, resume_of: str | None = None,
                picked: dict[str, list[str]] | None = None) -> None:
    """Search -> judge -> render for every report type. Runs on a worker thread.

    Pausable: /api/stop sets ``stop`` on the run, and the entity loop checks it
    before starting each entity. A pause is therefore never a kill - the run stops
    retrieving, then walks the rest of the way through curate and render with
    whatever it already has, so the emails are built from real retrieved findings
    rather than being thrown away.

    Resumable: every finished entity is checkpointed to the history store, so a run
    whose process dies can be picked up where it stopped. ``resume_of`` is the run id
    to continue; its checkpointed entities are replayed from the store - no Tavily
    request, no judge call - and only the rest are searched. A pause is deliberately
    not resumable: it ended in a briefing the reviewer asked for.

    ``picked`` narrows the run to named entities, per report type. None means the
    whole roster. This is what makes a cheap test possible: two Tavily requests
    per company, so a five-company run costs ten, and someone deciding what counts
    as "meaningful" can iterate without spending a roster's worth of credits.
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
            if picked is not None:
                # Matched case-insensitively against the roster, and anything not
                # on it is dropped: the roster is the authority on who exists, and
                # a typo should narrow a run rather than invent an entity.
                wanted = {n.casefold() for n in picked.get(key, [])}
                entries = [e for e in entries if e.name.casefold() in wanted]
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
            Losing the audit trail must not lose the briefing.

            Retried once through a fresh connection, because the store may now be
            on the far end of a network rather than a file on this disk. Two
            things make that worth doing rather than just logging: a run lasts
            hours and a dropped connection in the middle of one is ordinary, and
            in Postgres a failed statement aborts the transaction, so without a
            reconnect the first failure would quietly disable checkpointing - and
            with it resume - for the rest of the run.
            """
            nonlocal history
            if history is None:
                return None
            try:
                return fn(history, *a, **kw)
            except Exception as exc:  # noqa: BLE001
                log(f"  ! history {what} failed: {type(exc).__name__}: {str(exc)[:80]}")
            try:
                history = db.connect()
                out = fn(history, *a, **kw)
                log("  history store reconnected")
                return out
            except Exception as exc:  # noqa: BLE001
                log(f"  ! history reconnect failed: {type(exc).__name__}: {str(exc)[:80]}")
                return None

        period = search_mod.period_label(days)
        run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        results: dict[str, dict] = {}
        skipped: list[str] = []
        # Kept apart from `skipped` because they are different facts. One report
        # type was cut short by a pause; the other was never asked for. Reporting
        # "paused first" for a type nobody selected would be a small lie in the
        # one place a reviewer looks to find out what actually ran.
        unselected: list[str] = []

        for key, spec in specs.items():
            entries = planned[key]

            # Paused before this report type began: nothing was retrieved for it, so
            # there is nothing honest to put in an email. Say so rather than sending
            # an empty briefing that reads like "we looked and found nothing".
            if paused():
                skipped.append(spec["label"])
                log(f"[{spec['label']}] not started — paused before it began")
                continue

            # Nothing chosen from this list. Rendering it would produce a briefing
            # that says "we looked and found nothing", which is not what happened.
            if not entries:
                unselected.append(spec["label"])
                log(f"[{spec['label']}] none selected — not run")
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

            # One history row per report type. A resumed run keeps writing to the
            # row it was interrupted in, so the store shows one run that took two
            # attempts rather than two runs that each did half the roster.
            hist_id = f"{resume_of or run_id}:{key}"
            done_before: dict[str, dict] = {}
            if resume_of:
                hist_run = hist_id if remember("reopen_run", db.reopen_run, hist_id) else None
                done_before = remember("checkpoints", db.checkpoints, hist_id) or {}
                # Counted against what this run will actually attempt, not against
                # every checkpoint on the row: a selected subset is a smaller
                # denominator, and "2 of 1" is worse than no number at all.
                already = sum(1 for e in entries if e.name in done_before)
                if already:
                    log(f"  resuming: {already} of {len(entries)} "
                        f"{spec['entity_noun_plural']} already screened, replaying from history")
            else:
                hist_run = remember(
                    "start_run", db.start_run, key,
                    lookback_days=days, model=model, run_id=hist_id,
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
                # Replayed before the pause check: this entity was already paid for
                # in the interrupted attempt, and reading it back costs nothing.
                seen = done_before.get(entry.name)
                if seen is not None:
                    if seen["payload"]:
                        judged.append(seen["payload"])
                    kept += seen["kept"]
                    dropped += seen["dropped"]
                    found_total += seen["articles"]
                    skipped_seen += seen["skipped"]
                    processed += 1
                    log(f"  {entry.name}: replayed from the interrupted run "
                        f"({seen['kept']} kept)")
                    bump()
                    continue

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

                seen_here = 0
                if DEDUPE:
                    fresh = remember("unseen", db.unseen, key, name, articles)
                    if fresh is not None and len(fresh) < len(articles):
                        seen_here = len(articles) - len(fresh)
                        skipped_seen += seen_here
                        log(f"  {name}: {seen_here} seen in an earlier run, not re-judged")
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
                repeats_here = 0
                # Through remember() like every other store call: this one reaches
                # the network too, and failing open repeats a finding at worst,
                # where letting it raise would lose the whole run.
                checked = remember("fresh_events", db.fresh_events, key, name,
                                   one[0]["alerts"]) if one else None
                if checked:
                    new, repeats = checked
                    repeats_here = len(repeats)
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
                # The resume point. Written after the entity is fully judged and
                # recorded, so a process that dies here leaves work that is finished
                # or absent, never half-done.
                remember(
                    "checkpoint", db.checkpoint, hist_id, name,
                    payload=one[0] if one else None,
                    # What this entity contributed to the run's totals, so a resumed
                    # run reports coverage for the whole roster and not just its own
                    # half. found_total counts what search returned, before dedupe.
                    articles=len(articles) + seen_here, skipped=seen_here,
                    kept=k, dropped=d + repeats_here,
                )
                publish_usage()
                bump()

            cut_short = processed < len(entries)
            used = meter.totals()
            # A resumed run's row already holds what the interrupted attempt spent.
            # This process only knows its own delta, so carry the earlier numbers
            # forward or the row would report half the bill.
            prior = (remember("run_row", db.run_row, hist_run) or {}) if resume_of else {}

            def carried(field: str) -> int:
                return prior.get(field) or 0

            remember(
                "finish_run", db.finish_run, hist_run,
                status="paused" if cut_short else "complete",
                entities_searched=processed, articles_found=found_total,
                articles_skipped=skipped_seen, kept=kept, dropped=dropped,
                # Delta since this report type began: one press of the button
                # writes two run rows, and each should own its share of the bill
                # rather than both reporting the run total.
                tavily_queries=carried("tavily_queries") + used["tavily_queries"] - before["tavily_queries"],
                llm_calls=carried("llm_calls") + used["llm_calls"] - before["llm_calls"],
                input_tokens=carried("input_tokens") + used["input_tokens"] - before["input_tokens"],
                output_tokens=carried("output_tokens") + used["output_tokens"] - before["output_tokens"],
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
        if unselected:
            log("not run (nothing selected): " + ", ".join(unselected))

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


def launch_run(days: int, limit: int, resume_of: str | None = None,
               picked: dict[str, list[str]] | None = None) -> str | None:
    """Register a run and start its worker thread. None if one is already going.

    Shared by the button and the schedule so there is exactly one way a run
    starts. The in-progress check and the registration happen under one lock, or
    two requests a millisecond apart could both find the runner idle.
    """
    with RUNS_LOCK:
        if any(r["state"] == "running" for r in RUNS.values()):
            return None
        run_id = uuid.uuid4().hex[:12]
        RUNS[run_id] = {
            "state": "running", "log": [], "done": 0, "total": 0, "reports": {},
            "step": "list", "steps_done": [], "step_notes": {},
            # Pause flag, and whether the finished run actually honoured one.
            "stop": False, "stopped": False,
            # Measured provider usage, replaced after every entity.
            "usage": Meter().totals(),
        }
    threading.Thread(target=execute_run, args=(run_id, days, limit, resume_of, picked),
                     daemon=True).start()
    return run_id


def next_occurrence(at: dt_time, now: datetime | None = None) -> datetime:
    """The next UTC datetime matching a wall-clock time. Today if it has not
    passed, tomorrow if it has."""
    now = now or datetime.now(timezone.utc)
    target = now.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    return target if target > now else target + timedelta(days=1)


def parse_daily_time(raw: str) -> dt_time | None:
    """HH:MM, UTC. Anything else is None, and the caller says so out loud rather
    than silently never running."""
    try:
        hour, _, minute = raw.strip().partition(":")
        return dt_time(int(hour), int(minute))
    except (ValueError, AttributeError):
        return None


def schedule_daily(at: dt_time, days: int, limit: int) -> None:
    """Start one run a day, forever, on a worker thread.

    Deliberately a sleep loop rather than a cron dependency: the process is
    already long-lived - that is the whole reason the runner is hosted this way -
    and a scheduler that lives inside it cannot disagree with it about whether a
    run is already going. A run in progress when the hour comes round is left
    alone; the next day's tick will do it.
    """
    while True:
        due = next_occurrence(at)
        time.sleep(max(1.0, (due - datetime.now(timezone.utc)).total_seconds()))
        run_id = launch_run(days, limit)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        if run_id:
            print(f"[{stamp}] scheduled run started ({days}-day window)")
        else:
            print(f"[{stamp}] scheduled run skipped - a run was already in progress")


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


# Set when the server is told to listen anywhere but loopback. None means the
# old behaviour exactly: a local tool on a local port, no password, no prompt.
PASSWORD: str | None = None


class Handler(BaseHTTPRequestHandler):
    server_version = "TenantIntelRunner/1.0"

    def authorized(self) -> bool:
        """HTTP Basic, checked on every route.

        This page can start a run that spends real money and shows real findings
        about real companies, so exposing it without a password is not a
        configuration choice anyone should be able to make by accident - see
        main(), which refuses to bind a public interface without one.

        Any username is accepted; the password is the whole secret. compare_digest
        because a timing side channel on a shared password is worth the one line
        it costs to avoid.
        """
        if PASSWORD is None:
            return True
        header = self.headers.get("Authorization") or ""
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8", "replace")
        except (ValueError, binascii.Error):
            return False
        _, _, supplied = decoded.partition(":")
        return hmac.compare_digest(supplied, PASSWORD)

    def demand_password(self) -> None:
        body = b"Authentication required."
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Intel runner"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
        if not self.authorized():
            return self.demand_password()
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

        # What a killed run left behind. One button press writes a history row per
        # report type, so the rows are grouped back into the single run the
        # reviewer actually started.
        # The rosters, for the picker. Names only - the same fields the search
        # stage is allowed to see, and the same ones already on the reference page.
        if path == "/api/entities":
            config = load_config()
            out = {}
            for key, spec in config["report_types"].items():
                entries = read_entries(ROOT / spec["watchlist"], spec.get("name_aliases"))
                out[key] = {
                    "label": spec["label"],
                    "noun": spec["entity_noun_plural"],
                    "entities": [
                        {"name": e.name, "city": e.city, "rank": e.rank, "segment": e.segment}
                        for e in entries
                    ],
                }
            queries = max((len(s.get("query_templates") or []) for s in config["report_types"].values()),
                          default=2)
            return self._json({"types": out, "queries_per_entity": queries})

        if path == "/api/resumable":
            try:
                rows = db.resumable(db.connect())
            except Exception as exc:  # noqa: BLE001 - no history is not an error here
                return self._json({"runs": [], "error": f"{type(exc).__name__}: {exc}"})
            grouped: dict[str, dict] = {}
            for row in rows:
                head, _, key = row["run_id"].partition(":")
                run = grouped.setdefault(head, {
                    "id": head, "started_at": row["started_at"],
                    "days": row["lookback_days"], "finished": 0, "types": {},
                })
                run["finished"] += row["finished"]
                run["types"][key] = row["finished"]
                run["started_at"] = min(run["started_at"], row["started_at"])
            return self._json({"runs": sorted(
                grouped.values(), key=lambda r: r["started_at"], reverse=True)})

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
        if not self.authorized():
            return self.demand_password()
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

            # Resuming an interrupted run. The lookback comes from the run being
            # resumed, never from the page: the evidence window is part of what was
            # already judged, and mixing two windows into one briefing would make
            # its date range a lie.
            resume_of = (body.get("resume") or "").strip() or None
            if resume_of:
                match = [r for r in db.resumable(db.connect())
                         if r["run_id"].partition(":")[0] == resume_of]
                if not match:
                    return self._json({"error": "no interrupted run with that id"}, 404)
                stored_days = next((r["lookback_days"] for r in match if r["lookback_days"]), None)
                if stored_days:
                    days = stored_days

            # A selection narrows the run to named companies. Absent means the
            # whole roster; present but empty would mean "run nothing", which is
            # a mistake rather than an instruction, so it is rejected.
            picked = body.get("entities")
            if picked is not None:
                if not isinstance(picked, dict):
                    return self._json({"error": "entities must be an object keyed by report type"}, 400)
                known = set(load_config()["report_types"])
                picked = {k: [str(n) for n in v] for k, v in picked.items()
                          if k in known and isinstance(v, list)}
                if not any(picked.values()):
                    return self._json({"error": "no companies selected"}, 400)

            run_id = launch_run(days, limit, resume_of, picked)
            if run_id is None:
                return self._json({"error": "a run is already in progress"}, 409)
            return self._json({"run_id": run_id, "days": days, "resumed": bool(resume_of)})

        # Stop retrieving. The worker notices between entities and then finishes the
        # run normally - review, curate, render - on what it already has. One-way:
        # a paused run cannot be resumed - it ended in the briefing that was asked
        # for. Resume is for a run whose process died, which is a different thing.
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
    # A hosted process writes to a log pipe, not a terminal, and Python
    # block-buffers a pipe: without this the startup lines and every scheduled-run
    # message sit in a buffer for hours. Logs nobody can read are not logs.
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # PORT and HOST come from the environment because that is how every hosting
    # platform tells a process where to listen. Unset, the defaults are what they
    # always were: this machine only.
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT") or 8765))
    ap.add_argument("--host", default=os.environ.get("HOST") or "127.0.0.1",
                    help="default is loopback only, on purpose")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    # The interlock. A run started from this page spends real money and shows real
    # findings, so the server will not listen anywhere but this machine unless a
    # password exists to put in front of it. Refusing to start is the only
    # response that cannot be ignored.
    global PASSWORD
    PASSWORD = os.environ.get("RUNNER_PASSWORD") or None
    local_only = args.host in ("127.0.0.1", "localhost", "::1")
    if not local_only and not PASSWORD:
        print(f"refusing to listen on {args.host} without a password.\n"
              "  Set RUNNER_PASSWORD, or bind 127.0.0.1 to keep it local.",
              file=sys.stderr)
        return 2
    if PASSWORD and len(PASSWORD) < 12:
        print(f"refusing to start: RUNNER_PASSWORD is {len(PASSWORD)} characters.\n"
              "  This page can spend money and is reachable from the internet; use 12 or more.",
              file=sys.stderr)
        return 2

    missing = [k for k in ("TAVILY_API_KEY", "TRITONAI_API_KEY") if not os.environ.get(k)]
    if missing:
        print(f"warning: {', '.join(missing)} not set in .env - the page will say so too")

    # A run's live state is held in this process's memory, so any run row still
    # marked running belongs to a server that is gone. Close them out here rather
    # than leaving the history claiming work is in flight.
    try:
        stale = db.sweep_interrupted(db.connect())
        if stale:
            print(f"marked {stale} interrupted run(s) from a previous session")
    except Exception as exc:  # history is a convenience; never block the server
        print(f"warning: could not sweep interrupted runs: {exc}")

    # The daily run. Off unless DAILY_RUN_AT is set, because a schedule that
    # starts itself the first time the app is deployed would spend money nobody
    # asked it to spend.
    raw_at = os.environ.get("DAILY_RUN_AT", "").strip()
    if raw_at:
        at = parse_daily_time(raw_at)
        if at is None:
            print(f"refusing to start: DAILY_RUN_AT={raw_at!r} is not HH:MM (UTC).",
                  file=sys.stderr)
            print("  A schedule that silently never fires is worse than no schedule.",
                  file=sys.stderr)
            return 2
        config = load_config()
        presets = config.get("lookback_presets", [7, 30, 90])
        try:
            daily_days = int(os.environ.get("DAILY_RUN_DAYS") or 1)
        except ValueError:
            daily_days = 1
        if daily_days not in presets:
            print(f"refusing to start: DAILY_RUN_DAYS={daily_days} is not one of {presets}",
                  file=sys.stderr)
            return 2
        try:
            daily_limit = max(0, int(os.environ.get("DAILY_RUN_LIMIT") or 0))
        except ValueError:
            daily_limit = 0

        scope = f"{daily_limit} entities" if daily_limit else "the whole roster"
        print(f"daily run at {at.strftime('%H:%M')} UTC - {daily_days}-day window over {scope}"
              f" (next: {next_occurrence(at):%Y-%m-%d %H:%M} UTC)")
        threading.Thread(target=schedule_daily, args=(at, daily_days, daily_limit),
                         daemon=True).start()

    url = f"http://{args.host}:{args.port}/"
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"live runner on {url}   (Ctrl+C to stop)")
    print("password required" if PASSWORD else "no password - local only")
    # Only ever on this machine. A hosted process has no browser to open, and
    # opening one there would be a request from the server to itself.
    if not args.no_browser and local_only:
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
