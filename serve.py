"""Local live runner - pick a date range, click Run, watch a real pipeline execute.

    python serve.py                 # http://127.0.0.1:8765
    python serve.py --port 9000 --no-browser

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
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv

load_dotenv()  # before utils.connect is imported anywhere

import search as search_mod  # noqa: E402
from judge import judge_entities  # noqa: E402
from render import (  # noqa: E402
    DOCS,
    ROOT,
    build_context,
    default_model_note,
    get_env,
    load_config,
    read_watchlist,
    render_digest,
    report_meta,
)

DEFAULT_PRIORITIES = ["high", "medium"]

# run_id -> mutable state dict, guarded by RUNS_LOCK.
RUNS: dict[str, dict] = {}
RUNS_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------


def execute_run(run_id: str, days: int, limit: int) -> None:
    """Search -> judge -> render for every report type. Runs on a worker thread."""
    config = load_config()
    specs = config["report_types"]
    search_cfg = config.get("search", {})
    env = get_env()
    from utils.connect import DEFAULT_MODEL as model

    def log(msg: str) -> None:
        with RUNS_LOCK:
            RUNS[run_id]["log"].append(msg)

    def bump() -> None:
        with RUNS_LOCK:
            RUNS[run_id]["done"] += 1

    try:
        # Count the work up front so the progress bar is honest.
        planned = {}
        for key, spec in specs.items():
            entries = read_watchlist(ROOT / spec["watchlist"])
            planned[key] = entries[:limit] if limit else entries
        with RUNS_LOCK:
            RUNS[run_id]["total"] = sum(len(v) for v in planned.values())

        tavily = search_mod.get_client()
        from utils.connect import get_client as llm_client

        llm = llm_client()

        period = search_mod.period_label(days)
        run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        results: dict[str, dict] = {}

        for key, spec in specs.items():
            entries = planned[key]
            log(f"[{spec['label']}] {len(entries)} {spec['entity_noun_plural']}, {period}")

            judged: list[dict] = []
            kept = dropped = 0
            failed: list[str] = []

            # Search and judge one entity at a time. Interleaving keeps the progress
            # bar honest (one step per entity, start to finish) and preserves the
            # one-entity-at-a-time shape the privacy model wants.
            for name, city in entries:
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

                one, k, d, f = judge_entities(
                    [{"display_name": name, "city": city, "articles": articles}],
                    spec,
                    model=model,
                    client=llm,
                    progress=log,
                )
                judged += one
                kept += k
                dropped += d
                failed += f
                bump()

            log(f"{spec['label']}: {kept} findings kept, {dropped} excluded"
                + (f", {len(failed)} skipped" if failed else ""))

            monitored = len(entries)
            context = build_context(
                spec,
                config,
                entities=judged,
                priorities=DEFAULT_PRIORITIES,
                monitored=monitored,
                run_date=run_date,
                period_label=period,
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
                "_period": period,
                "_monitored": monitored,
            }

        with RUNS_LOCK:
            RUNS[run_id]["reports"] = results
            RUNS[run_id]["days"] = days
            RUNS[run_id]["state"] = "done"
        log("done")

    except Exception as exc:  # noqa: BLE001 - surface it to the page, don't kill the server
        traceback.print_exc()
        with RUNS_LOCK:
            RUNS[run_id]["state"] = "error"
            RUNS[run_id]["error"] = f"{type(exc).__name__}: {exc}"


def write_snapshot(run_id: str) -> list[str]:
    """Freeze a finished run into data/*.json, then rebuild docs/ from it."""
    with RUNS_LOCK:
        run = RUNS.get(run_id)
        if not run or run.get("state") != "done":
            raise ValueError("run is not finished")
        reports = run["reports"]

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

            presets = load_config().get("lookback_presets", [7, 30, 90])
            try:
                days = int(body.get("days", presets[0]))
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
                RUNS[run_id] = {"state": "running", "log": [], "done": 0, "total": 0, "reports": {}}

            threading.Thread(target=execute_run, args=(run_id, days, limit), daemon=True).start()
            return self._json({"run_id": run_id})

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
        entries = read_watchlist(ROOT / spec["watchlist"])
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
            period_label="no run yet — pick a range and click Run live",
            empty=True,
        )
        reports.append(report_meta(key, spec, context, render_digest(env, context), f"digest-{key}.html"))

    queries = max((len(s.get("query_templates") or []) for s in specs.values()), default=2)
    return env.get_template("runner.html").render(
        reports=reports,
        default_type=reports[0]["key"],
        presets=config.get("lookback_presets", [7, 30, 90]),
        entity_counts=entity_counts,
        queries_per_entity=queries,
        watchlist_paths=watchlist_paths,
        model_note=f"default model {default_model_note()}",
        has_tavily=bool(os.environ.get("TAVILY_API_KEY")),
        has_tritonai=bool(os.environ.get("TRITONAI_API_KEY")),
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
