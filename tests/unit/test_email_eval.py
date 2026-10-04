"""The email-triage eval's scoring, and the dataset itself. Bad gold labels make every score meaningless,
so the data gets tests too."""
import re
from datetime import date

import pytest

from qualitylab.evals import email_triage as et

pytestmark = [pytest.mark.unit, pytest.mark.area("evals")]

CASES, RECEIVED = et.load_cases()
CATEGORIES = {"Security", "Work", "Finance", "Releases", "Marketing", "Personal"}
STATUSES = {"rejected", "interview", "screening", "offer", "acknowledged", "none"}


def as_list(x):
    return x if isinstance(x, list) else [x]


# ---------- the dataset ----------

def test_case_ids_are_unique():
    ids = [c["id"] for c in CASES]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_gold_labels_use_the_classifiers_vocabulary(case):
    g = case["gold"]
    assert set(as_list(g["category"])) <= CATEGORIES
    assert set(as_list(g["jobStatus"])) <= STATUSES
    assert g.get("urgent") in (True, False, None)
    if g.get("event"):
        date.fromisoformat(g["event"]["date"])
        assert g["event"]["time"] is None or re.fullmatch(r"\d{2}:\d{2}", g["event"]["time"])


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_evidence_is_in_the_body(case):
    assert et.evidence_end(case) > 0, "the evidence text must appear in the body, or truncation can't be judged"


def test_the_long_cases_really_are_long():
    """These exist to show the 600-character cut. If an edit shortens them, they stop testing it."""
    long_ids = {"work-invite-ats-long", "work-invite-ats-relative", "work-screen-ats-long", "work-reject-long"}
    for c in CASES:
        if c["id"] in long_ids:
            assert et.evidence_end(c) > 600, c["id"]


def test_the_inbox_is_fictional():
    """Every address is on a reserved example domain, so no real person or company is in the data."""
    for c in CASES:
        assert re.search(r"@[\w.-]*example\.(com|org|net)(\.au)?$", c["from"][1]), c["from"][1]


# ---------- scoring ----------

INTERVIEW = {"id": "x", "evidence": "", "body": "", "gold": {"category": "Work", "jobStatus": "interview", "urgent": True,
                                                             "event": {"date": "2026-10-13", "time": "11:00"}}}


def verdict(**kw):
    base = {"category": "Work", "jobStatus": "interview", "flags": ["SUPER IMPORTANT"],
            "event": {"date": "2026-10-13", "time": "11:00"}}
    return {**base, **kw}


def test_a_perfect_verdict_scores_everything():
    s = et.score_case(INTERVIEW, verdict())
    assert (s["category"], s["job_status"], s["urgent"], s["event_found"], s["event_right"], s["false_entry"]) == \
        (True, True, True, True, True, False)


def test_a_missed_date_hurts_recall_not_precision():
    s = et.score_case(INTERVIEW, verdict(event=None))
    assert (s["event_found"], s["event_right"]) == (False, None)


def test_the_wrong_time_is_a_wrong_entry():
    s = et.score_case(INTERVIEW, verdict(event={"date": "2026-10-13", "time": "10:00"}))
    assert (s["event_found"], s["event_right"]) == (False, False)


def test_an_entry_where_none_belongs_is_a_false_entry():
    case = {**INTERVIEW, "gold": {**INTERVIEW["gold"], "event": None}}
    s = et.score_case(case, verdict())
    assert (s["event_right"], s["false_entry"]) == (False, True)


def test_optional_events_are_neither_required_nor_penalised():
    case = {**INTERVIEW, "gold": {**INTERVIEW["gold"], "event_optional": True}}
    assert et.score_case(case, verdict(event=None))["event_found"] is None
    assert et.score_case(case, verdict())["event_right"] is True


def test_a_list_of_gold_values_accepts_any():
    case = {**INTERVIEW, "gold": {**INTERVIEW["gold"], "jobStatus": ["screening", "interview"]}}
    assert et.score_case(case, verdict())["job_status"] is True


def test_no_answer_scores_as_wrong_not_as_skipped():
    s = et.score_case(INTERVIEW, None)
    assert s["answered"] is False and s["category"] is False


def test_consistency_compares_repeats():
    a = {"x": verdict(), "y": verdict()}
    b = {"x": verdict(), "y": verdict(category="Personal")}
    scores = [[et.score_case(INTERVIEW, v) for v in vs.values()] for vs in (a, b)]
    assert et.aggregate(scores, [a, b])["consistency"] == 0.5
