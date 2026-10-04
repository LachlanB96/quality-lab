"""The AI review: Claude reads the latest runs and the test code, then writes an overview and suggestions.

What comes back is not trusted. Before anything reaches the page:
  - every test id it cites must exist in the run records, or the citation is dropped
  - every number in a sentence must appear in the facts it was given, or the sentence is struck
  - suggestions merge into suggestions.json without touching decisions already made
The checks are deterministic and unit tested (tests/unit/test_analyst.py), so the guardrails are
tested even though the model isn't.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import history as h
from .claude import ClaudeError, ask

ROOT = Path(__file__).resolve().parent.parent
TEST_DIRS = ("tests/spirescape", "tests/unit")

SCHEMA = {
    "type": "object",
    "required": ["overview", "suggestions"],
    "properties": {
        "overview": {
            "type": "object",
            "required": ["headline", "paragraphs", "points"],
            "properties": {
                "headline": {"type": "string"},
                "paragraphs": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
                "points": {"type": "array", "maxItems": 6, "items": {
                    "type": "object", "required": ["kind", "text"],
                    "properties": {"kind": {"enum": ["good", "risk", "bad"]}, "text": {"type": "string"},
                                   "tests": {"type": "array", "items": {"type": "string"}}}}},
            },
        },
        "suggestions": {"type": "array", "maxItems": 6, "items": {
            "type": "object", "required": ["title", "why", "kind", "effort", "evidence"],
            "properties": {
                "title": {"type": "string"}, "why": {"type": "string"},
                "kind": {"enum": ["coverage gap", "weak assertion", "flakiness", "speed", "maintainability", "bug follow-up"]},
                "effort": {"enum": ["small", "medium", "large"]},
                "evidence": {"type": "array", "items": {"type": "string"}},
            }}},
    },
}

INSTRUCTION = """You are reviewing a pytest suite and its latest runs for the report page of a QA portfolio.
stdin holds FACTS (JSON: run summaries, known bugs, slowest tests, coverage, metrics, the test inventory),
then the full source of every test file.

Write:
1. overview: a one-line headline, at most 3 short paragraphs, and up to 6 points (good / risk / bad) on how
   the suite and the system under test are doing. Be specific and plain. No hype, no em dashes.
2. suggestions: up to 6 concrete ways to make the SUITE better (not the game): coverage gaps, weak
   assertions, flakiness risks, speed, maintainability, or a test a known bug still needs. Each needs a
   reason grounded in the facts or the code, a kind, an effort, and evidence: the exact test ids (as in
   the inventory, e.g. "tests/spirescape/test_world.py::test_npcs_wander_on_their_own") or file paths
   that show the problem.

Hard rules, because your output is checked by a script before it is published:
- Only quote numbers that appear in FACTS. Don't compute new ones (no percentages or sums you derived).
- Round durations to whole seconds, or one decimal place under ten seconds.
- When you give a count and then name the items, the count must match the list exactly.
- Tests run in parallel on several workers, so per-test durations don't add up to the run's wall time.
- Only cite test ids that appear in the inventory, spelled exactly.
- Don't repeat a suggestion listed in FACTS.existing_suggestions."""


# ------------------------------------------------------------------------------------------------
# facts: what the model is allowed to know, and the reference its claims are checked against
# ------------------------------------------------------------------------------------------------

