"""The guardrails on the AI review, tested without calling a model: invented numbers and tests get struck,
and the suggestions ledger never overwrites a decision."""
import pytest

from qualitylab import analyst

pytestmark = [pytest.mark.unit, pytest.mark.area("report")]

REAL = "tests/spirescape/test_world.py::test_npcs_wander_on_their_own"
FACTS = {
    "targets": {"local": {"summary": {"passed": 194, "known": 9, "pass_rate": 1.0, "wall": 42.34}}},
    "metrics": {"tick_median_ms": 599.4},
    "inventory": {REAL: {}, "tests/unit/test_wire.py::test_varint_matches_dotnet_7bit_encoding[300-\\xac\\x02]": {}},
}


@pytest.mark.parametrize("sentence, ok", [
    ("The local run passed 194 tests.", True),
    ("All 9 known bugs are still pinned.", True),
    ("The pass rate was 100% locally.", True),               # 1.0 as a percentage
    ("It took 42.3 seconds.", True),                          # rounded from 42.34
    ("The tick sat at 599 ms.", True),                        # rounded from 599.4
    ("The local run passed 196 tests.", False),               # not in the facts
    ("Pass rate was 98.5%.", False),                          # a derived number
    (f"{REAL} is slow.", True),
    ("tests/spirescape/test_world.py::test_made_up is slow.", False),
    ("BUG-004 and RFC 6455 are cited, which isn't a numeric claim.", True),
], ids=lambda x: str(x)[:30])
def test_sentences_are_checked_against_the_facts(sentence, ok):
    numbers = analyst.allowed_numbers(FACTS)
    assert analyst.sentence_ok(sentence, numbers, FACTS["inventory"])[0] is ok


def test_check_strikes_bad_sentences_and_keeps_good_ones():
    reply = {
        "overview": {"headline": "All green, 194 passed.",
                     "paragraphs": ["194 tests passed. Exactly 777 assertions ran."],
                     "points": [{"kind": "good", "text": "9 known bugs pinned.", "tests": [REAL, "tests/x.py::nope"]}]},
        "suggestions": [
            {"title": "Speed up the walk", "why": "It takes 42.3 s.", "kind": "speed", "effort": "small", "evidence": [REAL]},
            {"title": "Invented", "why": "Because.", "kind": "speed", "effort": "small", "evidence": ["tests/x.py::nope"]},
        ],
    }
    clean, report = analyst.check(reply, FACTS)
    assert clean["overview"]["paragraphs"] == ["194 tests passed."]
    assert clean["overview"]["points"][0]["tests"] == [REAL]
    assert [s["title"] for s in clean["suggestions"]] == ["Speed up the walk"]
    assert report["failed"] == len(report["struck"]) == 3
    assert any("777" in s for s in report["struck"])


def test_a_file_path_counts_as_evidence():
    assert analyst.known_test("tests/spirescape/test_world.py", FACTS["inventory"])
    assert not analyst.known_test("tests/spirescape/test_nothing.py", FACTS["inventory"])


def test_merge_keeps_decisions_and_skips_near_duplicates():
    existing = [{"id": "S-001", "title": "Add a test for chat rate limiting", "why": "x", "kind": "coverage gap",
                 "effort": "small", "evidence": [], "status": "rejected", "note": "out of scope"}]
    new = [
        {"title": "Add a test for the chat rate limiting", "why": "y", "kind": "coverage gap", "effort": "small", "evidence": []},
        {"title": "Measure login time on every run", "why": "z", "kind": "speed", "effort": "small", "evidence": []},
    ]
    merged = analyst.merge_suggestions(existing, new, "run-1", "2026-10-04")
    assert merged[0] == existing[0]
    assert [s["id"] for s in merged] == ["S-001", "S-002"]
    assert merged[1]["status"] == "open" and merged[1]["proposed"] == "2026-10-04"
