"""Pulling CI's run records: merge copies only the live records history lacks, fetch skips runs with no artifact."""
import json
import subprocess
from pathlib import Path

import pytest

from qualitylab import pull

pytestmark = [pytest.mark.unit, pytest.mark.area("report")]


def record(path: Path, target: str = "live", tests=()) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": 1, "target": target, "tests": list(tests)}), encoding="utf-8")


def test_merge_copies_only_the_live_records_history_lacks(tmp_path):
    runs = tmp_path / "history" / "runs"
    record(runs / "20261004T060846Z-live.json")
    one, two = tmp_path / "dl" / "1", tmp_path / "dl" / "2"
    record(one / "history/runs/20261004T060846Z-live.json")                      # every artifact carries the old one
    record(one / "history/runs/20261105T083430Z-live.json", tests=[{"id": "x"}])
    record(two / "history/runs/20261106T083355Z-live.json")
    record(two / "history/runs/20261106T083355Z-local.json", target="local")     # never in the artifact, and not live
    (two / "history/runs/notes-live.json").write_text("not a record", encoding="utf-8")
    record(two / "history/runs/20261107T000000Z-live.json", target="local")       # a live name on a local record

    added = pull.merge(runs, [one, two])

    assert added == ["20261105T083430Z-live.json", "20261106T083355Z-live.json"]
    assert sorted(p.name for p in runs.iterdir()) == ["20261004T060846Z-live.json", *added]
    assert json.loads((runs / "20261105T083430Z-live.json").read_text(encoding="utf-8"))["tests"] == [{"id": "x"}]


def test_merge_never_overwrites_a_record_history_has(tmp_path):
    runs = tmp_path / "runs"
    record(runs / "20261105T083430Z-live.json", tests=[{"id": "kept"}])
    record(tmp_path / "dl" / "history/runs/20261105T083430Z-live.json", tests=[{"id": "incoming"}])
    assert pull.merge(runs, [tmp_path / "dl"]) == []
    assert json.loads((runs / "20261105T083430Z-live.json").read_text(encoding="utf-8"))["tests"] == [{"id": "kept"}]


def test_fetch_filters_by_date_and_skips_runs_without_the_artifact(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[2] == "list":
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps([
                {"databaseId": 3, "createdAt": "2026-11-08T08:33:15Z"},
                {"databaseId": 2, "createdAt": "2026-11-07T08:33:16Z"},
                {"databaseId": 1, "createdAt": "2026-10-04T06:00:00Z"},
            ]), stderr="")
        assert cmd[2] == "download"
        dest = Path(cmd[cmd.index("-D") + 1])
        if cmd[3] == "2":
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no artifact matches live-run")
        record(dest / "history/runs" / f"2026110{cmd[3]}T083000Z-live.json")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(pull.subprocess, "run", fake_run)
    folders = list(pull.fetch("owner/repo", limit=10, since="2026-11-01"))
    assert [f.name for f in folders] == ["3"], "run 2 had no artifact and run 1 is before the since date"
    downloads = [c for c in calls if c[2] == "download"]
    assert [c[3] for c in downloads] == ["3", "2"]
    assert all("-n" in c and c[c.index("-n") + 1] == "live-run" for c in downloads)
    listing = calls[0]
    assert listing[:4] == ["gh", "run", "list", "-R"] and "Tests" in listing and "10" in listing
