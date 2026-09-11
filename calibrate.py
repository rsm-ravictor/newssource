"""Find out which model to judge with by trying them, not by being told.

    python calibrate.py            # probe every model this key can reach
    python calibrate.py --show     # print the stored result and do nothing

WHY THIS EXISTS
    Model ids are not a stable interface here. A TritonAI key carries a team, the
    team decides which models it may use, and a reissued key can change that set
    overnight - which is what turned a working pipeline into 689 identical 403s.
    A hand-maintained preference list survives that only until the next surprise.

    So the pipeline measures instead. Each candidate judges one small fixture
    entity under the real rubric, and is scored on the three things this job
    actually needs: did it return a valid envelope, how many output tokens did it
    spend, and how long did it take. The winner is cached against a fingerprint of
    the model set, so the probe re-runs when - and only when - the key's access
    changes.

WHY FIXTURES AND NOT REAL ARTICLES
    data/mock_articles_*.json are offline and fictional, so calibrating costs no
    Tavily credits and reveals nothing real to a model being evaluated. It is a
    test of whether a model can hold the contract, not of whether it judges well;
    no benchmark here can tell you the second thing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import model_choice  # noqa: E402
from judge import judge_entities  # noqa: E402
from render import ROOT, load_config  # noqa: E402
from sources import citable_limit, source_tiers  # noqa: E402
from utils.connect import get_client  # noqa: E402

CACHE = ROOT / "data" / "model_calibration.json"

# A model that cannot hold the contract is unusable however fast it is, so
# validity is absolute and the rest only breaks ties. Tokens before seconds:
# tokens are the bill, seconds are patience.
def score(row: dict) -> tuple:
    return (0 if row.get("valid") else 1, row.get("out_tokens") or 10**9, row.get("secs") or 10**9)


def fingerprint(ids: list[str]) -> str:
    """Identifies the key's access set. A new key with the same access reuses the
    calibration; the same key with new access re-probes."""
    return hashlib.sha256("|".join(sorted(ids)).encode()).hexdigest()[:16]


def load() -> dict:
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(data: dict) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def sample_entity(config: dict, report_type: str = "competitors") -> tuple[dict, dict]:
    """One fixture entity, with enough articles to be a real test of the envelope."""
    spec = config["report_types"][report_type]
    path = ROOT / f"data/mock_articles_{report_type}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    entries = raw if isinstance(raw, list) else raw.get("companies") or raw.get("entities") or []
    articles = [a for e in entries for a in e.get("articles", [])]
    entity = {
        "display_name": "Calibration Entity", "city": "San Diego", "rank": None,
        "rank_of": None, "segment": "Office", "articles": articles,
    }
    return entity, spec


def probe(client, model: str, entity: dict, spec: dict, config: dict) -> dict:
    """Judge one entity with one model and report what it cost."""
    started = time.time()
    row = {"model": model, "valid": False, "out_tokens": None, "secs": None, "note": ""}
    try:
        judged, kept, dropped, failed = judge_entities(
            [entity], spec, model=model, client=client, progress=lambda m: None,
            notes="", standard=config.get("evidence_standard", ""),
            tiers=source_tiers(config), citable_max=citable_limit(config),
            window_start=date(2020, 1, 1), attempts=1,
        )
        row["valid"] = not failed
        row["note"] = "skipped the entity" if failed else f"{kept} kept, {dropped} excluded"
    except Exception as exc:  # noqa: BLE001 - a model that raises is simply not a candidate
        row["note"] = f"{type(exc).__name__}: {str(exc)[:80]}"
    row["secs"] = round(time.time() - started, 1)
    return row


def calibrate(client=None, *, note=print) -> dict:
    """Probe every usable model the key can reach and store the ranking."""
    client = client or get_client()
    config = load_config()
    ids = [m for m in model_choice.available(client) if model_choice.usable(m)]
    if not ids:
        note("no candidate models - the proxy would not list any")
        return {}

    entity, spec = sample_entity(config)
    note(f"calibrating {len(ids)} models on {len(entity['articles'])} fixture articles")
    rows = []
    for model in ids:
        row = probe(client, model, entity, spec, config)
        rows.append(row)
        mark = "ok  " if row["valid"] else "FAIL"
        note(f"  {mark} {model:<26} {row['secs']:>6}s  {row['note']}")

    rows.sort(key=score)
    data = {
        "fingerprint": fingerprint(ids),
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "models": rows,
        "best": next((r["model"] for r in rows if r["valid"]), None),
    }
    save(data)
    note(f"\nbest available: {data['best'] or 'none of them held the contract'}")
    return data


def best_for(client, *, max_age_days: int = 30) -> str | None:
    """The cached winner, when it still applies to this key's access set.

    Returns None when there is no calibration, when the key's access has changed
    since, or when the result is old enough that the proxy has probably moved on.
    """
    data = load()
    if not data.get("best"):
        return None
    ids = [m for m in model_choice.available(client) if model_choice.usable(m)]
    if not ids or data.get("fingerprint") != fingerprint(ids):
        return None
    if data["best"] not in ids:
        return None
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(data["checked_at"])
        if age.days > max_age_days:
            return None
    except (KeyError, ValueError):
        return None
    return data["best"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--show", action="store_true", help="print the stored result, probe nothing")
    args = ap.parse_args()

    if args.show:
        data = load()
        if not data:
            print("no calibration stored yet - run `python calibrate.py`")
            return 1
        print(f"checked   {data.get('checked_at')}")
        print(f"best      {data.get('best')}")
        for row in data.get("models", []):
            mark = "ok  " if row.get("valid") else "FAIL"
            print(f"  {mark} {row['model']:<26} {row.get('secs')}s  {row.get('note')}")
        return 0

    return 0 if calibrate() else 1


if __name__ == "__main__":
    raise SystemExit(main())
