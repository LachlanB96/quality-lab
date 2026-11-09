"""The run recorder, tested the way pytest tests its own plugins: run a tiny suite in a sandbox (pytester)."""
import json

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.area("report")]
pytest_plugins = ["pytester"]

SUITE = '''
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.area("demo")]

def test_ok(record_property):
    """Passes and reports a metric."""
    record_property("metric", {"name": "speed", "value": 3, "unit": "ms"})

def test_bad():
    assert 1 == 2

@pytest.mark.xfail(reason="BUG-042: known", strict=True)
def test_known():
    assert False

@pytest.mark.skip(reason="not today")
def test_skipped():
    pass

@pytest.fixture
def broken():
    raise RuntimeError("setup blew up")

def test_setup_error(broken):
    pass
'''


@pytest.fixture
def record(pytester):
    pytester.makeini("[pytest]\nmarkers =\n    integration: x\n")
    pytester.makepyfile(test_demo=SUITE)
    out = pytester.path / "run.json"
    pytester.runpytest("-p", "qualitylab.results", f"--ql-out={out}", "--ql-label=unit")
    return json.loads(out.read_text())


def test_record_has_every_test_with_its_outcome(record):
    outcomes = {t["id"].split("::")[1]: t["outcome"] for t in record["tests"]}
    assert outcomes == {"test_ok": "passed", "test_bad": "failed", "test_known": "xfailed",
                        "test_skipped": "skipped", "test_setup_error": "error"}
    assert record["counts"] == {"passed": 1, "failed": 1, "xfailed": 1, "skipped": 1, "error": 1}
    assert record["label"] == "unit" and record["schema"] == 1


def test_labels_come_from_markers_and_docstrings(record):
    ok = next(t for t in record["tests"] if t["id"].endswith("test_ok"))
    assert (ok["layer"], ok["area"], ok["doc"]) == ("integration", "demo", "Passes and reports a metric.")
    assert ok["metrics"] == [{"name": "speed", "value": 3, "unit": "ms"}]


def test_failures_and_known_bugs_carry_their_message(record):
    by_name = {t["id"].split("::")[1]: t for t in record["tests"]}
    assert "assert 1 == 2" in by_name["test_bad"]["message"]
    assert "BUG-042" in by_name["test_known"]["message"]
    assert "setup blew up" in by_name["test_setup_error"]["message"]


def test_times_are_relative_to_the_run_start(record):
    starts = [t["start"] for t in record["tests"] if t["start"] is not None]
    assert starts and all(0 <= s < 60 for s in starts)


def test_record_has_no_home_paths(record, tmp_path_factory):
    from pathlib import Path
    assert str(Path.home()) not in json.dumps(record)


def _refusing(times: int | None):
    """os.replace that refuses the first `times` calls (every call when None), as Windows does while a
    reader has the target open."""
    import os
    real, calls = os.replace, {"n": 0}

    def fake(src, dst):
        calls["n"] += 1
        if times is None or calls["n"] <= times:
            raise PermissionError(13, "The process cannot access the file", str(dst))
        real(src, dst)
    return fake, calls


def test_a_refused_replace_is_tried_again_until_it_lands(tmp_path, monkeypatch):
    from qualitylab import results
    tmp, target = tmp_path / "run.tmp", tmp_path / "run.json"
    target.write_text("old")
    tmp.write_text("new")
    fake, calls = _refusing(3)
    monkeypatch.setattr(results.os, "replace", fake)
    results.replace_whole(tmp, target, wait=1.0, step=0.001)
    assert target.read_text() == "new" and not tmp.exists() and calls["n"] == 4


def test_a_replace_refused_past_the_wait_raises_and_leaves_no_temp_file(tmp_path, monkeypatch):
    from qualitylab import results
    tmp, target = tmp_path / "run.tmp", tmp_path / "run.json"
    target.write_text("old")
    tmp.write_text("new")
    fake, calls = _refusing(None)
    monkeypatch.setattr(results.os, "replace", fake)
    with pytest.raises(PermissionError):
        results.replace_whole(tmp, target, wait=0.05, step=0.001)
    assert target.read_text() == "old" and not tmp.exists() and calls["n"] > 1


def test_other_errors_are_not_retried(tmp_path):
    from qualitylab import results
    with pytest.raises(FileNotFoundError):
        results.replace_whole(tmp_path / "missing.tmp", tmp_path / "run.json", wait=1.0)
