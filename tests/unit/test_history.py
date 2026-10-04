"""The report's numbers, checked against small hand-made run records."""
import json

import pytest

from qualitylab import history as h

pytestmark = [pytest.mark.unit, pytest.mark.area("report")]


def run(target="local", started="2026-10-04T10:00:00+00:00", tests=(), **extra):
    return {"schema": 1, "id": f"{target}-{started}", "target": target, "started": started, "wall": 10.0,
            "tests": [dict(t) for t in tests], **extra}


def t(test_id, outcome="passed", **kw):
    return {"id": test_id, "outcome": outcome, "duration": kw.pop("duration", 0.1), "message": "", **kw}


@pytest.mark.parametrize("outcome, cls", [
    ("passed", "pass"), ("failed", "fail"), ("error", "fail"),
    ("xfailed", "known"), ("xpassed", "fixed"), ("skipped", "skip"), ("weird", "fail"),
])
def test_outcome_classes(outcome, cls):
    """An unrecognised outcome counts as a failure, never as a pass."""
    assert h.outcome_class(outcome) == cls


def test_pass_rate_ignores_known_bugs_and_skips():
    r = run(tests=[t("a"), t("b"), t("c", "failed"), t("d", "xfailed"), t("e", "skipped")])
    s = h.summarize(r)
    assert (s["passed"], s["failed"], s["known"], s["skipped"]) == (2, 1, 1, 1)
    assert s["pass_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert s["green"] is False


def test_an_xpass_is_not_green():
    """A strict xfail that passes means a bug was fixed and its marker must come off: the run isn't green."""
    assert h.summarize(run(tests=[t("a"), t("b", "xpassed")]))["green"] is False


def test_known_bugs_group_tests_by_id():
    r = run(tests=[
        t("x1", "xfailed", message="BUG-001: soft-lock"),
        t("x2", "xfailed", message="BUG-001: soft-lock"),
        t("x3", "xpassed", message="BUG-003: typo"),
        t("ok", "passed", message="mentions BUG-009 but passed"),
    ])
    bugs = h.known_bugs(r)
    assert list(bugs) == ["BUG-001", "BUG-003"]
    assert bugs["BUG-001"] == {"tests": ["x1", "x2"], "fixed": False}
    assert bugs["BUG-003"]["fixed"] is True


def test_flaky_finds_tests_that_flip_within_a_target():
    runs = [
        run(started="2026-10-01T00:00:00+00:00", tests=[t("steady"), t("flaky")]),
        run(started="2026-10-02T00:00:00+00:00", tests=[t("steady"), t("flaky", "failed")]),
        run(started="2026-10-03T00:00:00+00:00", tests=[t("steady"), t("flaky")]),
        run("live", started="2026-10-03T00:00:00+00:00", tests=[t("steady", "failed")]),
    ]
    found = h.flaky(runs)
    assert [(f["id"], f["target"], f["sequence"], f["flips"]) for f in found] == [("flaky", "local", "PFP", 2)]


def test_flaky_window_forgets_old_runs():
    old = [run(started=f"2026-09-{d:02d}T00:00:00+00:00", tests=[t("x", "failed" if d == 1 else "passed")])
           for d in range(1, 6)]
    assert h.flaky(old, window=3) == []


def test_matrix_counts_areas_by_layer_without_skips():
    r = run(tests=[t("a", area="combat", layer="integration"), t("b", area="combat", layer="integration"),
                   t("c", area="combat", layer="e2e"), t("d", "skipped", area="ui", layer="ui")])
    assert h.matrix(r) == {"combat": {"integration": 2, "e2e": 1}}


def test_slowest_skips_skipped_tests():
    r = run(tests=[t("fast", duration=0.1), t("slow", duration=9.0), t("skipped", "skipped", duration=99.0)])
    assert [x["id"] for x in h.slowest(r, 2)] == ["slow", "fast"]


def test_metric_series_follows_run_order():
    runs = [run(started=f"2026-10-0{d}T00:00:00+00:00",
                tests=[t("perf", metrics=[{"name": "rtt", "value": d * 10, "unit": "ms"}])]) for d in (1, 2)]
    assert [p["value"] for p in h.metric_series(runs)["rtt"]] == [10, 20]


def test_protocol_coverage_counts_types_seen_either_way():
    r = run(tests=[t("a")], protocol={"sent": {"Hello": 3}, "received": {"Welcome": 3}})
    cov = h.protocol_coverage(r, ["Hello", "Welcome", "Chat"])
    assert (cov["covered"], cov["total"]) == (2, 3)


def test_load_runs_sorts_and_skips_junk(tmp_path):
    (tmp_path / "b.json").write_text(json.dumps(run(started="2026-10-02T00:00:00+00:00", tests=[t("x")])))
    (tmp_path / "a.json").write_text(json.dumps(run(started="2026-10-03T00:00:00+00:00", tests=[t("x")])))
    (tmp_path / "junk.json").write_text("{not json")
    (tmp_path / "empty.json").write_text(json.dumps(run(tests=[])))
    assert [r["started"][:10] for r in h.load_runs(tmp_path)] == ["2026-10-02", "2026-10-03"]