def build_facts(runs: list[dict], existing: list[dict]) -> dict:
    latest = {t: h.latest(runs, t) for t in ("local", "live")}
    facts: dict = {"targets": {}, "existing_suggestions": [s["title"] for s in existing]}
    inventory: dict[str, dict] = {}
    for tgt, run in latest.items():
        if not run:
            continue
        s = h.summarize(run)
        facts["targets"][tgt] = {
            "summary": s, "tick_ms": run.get("tick_ms"), "workers": run.get("workers"),
            "by_layer": {k: dict(v) for k, v in h.by_layer(run).items()},
            "known_bugs": {k: v["tests"] for k, v in h.known_bugs(run).items()},
            "failures": [{"id": t["id"], "message": t.get("message", "")[:400]} for t in run["tests"]
                         if h.outcome_class(t["outcome"]) in (h.FAIL, h.FIXED)],
            "slowest": h.slowest(run, 8),
            "metrics": {mt["name"]: mt["value"] for t in run["tests"] for mt in t.get("metrics", [])},
            "protocol_covered": h.protocol_coverage(run, list(run.get("protocol", {}).get("sent", {}))
                                                    + list(run.get("protocol", {}).get("received", {})))["covered"],
        }
        for t in run["tests"]:
            inventory.setdefault(t["id"], {"layer": t.get("layer"), "area": t.get("area"), "doc": t.get("doc", "")})
    facts["flaky"] = h.flaky(runs)
    facts["runs_recorded"] = {"local": sum(r["target"] == "local" for r in runs), "live": sum(r["target"] == "live" for r in runs)}
    facts["inventory"] = inventory
    return facts


def allowed_numbers(facts: dict) -> set[str]:
    """Every number that appears anywhere in the facts, in the forms a writer would round it to."""
    found: set[str] = set()
    text = json.dumps({k: v for k, v in facts.items() if k != "inventory"})
    for raw in re.findall(r"-?\d+(?:\.\d+)?", text):
        found |= number_forms(float(raw))
    found |= {str(n) for n in range(0, 11)}  # small counting words ("two workers", "3 paragraphs") are safe
    for tid in facts.get("inventory", {}):     # numbers inside test names and parameters
        found |= {m for m in re.findall(r"\d+", tid)}
    return found


def number_forms(x: float) -> set[str]:
    forms = {f"{x:g}", f"{round(x)}", f"{x:.1f}", f"{x:.2f}"}
    if 0 <= x <= 1:  # rates are stored as fractions and written as percentages
        forms |= {f"{x * 100:g}", f"{round(x * 100)}", f"{x * 100:.1f}"}
    return {f.rstrip("0").rstrip(".") if "." in f else f for f in forms}


# ------------------------------------------------------------------------------------------------
# checks
# ------------------------------------------------------------------------------------------------

ID_LIKE = re.compile(r"tests/[\w/]+\.py(?:::[\w\[\]\-.,@]+)?")


def known_test(ref: str, inventory: dict) -> bool:
    if ref in inventory:
        return True
    if "::" not in ref and ref.endswith(".py"):
        return any(i.startswith(ref + "::") for i in inventory)
    return False


def sentence_ok(sentence: str, numbers: set[str], inventory: dict) -> tuple[bool, str]:
    for ref in ID_LIKE.findall(sentence):
        ref = ref.rstrip(".,;:")
        if not known_test(ref, inventory):
            return False, f"cites a test that doesn't exist: {ref}"
    stripped = ID_LIKE.sub("", sentence)
    stripped = re.sub(r"\bBUG-\d{3}\b|\b[A-Z]-\d{3}\b|\bRFC \d+\b|\bD-\d{3}\b|\bxdist\b|\bgw\d+\b", "", stripped)
    for raw in re.findall(r"\d+(?:\.\d+)?", stripped):
        if raw.rstrip("0").rstrip(".") not in numbers and raw not in numbers:
            return False, f"quotes {raw}, which isn't in the run records"
    return True, ""


def split_sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", text.strip()) if s]


