"""Reading run records back: summaries, trends, flaky tests, coverage. Everything the report shows is here,
as plain functions over the JSON that results.py writes, so it can be unit tested without running pytest.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

BUG_ID = re.compile(r"\bBUG-\d{3}\b")

# How each pytest outcome counts. A known bug (xfail) is neither a pass nor a failure: it's tracked.
PASS, FAIL, KNOWN, SKIP, FIXED = "pass", "fail", "known", "skip", "fixed"
_CLASS = {"passed": PASS, "failed": FAIL, "error": FAIL, "xfailed": KNOWN, "skipped": SKIP, "xpassed": FIXED}


def outcome_class(outcome: str) -> str:
    return _CLASS.get(outcome, FAIL)


def load_runs(folder: Path) -> list[dict]:
    """Every run record in a folder, oldest first. Unreadable files are skipped, not fatal."""
    runs = []
    for f in sorted(Path(folder).glob("*.json")):
        try:
            run = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if run.get("schema") == 1 and run.get("tests"):
            runs.append(run)
    return sorted(runs, key=lambda r: r["started"])


def summarize(run: dict) -> dict:
    classes = Counter(outcome_class(t["outcome"]) for t in run["tests"])
    decided = classes[PASS] + classes[FAIL]
    return {
        "id": run["id"],
        "target": run["target"],
        "started": run["started"],
        "wall": run["wall"],
        "total": len(run["tests"]),
        "passed": classes[PASS],
        "failed": classes[FAIL],
        "known": classes[KNOWN],
        "skipped": classes[SKIP],
        "fixed": classes[FIXED],
        "pass_rate": round(classes[PASS] / decided, 4) if decided else None,
        "green": classes[FAIL] == 0 and classes[FIXED] == 0,
    }


def by_layer(run: dict) -> dict[str, Counter]:
    out: dict[str, Counter] = defaultdict(Counter)
    for t in run["tests"]:
        out[t.get("layer", "unlabelled")][outcome_class(t["outcome"])] += 1
    return dict(out)


def matrix(run: dict) -> dict[str, dict[str, int]]:
    """area -> layer -> number of tests that ran (skips excluded)."""
    out: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for t in run["tests"]:
        if outcome_class(t["outcome"]) == SKIP:
            continue
        out[t.get("area") or "other"][t.get("layer", "unlabelled")] += 1
    return {a: dict(layers) for a, layers in out.items()}


def known_bugs(run: dict) -> dict[str, dict]:
    """BUG id -> the tests that pin it and whether any of them now pass (an XPASS means fixed)."""
    bugs: dict[str, dict] = {}
    for t in run["tests"]:
        cls = outcome_class(t["outcome"])
        if cls not in (KNOWN, FIXED):
            continue
        for bug in BUG_ID.findall(t.get("message", "")):
            b = bugs.setdefault(bug, {"tests": [], "fixed": False})
            b["tests"].append(t["id"])
            b["fixed"] = b["fixed"] or cls == FIXED
    return dict(sorted(bugs.items()))


def flaky(runs: list[dict], window: int = 20) -> list[dict]:
    """Tests that both passed and failed in the last `window` runs of the same target, worst first.

    A flip is a change between pass and fail from one run to the next. Known bugs and skips don't count."""
    by_target: dict[str, list[dict]] = defaultdict(list)
    for r in runs:
        by_target[r["target"]].append(r)
    out = []
    for target, rs in by_target.items():
        rs = rs[-window:]
        history: dict[str, list[str]] = defaultdict(list)
        for r in rs:
            for t in r["tests"]:
                cls = outcome_class(t["outcome"])
                if cls in (PASS, FAIL):
                    history[t["id"]].append(cls)
        for test_id, seq in history.items():
            if PASS in seq and FAIL in seq:
                flips = sum(1 for a, b in zip(seq, seq[1:]) if a != b)
                out.append({"id": test_id, "target": target, "runs": len(seq), "fails": seq.count(FAIL),
                            "flips": flips, "sequence": "".join("P" if s == PASS else "F" for s in seq)})
    return sorted(out, key=lambda x: (-x["flips"], -x["fails"], x["id"]))


def slowest(run: dict, n: int = 10) -> list[dict]:
    ran = [t for t in run["tests"] if outcome_class(t["outcome"]) != SKIP]
    return [{"id": t["id"], "duration": t["duration"], "layer": t.get("layer", "")}
            for t in sorted(ran, key=lambda t: -t["duration"])[:n]]


def metric_series(runs: list[dict]) -> dict[str, list[dict]]:
    """metric name -> [{run, target, started, value, unit}] in run order."""
    out: dict[str, list[dict]] = defaultdict(list)
    for r in runs:
        for t in r["tests"]:
            for mtr in t.get("metrics", []):
                out[mtr["name"]].append({"run": r["id"], "target": r["target"], "started": r["started"],
                                         "value": mtr["value"], "unit": mtr.get("unit", "")})
    return dict(out)


def protocol_coverage(run: dict, all_types: list[str]) -> dict:
    sent = run.get("protocol", {}).get("sent", {})
    received = run.get("protocol", {}).get("received", {})
    rows = [{"type": name, "sent": sent.get(name, 0), "received": received.get(name, 0)} for name in all_types]
    covered = sum(1 for row in rows if row["sent"] or row["received"])
    return {"covered": covered, "total": len(rows), "types": rows}


def latest(runs: list[dict], target: str) -> dict | None:
    for r in reversed(runs):
        if r["target"] == target:
            return r
    return None
