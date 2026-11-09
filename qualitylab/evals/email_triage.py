"""Evals for the email dashboard's AI classifier, run through its real code path.

The classifier (`classify.llm_verdicts` in the private Comms repo, located by
EMAIL_DASHBOARD_DIR) sends batches of emails to Claude Haiku and gets back a category, a job status,
an importance, whether the sender expects a reply and maybe a calendar event. Here it classifies a
labelled, made-up inbox (evals/email_triage/cases.json) under named variants, several times each, and
the answers are scored against the gold labels.

Variants change one thing: how many characters of each body the model sees (classify.BODY_CHARS), a
list of cuts with "full" for the whole body. Production sends 600. ATS emails often put the interview
date further in than that, and the long cases put it at every length up to past 4,000.

Every miss is then sorted into one of three kinds:
  - product issue: the code around the model got in the way (the deciding text was cut off), or the
    instructions were clear and the model still got it wrong
  - prompt gap: the instructions never told the model how to handle this case
  - rubric gap: the gold label itself is wrong or arguable
Truncation is detected by code. The rest are sorted by a Claude Sonnet judge, one sentence each.

Each variant also records what its calls cost in tokens (a longer cut is paid on every mail), how well
the certainty Haiku gives each date matches how often those dates are right, and the reply score on
each tagged group of cases. An experiment (a classifier on a branch, say) is recorded beside the page's
history rather than in it, so the page keeps showing the production classifier.
"""
from __future__ import annotations

import concurrent.futures as cf
import importlib
import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
CASES = ROOT / "evals" / "email_triage" / "cases.json"
EVALS = ROOT / "history" / "evals"
EXPERIMENTS = EVALS / "experiments"  # build.py reads only the newest record directly in EVALS
PRODUCTION = 600  # classify.BODY_CHARS on Comms' main
MODEL, BATCH = "haiku", 12  # Comms' config.json: classify.model, classify.batchSize
BANDS = ((0, 60), (60, 70), (70, 80), (80, 90), (90, 95), (95, 101))  # certainty bands, upper bound excluded

METRICS = [
    {"key": "category", "label": "Category right"},
    {"key": "job_status", "label": "Job status right"},
    {"key": "event_recall", "label": "Dates caught"},
    {"key": "event_precision", "label": "Calendar entries right"},
    {"key": "urgent", "label": "Urgent flag right"},
    {"key": "reply", "label": "Reply expected right"},
    {"key": "consistency", "label": "Same answer every repeat"},
]


# ------------------------------------------------------------------------------------------------
# dataset
# ------------------------------------------------------------------------------------------------

def load_cases(path: Path = CASES) -> tuple[list[dict], str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["cases"], data["received"]


def parse_chars(values: list[str]) -> list[int | None]:
    """Cuts from the command line: a number of characters, or "full" (None) for the whole body."""
    out: list[int | None] = []
    for v in values:
        if v == "full":
            out.append(None)
        elif v.isdigit() and int(v) > 0:
            out.append(int(v))
        else:
            raise ValueError(f"a cut is a number of characters or 'full', not {v!r}")
    return out


def variant_name(chars: int | None) -> str:
    return "full" if chars is None else str(chars)


def body_limit(chars: int | None) -> int:
    """What classify.BODY_CHARS is set to: it slices each body with it, so the whole body is any number past the end."""
    return sys.maxsize if chars is None else chars


def normalise(body: str) -> str:
    """What classify.llm_verdicts does to a body before cutting it."""
    return re.sub(r"\s+", " ", body)


def chars_sent(cases: list[dict], chars: int | None) -> float:
    """Characters of body the model sees per mail, on average, at this cut."""
    return round(sum(min(len(normalise(c["body"])), body_limit(chars)) for c in cases) / len(cases), 1)


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
        # replyExpected is True, False, or None when the model said neither, which counts as wrong
        "reply": None if g.get("replyExpected") is None else accepts(g["replyExpected"], v.get("replyExpected")),
        # recall: a required event was found with the right date (and time, when the gold has one)
        "event_found": None if (gold_ev is None or optional) else bool(pred_ev and event_matches(gold_ev, pred_ev)),
        # precision: an event the model produced is a right one. An optional entry with no gold date to check
        # it against is allowed, so it isn't scored either way
        "event_right": None if not pred_ev or (gold_ev is None and optional)
        else bool(gold_ev and event_matches(gold_ev, pred_ev)),
        "false_entry": bool(pred_ev and gold_ev is None and not optional),
        "answered": verdict is not None,
    }
    return out


