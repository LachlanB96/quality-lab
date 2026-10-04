"""A pytest plugin that records a run as one JSON file: every test's outcome, timing, worker and labels.

The page replays runs from these files, and the analytics read their history. Enabled with
`--ql-out <file>`; without it the plugin does nothing, so ad-hoc runs never touch history.

Works under pytest-xdist: workers attach each test's labels to its report (user_properties travel
with reports), and only the controller writes the file.
"""
from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

LAYERS = ("unit", "contract", "integration", "e2e", "property", "robustness", "data", "perf", "ui")
_HOME = str(Path.home())


def pytest_addoption(parser: pytest.Parser) -> None:
    g = parser.getgroup("quality-lab")
    g.addoption("--ql-out", default="", help="write this run's record to this JSON file")
    g.addoption("--ql-label", default="", help="a short label stored with the run (why it ran)")


def _clean(text: str, limit: int = 1800) -> str:
    """Failure text without local paths, so a record is safe to publish."""
    text = text.replace(_HOME, "~").replace(_HOME.replace("\\", "/"), "~")
    text = re.sub(r"[A-Za-z]:\\[^\s:'\"]*\\(?=[\w.-]+\.py)", "", text)  # absolute dirs before a .py name
    return text if len(text) <= limit else text[:limit] + "\n... (truncated)"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when != "call" and not (report.when == "setup" and report.outcome != "passed"):
        return
    names = [mk.name for mk in item.iter_markers()]
    layer = next((n for n in names if n in LAYERS), "unlabelled")
    area_mark = item.get_closest_marker("area")
    doc = (getattr(item.obj, "__doc__", None) or "").strip().splitlines()
    report.user_properties.append(("ql", {
        "layer": layer,
        "area": area_mark.args[0] if area_mark and area_mark.args else "",
        "markers": sorted(set(n for n in names if n not in ("parametrize", "usefixtures", "area", "filterwarnings"))),
        "doc": doc[0] if doc else "",
        "file": item.location[0].replace("\\", "/"),
        "line": (item.location[1] or 0) + 1,
    }))


class Recorder:
    def __init__(self, config: pytest.Config, out: Path) -> None:
        self.config = config
        self.out = out
        self.started = time.time()
        self.tests: dict[str, dict] = {}

    def _rec(self, nodeid: str) -> dict:
        return self.tests.setdefault(nodeid, {"id": nodeid, "outcome": "passed", "duration": 0.0, "call": 0.0,
                                              "start": None, "stop": None, "worker": "main", "message": ""})

    @pytest.hookimpl
    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        # xdist's --dist loadgroup appends "@group" to grouped node ids; strip it so a test keeps one id
        # across runs with and without grouping
        r = self._rec(re.sub(r"@[\w-]+$", "", report.nodeid))
        r["duration"] = round(r["duration"] + (report.duration or 0.0), 4)
        start, stop = getattr(report, "start", None), getattr(report, "stop", None)
        if start is not None:
            r["start"] = start if r["start"] is None else min(r["start"], start)
        if stop is not None:
            r["stop"] = stop if r["stop"] is None else max(r["stop"], stop)
        node = getattr(report, "node", None)
        gateway = getattr(node, "gateway", None)
        if gateway is not None:
            r["worker"] = gateway.id
        for key, value in report.user_properties:
            if key == "ql":
                r.update(value)
            elif key == "metric" and report.when == "call":  # record_property("metric", {...}) in a test
                r.setdefault("metrics", []).append(value)
        wasxfail = getattr(report, "wasxfail", None)
        if report.when == "call":
            r["call"] = round(report.duration or 0.0, 4)
            if wasxfail is not None:
                r["outcome"] = "xfailed" if report.skipped else "xpassed"
                r["message"] = _clean(str(wasxfail))
            elif report.failed:
                r["outcome"] = "failed"
                r["message"] = _clean(report.longreprtext)
            elif report.skipped:
                r["outcome"] = "skipped"
        elif report.failed:
            r["outcome"] = "error"
            r["message"] = _clean(report.longreprtext)
        elif report.skipped and report.when == "setup":
            r["outcome"] = "xfailed" if wasxfail is not None else "skipped"
            if isinstance(report.longrepr, tuple) and len(report.longrepr) == 3:
                r["message"] = _clean(str(report.longrepr[2]))

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        finished = time.time()
        tests = sorted(self.tests.values(), key=lambda t: (t["start"] or 0, t["id"]))
        for t in tests:  # store times relative to the run start: smaller, and replayable
            for k in ("start", "stop"):
                t[k] = round(t[k] - self.started, 3) if t[k] is not None else None
        counts: dict[str, int] = {}
        for t in tests:
            counts[t["outcome"]] = counts.get(t["outcome"], 0) + 1
        target = getattr(self.config, "_ql_target", None)
        record = {
            "schema": 1,
            "id": datetime.fromtimestamp(self.started, timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6],
            "label": self.config.getoption("--ql-label"),
            "started": datetime.fromtimestamp(self.started, timezone.utc).isoformat(timespec="seconds"),
            "wall": round(finished - self.started, 2),
            "target": getattr(target, "name", ""),
            "tick_ms": getattr(target, "tick_ms", None),
            "seeded": getattr(target, "seeded", None),
            "workers": int(getattr(self.config.option, "numprocesses", 0) or 0),
            "exitstatus": int(exitstatus),
            "counts": counts,
            "env": {
                "python": platform.python_version(),
                "pytest": pytest.__version__,
                "os": platform.system(),
                "lab_commit": _git_sha(Path(__file__).resolve().parent.parent),
                "sut_commit": _git_sha(Path(os.environ["SPIRESCAPE_REPO"])) if os.environ.get("SPIRESCAPE_REPO") else "",
            },
            "protocol": _merge_traffic(),
            "tests": tests,
        }
        self.out.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.out.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, indent=1), encoding="utf-8")
        os.replace(tmp, self.out)


def _git_sha(repo: Path) -> str:
    try:
        out = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"], capture_output=True,
                             text=True, timeout=10)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _merge_traffic() -> dict:
    """Message types sent and received, summed over every worker's traffic file."""
    merged: dict[str, dict[str, int]] = {"sent": {}, "received": {}}
    folder = os.environ.get("QL_ARTIFACT_DIR", "")
    if not folder:
        return merged
    for f in Path(folder).glob("traffic-*.json"):
        data = json.loads(f.read_text(encoding="utf-8"))
        for direction in merged:
            for name, n in data.get(direction, {}).items():
                merged[direction][name] = merged[direction].get(name, 0) + n
    return merged


def _dump_traffic(config: pytest.Config) -> None:
    folder = os.environ.get("QL_ARTIFACT_DIR", "")
    client = __import__("sys").modules.get("qualitylab.spirescape.client")
    if not folder or client is None:
        return
    worker = getattr(config, "workerinput", {}).get("workerid", "main")
    (Path(folder) / f"traffic-{worker}.json").write_text(json.dumps(client.TRAFFIC), encoding="utf-8")


@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    _dump_traffic(session.config)  # every process, before the controller's recorder merges them


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "area(name): the part of the system a test covers")
    out = config.getoption("--ql-out", default="")
    if out and not hasattr(config, "workerinput"):
        import tempfile
        os.environ["QL_ARTIFACT_DIR"] = tempfile.mkdtemp(prefix="ql-")  # workers inherit it
        config.pluginmanager.register(Recorder(config, Path(out)), "ql-recorder")
