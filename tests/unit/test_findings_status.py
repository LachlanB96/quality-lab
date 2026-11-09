"""A finding with a `metrics` list is intermittent: its status is whether the latest live run saw it, read from the
metrics tests recorded (a retried connect, the ten-connect probe), never "fixed" on the strength of one quiet run."""
import json

import pytest

from qualitylab.report import build

pytestmark = [pytest.mark.unit, pytest.mark.area("report")]

PROBE = "tests/spirescape/test_session.py::test_every_upgrade_is_answered"
CHAT = "tests/spirescape/test_multiplayer.py::test_chat_reaches_everyone_in_the_region"
LOGIN = "tests/spirescape/test_performance.py::test_login_time"


def rec(test_id: str, outcome: str = "passed", message: str = "", metrics: list | None = None) -> dict:
    t = {"id": test_id, "outcome": outcome, "duration": 1.0, "call": 1.0, "start": 1.0, "stop": 2.0, "worker": "gw0",
         "message": message, "layer": "integration", "area": "session"}
    if metrics:
        t["metrics"] = metrics
    return t


def lab(root, tests: list[dict]) -> dict[str, dict]:
    """A lab folder with one live run and two findings: BUG-008 (intermittent) and BUG-001 (pinned by xfail)."""
    (root / "history" / "runs").mkdir(parents=True)
    run = {"schema": 1, "id": "20261109T083000Z-abc123", "label": "ci", "started": "2026-11-09T08:30:00+00:00",
           "wall": 100.0, "target": "live", "tick_ms": 600, "seeded": False, "workers": 4, "exitstatus": 0,
           "counts": {}, "env": {}, "protocol": {"sent": {}, "received": {}}, "tests": tests}
    (root / "history" / "runs" / "20261109T083000Z-live.json").write_text(json.dumps(run), encoding="utf-8")
    blank = {"title": "t", "severity": "high", "how": "", "impact": "", "repro": [], "fix": ""}
    (root / "findings.json").write_text(json.dumps({"note": "", "findings": [
        {"id": "BUG-008", "area": "session", "found": "2026-11-09",
         "metrics": ["handshake_stalls", "handshake_stalls_of_10"], **blank},
        {"id": "BUG-001", "area": "tutorial", "found": "2026-10-04", **blank},
    ]}), encoding="utf-8")
    return {f["id"]: f for f in build.collect(root)["findings"]}


def stalls(n: int, of_ten: bool = False) -> list[dict]:
    return [{"name": "handshake_stalls_of_10" if of_ten else "handshake_stalls", "value": n, "unit": "stalls"}]


def test_seen_through_a_retried_connect_and_the_probe(tmp_path):
    f = lab(tmp_path, [
        rec(CHAT, metrics=stalls(1)),                                       # the fixture retried once
        rec(PROBE, "xfailed", "BUG-008: 3 of 10 never answered", stalls(3, of_ten=True)),
        rec(LOGIN, metrics=[{"name": "login_ms", "value": 900, "unit": "ms"}]),  # another metric: not counted
    ])["BUG-008"]
    assert (f["status"], f["seen"]) == ("open", 4)
    assert f["tests"] == sorted([CHAT, PROBE])


def test_unseen_when_the_latest_live_run_recorded_no_stall(tmp_path):
    found = lab(tmp_path, [rec(PROBE, metrics=stalls(0, of_ten=True)), rec(CHAT)])
    assert (found["BUG-008"]["status"], found["BUG-008"]["seen"], found["BUG-008"]["tests"]) == ("unseen", 0, [])
    assert found["BUG-001"]["status"] == "untested", "a pinned finding keeps the xfail rule"
    assert "seen" not in found["BUG-001"]


def test_a_pinned_xfail_with_the_id_counts_as_seen_too(tmp_path):
    f = lab(tmp_path, [rec(PROBE, "xfailed", "BUG-008: 1 of 10 never answered")])["BUG-008"]
    assert (f["status"], f["seen"], f["tests"]) == ("open", 0, [PROBE])