def rate(values: list) -> float | None:
    vals = [x for x in values if x is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def reply_steady(ids: list[str], verdict_sets: list[dict]) -> float | None:
    """Share of these cases whose reply answer was the same in every repeat (None with a single repeat)."""
    if len(verdict_sets) < 2 or not ids:
        return None
    return rate([len({(vs.get(cid) or {}).get("replyExpected") for vs in verdict_sets}) == 1 for cid in ids])


def aggregate(case_scores: list[list[dict]], verdict_sets: list[dict], cases: list[dict] | None = None) -> dict:
    """case_scores[repeat][case]; verdict_sets[repeat] = {case_id: verdict}."""
    flat = [s for rep in case_scores for s in rep]
    ids = list(verdict_sets[0]) if verdict_sets else []
    same = []
    for cid in ids:
        sig = {json.dumps([vs.get(cid, {}).get(k) for k in ("category", "jobStatus", "flags", "replyExpected")]
                          + [event_key(vs.get(cid, {}).get("event"))]) for vs in verdict_sets}
        same.append(len(sig) == 1)
    replied = [c["id"] for c in cases if c["gold"].get("replyExpected") is not None] if cases is not None else ids
    return {
        "reply_steady": reply_steady(replied, verdict_sets),
        "category": rate([s["category"] for s in flat]),
        "job_status": rate([s["job_status"] for s in flat]),
        "urgent": rate([s["urgent"] for s in flat]),
        "reply": rate([s["reply"] for s in flat]),
        "event_recall": rate([s["event_found"] for s in flat]),
        "event_precision": rate([s["event_right"] for s in flat]),
        "false_entries": sum(s["false_entry"] for s in flat),
        "unanswered": sum(not s["answered"] for s in flat),
        "consistency": rate(same) if len(verdict_sets) > 1 else None,
    }


def tag_scores(cases: list[dict], case_scores: list[list[dict]], verdict_sets: list[dict]) -> dict:
    """The reply score and its steadiness on each tagged group of cases (cases.json's tags)."""
    out = {}
    for tag in sorted({t for c in cases for t in c.get("tags", [])}):
        idx = [i for i, c in enumerate(cases) if tag in c.get("tags", [])]
        out[tag] = {"cases": len(idx), "reply": rate([rep[i]["reply"] for rep in case_scores for i in idx]),
                    "reply_steady": reply_steady([cases[i]["id"] for i in idx], verdict_sets)}
    return out


def per_case(cases: list[dict], case_scores: list[list[dict]]) -> dict:
    """How often each case was right on dates and on the reply, across the repeats: where a rate comes from."""
    out = {}
    for i, c in enumerate(cases):
        row = {k: rate([rep[i][f] for rep in case_scores]) for k, f in (("event_found", "event_found"),
                                                                         ("event_right", "event_right"),
                                                                         ("reply", "reply"))}
        row = {k: v for k, v in row.items() if v is not None}
        if row:
            out[c["id"]] = row
    return out


def calibration(cases: list[dict], verdict_sets: list[dict]) -> dict:
    """Is the certainty Haiku gives each date as often right as it says?

    Every calendar entry the model made that can be scored (as event_right is: a date on a case with a gold
    date, or an entry on a case that should have none) is a point: its certainty, and whether it was right.
    A missed date has no certainty, so it isn't here (that's recall). Gives a reliability table by certainty
    band, the Brier score (mean squared gap between certainty and outcome: 0 is perfect, 0.25 is what saying
    50 every time scores on a coin flip) and the expected calibration error (the gap between certainty and
    how often right, averaged over the bands, weighted by their size)."""
    points, unrated, seen = [], 0, set()
    for vs in verdict_sets:
        for c in cases:
            v = vs.get(c["id"])
            s = score_case(c, v)
            if s["event_right"] is None:
                continue
            cert = ((v or {}).get("event") or {}).get("certainty")
            if cert is None:
                unrated += 1
                continue
            points.append((float(cert), bool(s["event_right"])))
            seen.add(c["id"])
    out: dict = {"entries": len(points), "unrated": unrated, "cases": len(seen), "bands": []}
    if not points:
        return {**out, "brier": None, "ece": None, "certainty": None, "right": None}
    n = len(points)
    ece = 0.0
    for lo, hi in BANDS:
        inside = [(c, y) for c, y in points if lo <= c < hi]
        if not inside:
            continue
        cert = sum(c for c, _ in inside) / len(inside)
        right = sum(y for _, y in inside) / len(inside)
        ece += len(inside) / n * abs(right - cert / 100)
        out["bands"].append({"band": f"{lo}-{min(hi, 101) - 1}", "entries": len(inside),
                             "certainty": round(cert, 1), "right": round(right, 4)})
    return {**out, "brier": round(sum((c / 100 - y) ** 2 for c, y in points) / n, 4), "ece": round(ece, 4),
            "certainty": round(sum(c for c, _ in points) / n, 1), "right": round(sum(y for _, y in points) / n, 4)}


def misses(cases: list[dict], scores: list[dict], verdicts: dict) -> list[dict]:
    out = []
    for case, s in zip(cases, scores):
        wrong = [k for k in ("category", "job_status", "urgent", "reply", "event_found", "event_right")
                 if s[k] is False]
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
        raise RuntimeError("Set EMAIL_DASHBOARD_DIR to the Comms folder (it holds classify.py).")
    if raw not in sys.path:
        sys.path.insert(0, raw)
    return importlib.import_module("classify")


class Meter:
    """Stands in for the subprocess module inside classify, to keep each `claude -p` call's token use.

    classify reads the model's reply as plain text, which carries no usage. While metering, run() adds
    --output-format json to the command, keeps the usage from the JSON, and hands classify the reply's text
    as stdout, so classify parses exactly what it would have. The prompt and the model are untouched. Output
    that isn't JSON (claude crashed) is passed on as it came and counted as an unmetered call."""

    def __init__(self, real=subprocess) -> None:
        self.real = real
        self.calls: list[dict] = []
        self.lock = threading.Lock()

    def __getattr__(self, name: str):  # everything but run() is the real module (TimeoutExpired and the rest)
        return getattr(self.real, name)

    def run(self, cmd, *args, **kwargs):
        proc = self.real.run([*cmd, "--output-format", "json"], *args, **kwargs)
        try:
            data = json.loads(proc.stdout or "")
        except json.JSONDecodeError:
            data = None
        if not isinstance(data, dict):
            with self.lock:
                self.calls.append({"metered": False})
            return proc
        u = data.get("usage") or {}
        with self.lock:
            self.calls.append({"metered": True, "fresh": int(u.get("input_tokens") or 0),
                               "cache_write": int(u.get("cache_creation_input_tokens") or 0),
                               "cache_read": int(u.get("cache_read_input_tokens") or 0),
                               "output": int(u.get("output_tokens") or 0),
                               "cost_usd": float(data.get("total_cost_usd") or 0.0)})
        proc.stdout = data.get("result") or ""
        return proc

    def take(self) -> list[dict]:
        with self.lock:
            calls, self.calls = self.calls, []
        return calls


def token_use(calls: list[dict], mails: int) -> dict:
    """Per mail classified: input tokens (fresh, cache writes and cache reads together: everything the model
    read), output tokens, and the list-price cost `claude -p` reports for them."""
    metered = [c for c in calls if c["metered"]]
    per = lambda total: round(total / mails, 1) if mails and metered else None
    return {"calls": len(calls), "unmetered": len(calls) - len(metered),
            "input_per_mail": per(sum(c["fresh"] + c["cache_write"] + c["cache_read"] for c in metered)),
            "input_per_call": round(sum(c["fresh"] + c["cache_write"] + c["cache_read"] for c in metered)
                                    / len(metered)) if metered else None,
            "cache_read_share": round(sum(c["cache_read"] for c in metered)
                                      / max(1, sum(c["fresh"] + c["cache_write"] + c["cache_read"] for c in metered)), 3)
            if metered else None,
            "output_per_mail": per(sum(c["output"] for c in metered)),
            "usd_per_1000_mails": round(sum(c["cost_usd"] for c in metered) / mails * 1000, 3) if mails and metered else None}


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

def classifier_commit() -> str:
    """The classifier's commit, so a record says which classify.py it measured."""
    out = subprocess.run(["git", "-C", os.environ["EMAIL_DASHBOARD_DIR"], "rev-parse", "--short", "HEAD"],
                         capture_output=True, text=True, timeout=10)
    if out.returncode != 0:
        raise RuntimeError(f"can't read the classifier's commit: {out.stderr.strip()}")
    return out.stdout.strip()


def cut_words(chars: int | None) -> str:
    return "the whole body" if chars is None else f"the first {chars:,} characters"


def describe(r: dict) -> str:
    s, t, cal = r["scores"], r["tokens"], r["calibration"]
    return (f"{r['name']:>5}: dates caught {s['event_recall']}, entries right {s['event_precision']}, "
            f"false entries {s['false_entries']}, reply {s['reply']} (steady {s['reply_steady']}), "
            f"input/mail {t['input_per_mail']}, brier {cal['brier']}")


def main(repeats: int = 2, chars: list[int | None] | None = None, experiment: str = "") -> int:
    chars = chars or [PRODUCTION, None]
    if experiment and not re.fullmatch(r"[a-z0-9][a-z0-9-]*", experiment):
        raise ValueError(f"an experiment's name is lowercase words joined by hyphens, not {experiment!r}")
    classify = load_classifier()
    commit = classifier_commit()
    cases, received = load_cases()
    cases_by_id = {c["id"]: c for c in cases}
    meter = Meter(classify.subprocess)
    classify.subprocess = meter
    results = []
    try:
        for cut in chars:
            limit = body_limit(cut)
            verdict_sets = [classify_all(classify, cases, received, limit) for _ in range(repeats)]
            calls = meter.take()
            scores = [[score_case(c, vs.get(c["id"])) for c in cases] for vs in verdict_sets]
            first_misses = misses(cases, scores[0], verdict_sets[0])
            results.append({"name": variant_name(cut), "body_chars": cut, "chars_sent": chars_sent(cases, cut),
                            "scores": aggregate(scores, verdict_sets, cases),
                            "tags": tag_scores(cases, scores, verdict_sets),
                            "tokens": token_use(calls, len(cases) * repeats),
                            "calibration": calibration(cases, verdict_sets),
                            "per_case": per_case(cases, scores),
                            "misses": triage(classify, cases_by_id, first_misses, limit)})
            print(describe(results[-1]), flush=True)
    finally:
        classify.subprocess = meter.real
    base, cand = results[0], results[-1]
    groups: dict[str, list[str]] = {}
    for m in base["misses"] + ([] if cand is base else cand["misses"]):
        tag = "before" if m in base["misses"] else "after"
        groups.setdefault(m["kind"], []).append(f"{m['id']} ({tag}): {m['reason']}")
    now = datetime.now(timezone.utc)
    cuts = ", ".join("the whole body" if r["body_chars"] is None else f"{r['body_chars']:,}" for r in results)
    record = {
        "generated": now.isoformat(timespec="seconds"),
        "title": (f"Email triage experiment: {experiment}" if experiment
                  else "Email triage: how much of each body to send"),
        "summary": (f"{len(cases)} made-up emails, each classified {repeats} times at each cut ({cuts} characters) "
                    f"by Claude Haiku through the dashboard's own classify.llm_verdicts. Before: "
                    f"{cut_words(base['body_chars'])} of each body"
                    + (", as the dashboard sends today" if base["body_chars"] == PRODUCTION else "")
                    + f". After: {cut_words(cand['body_chars'])}."),
        "legend": (f"Calendar entries that shouldn't exist: {base['scores']['false_entries']} before, "
                   f"{cand['scores']['false_entries']} after (across all repeats). Rates count only the cases where "
                   "a field applies: dates caught is over emails that need a calendar entry."),
        "experiment": experiment or None, "classifier": {"commit": commit},
        "baseline": base["name"], "candidate": cand["name"], "model": MODEL, "repeats": repeats,
        "cases": len(cases), "metrics": METRICS,
        "variants": [{k: r[k] for k in ("name", "body_chars", "chars_sent", "scores", "tags", "tokens", "calibration",
                                         "per_case")} for r in results],
        "triage": [{"kind": k, "text": t} for k in ("product issue", "prompt gap", "rubric gap", "unsorted")
                   for t in groups.get(k, [])],
        "misses": {r["name"]: r["misses"] for r in results},
    }
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    out = EXPERIMENTS / f"{stamp}-email-{experiment}.json" if experiment else EVALS / f"{stamp}-email.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"recorded {out.relative_to(ROOT).as_posix()}")
    return 0
