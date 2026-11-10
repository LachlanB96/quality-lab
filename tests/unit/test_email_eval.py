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
    assert g["replyExpected"] in (True, False), "every case says whether the sender is waiting on a reply"
    if g.get("event"):
        date.fromisoformat(g["event"]["date"])
        assert g["event"]["time"] is None or re.fullmatch(r"\d{2}:\d{2}", g["event"]["time"])


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_evidence_is_in_the_body(case):
    assert et.evidence_end(case) > 0, "the evidence text must appear in the body, or truncation can't be judged"


LONG = {  # where each long case's deciding text ends: (past, up to), so each length in a sweep has its cases
    "work-screen-ats-long": (600, 1000), "work-reject-long": (600, 1000), "work-invite-ats-relative": (600, 1000),
    "work-invite-ats-long": (1000, 1500), "work-invite-teams": (1000, 1500),
    "work-panel-interview": (1500, 2000), "work-recruiter-long": (1500, 2000),
    "work-assessment-day": (2000, 3000),
    "work-final-onsite": (3000, 4000),
    "work-confirmed-zoom": (4000, None),
}


@pytest.mark.parametrize("cid", sorted(LONG))
def test_the_long_cases_put_their_deciding_text_in_their_band(cid):
    """These exist to test each cut. If an edit moves the deciding text out of its band, the sweep stops
    telling those two cuts apart."""
    lo, hi = LONG[cid]
    end = et.evidence_end(next(c for c in CASES if c["id"] == cid))
    assert end > lo and (hi is None or end <= hi), end


def test_every_band_of_a_sweep_has_a_date_to_find():
    """A cut can only beat the one below it on dates if some date ends between them."""
    dated = [et.evidence_end(c) for c in CASES
             if c["gold"].get("event") and not c["gold"].get("event_optional") and c["id"] in LONG]
    for lo, hi in ((600, 1000), (1000, 1500), (1500, 2000), (2000, 3000), (3000, 4000), (4000, None)):
        assert any(e > lo and (hi is None or e <= hi) for e in dated), (lo, hi)


TAGS = {"confirm-by-reply": True, "automated-no-reply": False}


@pytest.mark.parametrize("case", [c for c in CASES if c.get("tags")], ids=lambda c: c["id"])
def test_a_tag_agrees_with_the_reply_gold(case):
    """confirm-by-reply cases expect a reply and automated-no-reply ones don't: the tag's score means that."""
    for tag in case["tags"]:
        assert tag in TAGS, f"unknown tag {tag}"
        assert case["gold"]["replyExpected"] is TAGS[tag]


def test_each_reply_group_has_several_cases():
    """One case per group can't show that a prompt line steadies the answer."""
    for tag in TAGS:
        assert sum(tag in c.get("tags", []) for c in CASES) >= 3, tag


def test_the_inbox_is_fictional():
    """Every address is on a reserved example domain, so no real person or company is in the data."""
    for c in CASES:
        assert re.search(r"@[\w.-]*example\.(com|org|net)(\.au)?$", c["from"][1]), c["from"][1]


# ---------- scoring ----------

INTERVIEW = {"id": "x", "evidence": "", "body": "", "gold": {"category": "Work", "jobStatus": "interview", "urgent": True,
                                                             "replyExpected": True,
                                                             "event": {"date": "2026-10-13", "time": "11:00"}}}


def verdict(**kw):
    base = {"category": "Work", "jobStatus": "interview", "flags": ["SUPER IMPORTANT"], "replyExpected": True,
            "event": {"date": "2026-10-13", "time": "11:00"}}
    return {**base, **kw}


def test_a_perfect_verdict_scores_everything():
    s = et.score_case(INTERVIEW, verdict())
    assert (s["category"], s["job_status"], s["urgent"], s["reply"], s["event_found"], s["event_right"],
            s["false_entry"]) == (True, True, True, True, True, True, False)


def test_the_wrong_reply_answer_is_a_miss():
    s = et.score_case(INTERVIEW, verdict(replyExpected=False))
    assert s["reply"] is False
    assert et.misses([INTERVIEW], [s], {"x": verdict(replyExpected=False)})[0]["fields"] == ["reply"]


def test_a_reply_the_model_left_open_counts_as_wrong():
    """llm_verdicts gives None when the model said neither yes nor no. That's a miss, not a skip."""
    assert et.score_case(INTERVIEW, verdict(replyExpected=None))["reply"] is False
    assert et.score_case(INTERVIEW, {k: v for k, v in verdict().items() if k != "replyExpected"})["reply"] is False


