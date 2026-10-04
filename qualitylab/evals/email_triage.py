"""Evals for the email dashboard's AI classifier, run through its real code path.

The classifier (`classify.llm_verdicts` in the private Email Dashboard repo, located by
EMAIL_DASHBOARD_DIR) sends batches of emails to Claude Haiku and gets back a category, a job status,
an importance and maybe a calendar event. Here it classifies a labelled, made-up inbox
(evals/email_triage/cases.json) under named variants, several times each, and the answers are
scored against the gold labels.

Variants change one thing: how many characters of each body the model sees (classify.BODY_CHARS).
Production sends 600. ATS emails often put the interview date further in than that.

Every miss is then sorted into one of three kinds:
  - product issue: the code around the model got in the way (the deciding text was cut off), or the
    instructions were clear and the model still got it wrong
  - prompt gap: the instructions never told the model how to handle this case
  - rubric gap: the gold label itself is wrong or arguable
Truncation is detected by code. The rest are sorted by a Claude Sonnet judge, one sentence each.
"""
from __future__ import annotations

import concurrent.futures as cf
import importlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
CASES = ROOT / "evals" / "email_triage" / "cases.json"
VARIANTS = {"production": 600, "full-body": 4000}
MODEL, BATCH = "haiku", 12  # Email Dashboard config.json: classify.model, classify.batchSize

METRICS = [
    {"key": "category", "label": "Category right"},
    {"key": "job_status", "label": "Job status right"},
    {"key": "event_recall", "label": "Dates caught"},
    {"key": "event_precision", "label": "Calendar entries right"},
    {"key": "urgent", "label": "Urgent flag right"},
    {"key": "consistency", "label": "Same answer every repeat"},
]


# ------------------------------------------------------------------------------------------------
# dataset
# ------------------------------------------------------------------------------------------------