def check(reply: dict, facts: dict) -> tuple[dict, dict]:
    """Return (cleaned reply, check report). Bad sentences and citations are removed, and listed."""
    numbers, inventory = allowed_numbers(facts), facts["inventory"]
    total = passed = 0
    struck: list[str] = []

    def keep(text: str) -> str:
        nonlocal total, passed
        out = []
        for s in split_sentences(text):
            total += 1
            ok, why = sentence_ok(s, numbers, inventory)
            if ok:
                passed += 1
                out.append(s)
            else:
                struck.append(f"{s} ({why})")
        return " ".join(out)

    ov = reply.get("overview", {})
    clean_ov = {"headline": keep(ov.get("headline", "")),
                "paragraphs": [p for p in (keep(x) for x in ov.get("paragraphs", [])) if p],
                "points": []}
    for pt in ov.get("points", []):
        text = keep(pt.get("text", ""))
        refs = pt.get("tests", [])
        good_refs = [r for r in refs if known_test(r, inventory)]
        total += len(refs)
        passed += len(good_refs)
        struck += [f"citation {r} (no such test)" for r in refs if r not in good_refs]
        if text:
            clean_ov["points"].append({"kind": pt.get("kind", "good"), "text": text, "tests": good_refs})

    clean_sugs = []
    for sg in reply.get("suggestions", []):
        title, why = keep(sg.get("title", "")), keep(sg.get("why", ""))
        ev = sg.get("evidence", [])
        good_ev = [e for e in ev if known_test(e, inventory)]
        total += len(ev)
        passed += len(good_ev)
        struck += [f"evidence {e} (no such test or file)" for e in ev if e not in good_ev]
        if title and why and (good_ev or not ev):
            clean_sugs.append({**sg, "title": title, "why": why, "evidence": good_ev})
    report = {"total": total, "passed": passed, "failed": total - passed, "struck": struck}
    return {"overview": clean_ov, "suggestions": clean_sugs}, report


# ------------------------------------------------------------------------------------------------
# suggestions ledger
# ------------------------------------------------------------------------------------------------

def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", s.lower()) if len(w) > 3}


def merge_suggestions(existing: list[dict], new: list[dict], source: str, today: str) -> list[dict]:
    """Add new suggestions as 'open'. Never changes an existing one; skips near-duplicates by title."""
    out = [dict(s) for s in existing]
    n = max([int(s["id"].split("-")[1]) for s in out if re.fullmatch(r"S-\d+", s.get("id", ""))] + [0])
    for sg in new:
        w = _words(sg["title"])
        if any(len(w & _words(s["title"])) / max(1, len(w | _words(s["title"]))) > 0.5 for s in out):
            continue
        n += 1
        out.append({"id": f"S-{n:03d}", "status": "open", "proposed": today, "source": source,
                    **{k: sg[k] for k in ("title", "why", "kind", "effort", "evidence")}})
    return out


# ------------------------------------------------------------------------------------------------

def test_sources(root: Path = ROOT) -> str:
    parts = []
    for d in TEST_DIRS:
        for f in sorted((root / d).glob("test_*.py")):
            parts.append(f"===== {f.relative_to(root).as_posix()} =====\n{f.read_text(encoding='utf-8')}")
    return "\n\n".join(parts)


def main(model: str = "sonnet", root: Path = ROOT) -> int:
    runs = h.load_runs(root / "history" / "runs")
    if not runs:
        print("no runs recorded yet")
        return 1
    ledger_path = root / "suggestions.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.exists() else {"suggestions": []}
    facts = build_facts(runs, ledger["suggestions"])
    context = "FACTS\n" + json.dumps(facts, indent=1) + "\n\nTEST SOURCES\n" + test_sources(root)
    try:
        reply = ask(INSTRUCTION, context, SCHEMA, model=model)
    except ClaudeError as e:
        print(f"analysis skipped: {e}")
        return 1
    clean, report = check(reply.data, facts)
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    used = [r["id"] for r in (h.latest(runs, "local"), h.latest(runs, "live")) if r]
    record = {"generated": now.isoformat(timespec="seconds"), "model": reply.model, "cost_usd": reply.cost_usd,
              "seconds": reply.seconds, "runs": used, "overview": clean["overview"], "checks": report,
              "raw": reply.data}
    out = root / "history" / "analysis" / f"{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=1, ensure_ascii=False), encoding="utf-8")
    ledger["suggestions"] = merge_suggestions(ledger["suggestions"], clean["suggestions"], stamp, now.strftime("%Y-%m-%d"))
    ledger_path.write_text(json.dumps(ledger, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"analysis by {reply.model} in {reply.seconds}s (${reply.cost_usd:.3f}): "
          f"{report['passed']}/{report['total']} claims checked out, {len(report['struck'])} struck")
    return 0