def test_a_case_without_a_reply_gold_is_not_scored_on_it():
    case = {**INTERVIEW, "gold": {k: v for k, v in INTERVIEW["gold"].items() if k != "replyExpected"}}
    assert et.score_case(case, verdict(replyExpected=False))["reply"] is None


def test_the_reply_rate_is_over_the_cases_that_have_one():
    a = {"x": verdict(), "y": verdict(replyExpected=False)}
    scores = [[et.score_case(INTERVIEW, v) for v in a.values()]]
    assert et.aggregate(scores, [a])["reply"] == 0.5


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


def test_an_optional_entry_with_no_gold_date_counts_neither_way():
    """fin-overdue: 'pay today' may get an entry, and there's no date to hold it to, so it isn't a wrong one."""
    case = {**INTERVIEW, "gold": {**INTERVIEW["gold"], "event": None, "event_optional": True}}
    s = et.score_case(case, verdict())
    assert (s["event_found"], s["event_right"], s["false_entry"]) == (None, None, False)


def test_an_optional_entry_on_the_wrong_date_is_still_wrong():
    case = {**INTERVIEW, "gold": {**INTERVIEW["gold"], "event_optional": True}}
    assert et.score_case(case, verdict(event={"date": "2026-10-14", "time": "11:00"}))["event_right"] is False


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


def test_a_reply_answer_that_changes_between_repeats_is_inconsistent():
    a = {"x": verdict()}
    b = {"x": verdict(replyExpected=False)}
    scores = [[et.score_case(INTERVIEW, v) for v in vs.values()] for vs in (a, b)]
    assert et.aggregate(scores, [a, b])["consistency"] == 0.0
    assert et.aggregate(scores, [a, b])["reply_steady"] == 0.0


# ---------- cuts ----------

def test_cuts_are_numbers_or_full():
    assert et.parse_chars(["600", "1500", "full"]) == [600, 1500, None]
    for bad in ("0", "-5", "600c", "all"):
        with pytest.raises(ValueError):
            et.parse_chars([bad])


def test_the_full_body_is_never_cut():
    body = "x" * 10_000
    assert len(body[:et.body_limit(None)]) == 10_000
    assert et.variant_name(None) == "full" and et.variant_name(600) == "600"


def test_the_summary_lists_the_cuts_in_plain_words():
    s = et.summary_text(50, 5, [600, 1000, 4000, None])
    assert ("each classified 5 times at each cut (the first 600, 1,000 and 4,000 characters and the whole body)"
            in s)
    assert s.endswith("Before: the first 600 characters of each body, as the dashboard sends today. "
                      "After: the whole body.")
    assert et.summary_text(50, 8, [600]).endswith("(the first 600 characters) by Claude Haiku through the "
                                                  "dashboard's own classify.llm_verdicts. Before: the first 600 "
                                                  "characters of each body, as the dashboard sends today.")


def test_chars_sent_is_what_the_cut_lets_through():
    cases = [{"body": "a" * 100}, {"body": "b" * 1000}]
    assert et.chars_sent(cases, 600) == 350.0
    assert et.chars_sent(cases, None) == 550.0


# ---------- reply groups ----------

def tagged(cid, tag, reply):
    return {"id": cid, "tags": [tag], "gold": {**INTERVIEW["gold"], "replyExpected": reply}}


def test_tag_scores_rate_the_reply_on_each_group_and_how_steady_it_is():
    cases = [tagged("a", "confirm-by-reply", True), tagged("b", "confirm-by-reply", True),
             tagged("c", "automated-no-reply", False)]
    runs = [{"a": verdict(replyExpected=True), "b": verdict(replyExpected=False), "c": verdict(replyExpected=False)},
            {"a": verdict(replyExpected=True), "b": verdict(replyExpected=True), "c": verdict(replyExpected=False)}]
    scores = [[et.score_case(c, vs[c["id"]]) for c in cases] for vs in runs]
    got = et.tag_scores(cases, scores, runs)
    assert got["confirm-by-reply"] == {"cases": 2, "reply": 0.75, "reply_steady": 0.5}
    assert got["automated-no-reply"] == {"cases": 1, "reply": 1.0, "reply_steady": 1.0}


# ---------- calibration ----------

def dated(cid, date="2026-10-13", time="11:00"):
    return {"id": cid, "gold": {**INTERVIEW["gold"], "event": {"date": date, "time": time}}}


def entry(certainty, date="2026-10-13", time="11:00"):
    return verdict(event={"date": date, "time": time, "certainty": certainty})