def load_cases(path: Path = CASES) -> tuple[list[dict], str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["cases"], data["received"]


def normalise(body: str) -> str:
    """What classify.llm_verdicts does to a body before cutting it."""
    return re.sub(r"\s+", " ", body)


def evidence_end(case: dict) -> int:
    """Character position where the deciding text ends, in the body the model is sent."""
    body = normalise(case["body"])
    i = body.find(normalise(case["evidence"]))
    return -1 if i < 0 else i + len(normalise(case["evidence"]))


def as_message(case: dict, received: str) -> dict:
    name, email = case["from"]
    return {"id": case["id"], "date": received, "from": {"name": name, "email": email}, "subject": case["subject"],
            "bodyText": case["body"], "bulk": case["bulk"]}


def accepts(gold, value) -> bool:
    return value in gold if isinstance(gold, list) else value == gold


# ------------------------------------------------------------------------------------------------
# scoring
# ------------------------------------------------------------------------------------------------

def event_key(ev) -> tuple | None:
    return None if not ev else (ev.get("date"), ev.get("time"))


def event_matches(gold_ev: dict, pred_ev: dict) -> bool:
    if pred_ev.get("date") != gold_ev["date"]:
        return False
    return gold_ev.get("time") is None or pred_ev.get("time") == gold_ev["time"]


def score_case(case: dict, verdict: dict | None) -> dict:
    """Which fields this verdict got right. None means the case wasn't scored on that field."""
    g = case["gold"]
    v = verdict or {}
    pred_ev = v.get("event")
    gold_ev = g.get("event")
    optional = g.get("event_optional", False)
    out = {
        "category": accepts(g["category"], v.get("category")),
        "job_status": accepts(g["jobStatus"], v.get("jobStatus", "none")),
        "urgent": None if g.get("urgent") is None else (("SUPER IMPORTANT" in (v.get("flags") or [])) == g["urgent"]),
        # recall: a required event was found with the right date (and time, when the gold has one)
        "event_found": None if (gold_ev is None or optional) else bool(pred_ev and event_matches(gold_ev, pred_ev)),
        # precision: an event the model produced is a right one
        "event_right": None if not pred_ev else bool(gold_ev and event_matches(gold_ev, pred_ev)),
        "false_entry": bool(pred_ev and gold_ev is None and not optional),
        "answered": verdict is not None,
    }
    return out


def rate(values: list) -> float | None:
    vals = [x for x in values if x is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def aggregate(case_scores: list[list[dict]], verdict_sets: list[dict]) -> dict:
    """case_scores[repeat][case]; verdict_sets[repeat] = {case_id: verdict}."""
    flat = [s for rep in case_scores for s in rep]
    ids = list(verdict_sets[0]) if verdict_sets else []
    same = []
    for cid in ids:
        sig = {json.dumps([vs.get(cid, {}).get(k) for k in ("category", "jobStatus", "flags")]
                          + [event_key(vs.get(cid, {}).get("event"))]) for vs in verdict_sets}
        same.append(len(sig) == 1)
    return {
        "category": rate([s["category"] for s in flat]),
        "job_status": rate([s["job_status"] for s in flat]),
        "urgent": rate([s["urgent"] for s in flat]),
        "event_recall": rate([s["event_found"] for s in flat]),
        "event_precision": rate([s["event_right"] for s in flat]),
        "false_entries": sum(s["false_entry"] for s in flat),
        "unanswered": sum(not s["answered"] for s in flat),
        "consistency": rate(same) if len(verdict_sets) > 1 else None,
    }


def misses(cases: list[dict], scores: list[dict], verdicts: dict) -> list[dict]:
    out = []
    for case, s in zip(cases, scores):
        wrong = [k for k in ("category", "job_status", "urgent", "event_found", "event_right") if s[k] is False]
        if s["false_entry"]:
            wrong.append("false_entry")
        if not s["answered"]:
            wrong.append("no_answer")
        if wrong:
            out.append({"id": case["id"], "fields": wrong, "gold": case["gold"], "got": verdicts.get(case["id"])})
    return out


# ------------------------------------------------------------------------------------------------
# running the real classifier
# ------------------------------------------------------------------------------------------------

def load_classifier():
    raw = os.environ.get("EMAIL_DASHBOARD_DIR", "")
    if not raw or not (Path(raw) / "classify.py").exists():
        raise RuntimeError("Set EMAIL_DASHBOARD_DIR to the Email Dashboard folder (it holds classify.py).")
    if raw not in sys.path:
        sys.path.insert(0, raw)
    return importlib.import_module("classify")


def classify_all(classify, cases: list[dict], received: str, body_chars: int, workers: int = 4) -> dict:
    """{case id: verdict or None}. Batches of 12 in parallel, exactly as production batches them."""
    classify.BODY_CHARS = body_chars
    msgs = [as_message(c, received) for c in cases]
    batches = [msgs[i:i + BATCH] for i in range(0, len(msgs), BATCH)]
    out: dict = {}
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        for batch, got in zip(batches, pool.map(lambda b: classify.llm_verdicts(b, MODEL, 180), batches)):
            for i, m in enumerate(batch):
                out[m["id"]] = got.get(i)
    return out


# ------------------------------------------------------------------------------------------------
# triage
# ------------------------------------------------------------------------------------------------

JUDGE_SCHEMA = {"type": "object", "required": ["verdicts"], "properties": {"verdicts": {"type": "array", "items": {
    "type": "object", "required": ["id", "kind", "reason"],
    "properties": {"id": {"type": "string"}, "kind": {"enum": ["prompt gap", "rubric gap", "product issue"]},
                   "reason": {"type": "string"}}}}}}

JUDGE_INSTRUCTION = """You are triaging misses from an eval of an email classifier. stdin has the classifier's
full instructions, then each missed case: the email, the gold label, what the classifier returned, and
which fields were wrong. For each miss, choose one kind:
- "prompt gap": the instructions don't say how to handle this case, so the model had to guess.
- "rubric gap": the gold label is wrong or arguable under the instructions as written.
- "product issue": the instructions cover it clearly and the classifier still got it wrong.
Give one plain sentence of reason that names the specific thing (no em dashes). Be strict: only call it a
rubric gap if a careful reader of the instructions could reasonably give the classifier's answer."""


def triage(classify, cases_by_id: dict, found: list[dict], body_chars: int, judge_model: str = "sonnet") -> list[dict]:
    sorted_out, for_judge = [], []
    for miss in found:
        case = cases_by_id[miss["id"]]
        end = evidence_end(case)
        if end > body_chars:
            sorted_out.append({**miss, "kind": "product issue", "by": "code",
                               "reason": f"The deciding text ends at character {end}, but the classifier only sends "
                                         f"the first {body_chars}, so the model never saw it."})
        else:
            for_judge.append(miss)
    if for_judge:
        from ..claude import ClaudeError, ask
        context = "CLASSIFIER INSTRUCTIONS\n" + classify.INSTRUCTIONS + "\n\nMISSES\n" + json.dumps([
            {"id": m["id"], "subject": cases_by_id[m["id"]]["subject"], "from": cases_by_id[m["id"]]["from"],
             "bulk": cases_by_id[m["id"]]["bulk"], "body": cases_by_id[m["id"]]["body"],
             "received": "2026-10-02", "gold": m["gold"], "got": m["got"], "wrong_fields": m["fields"],
             "note": cases_by_id[m["id"]].get("note", "")} for m in for_judge], indent=1)
        try:
            reply = ask(JUDGE_INSTRUCTION, context, JUDGE_SCHEMA, model=judge_model)
            by_id = {v["id"]: v for v in reply.data["verdicts"]}
        except ClaudeError as e:
            by_id = {}
            print(f"judge skipped: {e}")
        for m in for_judge:
            v = by_id.get(m["id"])
            sorted_out.append({**m, "kind": v["kind"] if v else "unsorted", "by": "judge" if v else "",
                               "reason": v["reason"] if v else ""})
    return sorted_out


# ------------------------------------------------------------------------------------------------

def main(repeats: int = 2, variants: list[str] | None = None) -> int:
    variants = variants or list(VARIANTS)
    classify = load_classifier()
    cases, received = load_cases()
    cases_by_id = {c["id"]: c for c in cases}
    results = []
    for name in variants:
        chars = VARIANTS[name]
        verdict_sets = [classify_all(classify, cases, received, chars) for _ in range(repeats)]
        scores = [[score_case(c, vs.get(c["id"])) for c in cases] for vs in verdict_sets]
        first_misses = misses(cases, scores[0], verdict_sets[0])
        results.append({"name": name, "body_chars": chars, "scores": aggregate(scores, verdict_sets),
                        "misses": triage(classify, cases_by_id, first_misses, chars), "verdicts": verdict_sets[0]})
        print(f"{name} ({chars} chars): {results[-1]['scores']}")
    base, cand = results[0], results[-1]
    groups: dict[str, list[str]] = {}
    for m in base["misses"] + ([] if cand is base else cand["misses"]):
        tag = "before" if m in base["misses"] else "after"
        groups.setdefault(m["kind"], []).append(f"{m['id']} ({tag}): {m['reason']}")
    now = datetime.now(timezone.utc)
    record = {
        "generated": now.isoformat(timespec="seconds"),
        "title": "Email triage: production vs. full body",
        "summary": (f"{len(cases)} made-up emails, each classified {repeats} times per variant by Claude Haiku "
                    f"through the dashboard's own classify.llm_verdicts. Before: the first {base['body_chars']:,} "
                    f"characters of each body, as the dashboard sends today. After: the first "
                    f"{cand['body_chars']:,} characters."),
        "legend": (f"Calendar entries that shouldn't exist: {base['scores']['false_entries']} before, "
                   f"{cand['scores']['false_entries']} after (across all repeats). Rates count only the cases where "
                   "a field applies: dates caught is over emails that need a calendar entry."),
        "baseline": base["name"], "candidate": cand["name"], "model": MODEL, "repeats": repeats,
        "cases": len(cases), "metrics": METRICS,
        "variants": [{k: r[k] for k in ("name", "body_chars", "scores")} for r in results],
        "triage": [{"kind": k, "text": t} for k in ("product issue", "prompt gap", "rubric gap", "unsorted")
                   for t in groups.get(k, [])],
        "misses": {r["name"]: r["misses"] for r in results},
    }
    out = ROOT / "history" / "evals" / f"{now.strftime('%Y%m%dT%H%M%SZ')}-email.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"recorded {out.relative_to(ROOT)}")
    return 0