def test_calibration_puts_each_entry_in_its_band_with_how_often_it_was_right():
    cases = [dated("a"), dated("b"), dated("c"), dated("d")]
    runs = [{"a": entry(95), "b": entry(95), "c": entry(70), "d": entry(70, time="12:00")}]
    cal = et.calibration(cases, runs)
    assert cal["entries"] == 4 and cal["cases"] == 4 and cal["unrated"] == 0
    assert cal["bands"] == [{"band": "70-79", "entries": 2, "certainty": 70.0, "right": 0.5},
                            {"band": "95-100", "entries": 2, "certainty": 95.0, "right": 1.0}]
    # Brier: (0.05^2 * 2 + 0.3^2 + 0.7^2) / 4; ECE: half the entries 0.2 off, half 0.05 off
    assert cal["brier"] == round((0.0025 * 2 + 0.09 + 0.49) / 4, 4)
    assert cal["ece"] == round(0.5 * 0.2 + 0.5 * 0.05, 4)


def test_an_entry_where_none_belongs_counts_as_wrong_and_a_missed_date_not_at_all():
    no_event = {"id": "n", "gold": {**INTERVIEW["gold"], "event": None}}
    cases = [no_event, dated("m")]
    cal = et.calibration(cases, [{"n": entry(90), "m": verdict(event=None)}])
    assert cal["entries"] == 1 and cal["right"] == 0.0 and cal["bands"][0]["band"] == "90-94"


def test_an_entry_without_a_certainty_is_counted_apart():
    cal = et.calibration([dated("a")], [{"a": verdict()}])
    assert (cal["entries"], cal["unrated"], cal["brier"]) == (0, 1, None)


def test_per_case_gives_each_case_its_rates_and_mean_certainty():
    cases = [dated("a"), {"id": "n", "gold": {**INTERVIEW["gold"], "event": None, "replyExpected": None}}]
    runs = [{"a": entry(90), "n": verdict(event=None)}, {"a": entry(70, time="12:00"), "n": verdict(event=None)}]
    scores = [[et.score_case(c, vs[c["id"]]) for c in cases] for vs in runs]
    got = et.per_case(cases, scores, runs)
    assert got["a"] == {"event_found": 0.5, "event_right": 0.5, "reply": 1.0, "certainty": 80.0}
    assert "n" not in got, "a case with nothing scored and no entries has no row"


def test_an_unscored_optional_entry_is_left_out():
    case = {"id": "o", "gold": {**INTERVIEW["gold"], "event": None, "event_optional": True}}
    assert et.calibration([case], [{"o": entry(80)}])["entries"] == 0


# ---------- token meter ----------

class FakeSubprocess:
    TimeoutExpired = TimeoutError

    def __init__(self, stdout):
        self.stdout, self.cmds = stdout, []

    def run(self, cmd, **kwargs):
        import subprocess
        self.cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=self.stdout, stderr="")


def test_the_meter_asks_for_json_keeps_the_usage_and_hands_back_the_reply_text():
    import json
    reply = json.dumps({"result": '[{"i":0}]', "total_cost_usd": 0.002,
                        "usage": {"input_tokens": 10, "cache_creation_input_tokens": 1000,
                                  "cache_read_input_tokens": 20000, "output_tokens": 300}})
    real = FakeSubprocess(reply)
    meter = et.Meter(real)
    proc = meter.run(["claude", "-p", "prompt"], capture_output=True)
    assert proc.stdout == '[{"i":0}]'
    assert real.cmds == [["claude", "-p", "prompt", "--output-format", "json"]]
    assert meter.TimeoutExpired is TimeoutError, "the rest of the module is the real one"
    use = et.token_use(meter.take(), mails=12)
    assert use == {"calls": 1, "unmetered": 0, "input_per_mail": round(21010 / 12, 1), "input_per_call": 21010,
                   "cache_read_share": round(20000 / 21010, 3), "output_per_mail": 25.0, "usd_per_1000_mails": 0.167}
    assert meter.take() == [], "take empties the meter for the next variant"


def test_output_that_isnt_json_passes_through_and_is_counted_unmetered():
    meter = et.Meter(FakeSubprocess("claude crashed"))
    assert meter.run(["claude"]).stdout == "claude crashed"
    assert et.token_use(meter.take(), mails=12) == {"calls": 1, "unmetered": 1, "input_per_mail": None,
                                                    "input_per_call": None, "cache_read_share": None,
                                                    "output_per_mail": None, "usd_per_1000_mails": None}
