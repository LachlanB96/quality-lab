"""Build the report page: run history in, one self-contained index.html (plus fonts/) out.

The page loads nothing from other origins. All data is embedded as JSON and rendered by the page's
own script, so the build is a data-shaping step; the look lives in template.html.
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .. import history as h
from ..spirescape import messages as m

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
REPO_URL = "https://github.com/LachlanB96/quality-lab"
DEMO_URL = "https://spirescape.lachlanb.com/"


def humanize(test_id: str) -> dict:
    """tests/spirescape/test_world.py::test_move_to_walks[a-b] -> file 'world', name 'Move to walks', param 'a-b'."""
    path, _, func = test_id.partition("::")
    stem = Path(path).stem.removeprefix("test_")
    param = ""
    if "[" in func and func.endswith("]"):
        func, param = func[:-1].split("[", 1)
    words = " ".join(WORDS.get(w, w) for w in func.removeprefix("test_").split("_") if w)
    return {"file": stem, "suite": Path(path).parent.name, "name": words[:1].upper() + words[1:], "param": param}


# Function names can't hold apostrophes or capitals; put them back for display.
WORDS = {"cant": "can't", "dont": "don't", "doesnt": "doesn't", "isnt": "isn't", "wont": "won't", "elses": "else's",
         "npcs": "NPCs", "dotnet": ".NET", "utf8": "UTF-8", "int32": "int32", "ui": "UI", "e2e": "end-to-end"}


def shape_test(t: dict) -> dict:
    cls = h.outcome_class(t["outcome"])
    out = {"id": t["id"], **humanize(t["id"]), "layer": t.get("layer", ""), "area": t.get("area", ""),
           "cls": cls, "start": t.get("start"), "stop": t.get("stop"), "dur": t.get("duration", 0.0),
           "worker": t.get("worker", "main"), "doc": t.get("doc", "")}
    if cls in (h.FAIL, h.KNOWN, h.FIXED):
        out["msg"] = (t.get("message") or "")[:600]
    return out


def shape_target(run: dict) -> dict:
    return {
        "summary": h.summarize(run),
        "label": run.get("label", ""),
        "tick_ms": run.get("tick_ms"),
        "seeded": run.get("seeded"),
        "workers": run.get("workers", 0),
        "commit": run.get("env", {}).get("lab_commit", ""),
        "tests": [shape_test(t) for t in run["tests"]],
        "layers": {k: dict(v) for k, v in h.by_layer(run).items()},
        "matrix": h.matrix(run),
        "bugs": h.known_bugs(run),
        "slowest": [{**s, **humanize(s["id"])} for s in h.slowest(run, 8)],
        "protocol": h.protocol_coverage(run, [c.__name__ for c in sorted(m.REGISTRY.values(), key=lambda c: c.type_id)]),
        "metrics": {mt["name"]: mt for t in run["tests"] for mt in t.get("metrics", [])},
    }


def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def latest_json(folder: Path) -> dict | None:
    files = sorted(folder.glob("*.json")) if folder.is_dir() else []
    return _load(files[-1], None) if files else None


def collect(root: Path = ROOT) -> dict:
    runs = h.load_runs(root / "history" / "runs")
    targets = {t: shape_target(r) for t in ("local", "live") if (r := h.latest(runs, t))}
    findings = _load(root / "findings.json", {"findings": []})["findings"]
    for f in findings:  # status comes from the latest runs: still pinned by an xfail, or now passing
        pinned = [b for tgt in targets.values() for k, b in tgt["bugs"].items() if k == f["id"]]
        f["tests"] = sorted({tid for b in pinned for tid in b["tests"]})
        f["status"] = "fixed" if any(b["fixed"] for b in pinned) else ("open" if pinned else "untested")
    review = _load(root / "review.json", {})
    distinct = sorted({t["id"] for tgt in targets.values() for t in tgt["tests"]})
    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repo": REPO_URL,
        "demo": DEMO_URL,
        "targets": targets,
        "runs": [h.summarize(r) for r in runs],
        "metrics": h.metric_series(runs),
        "flaky": h.flaky(runs),
        "findings": findings,
        "analysis": latest_json(root / "history" / "analysis"),
        "suggestions": _load(root / "suggestions.json", {}).get("suggestions", []),
        "evals": latest_json(root / "history" / "evals"),
        "review": {
            "total": len(distinct),
            "reviewed": sum(1 for i in distinct if review.get(i, {}).get("status") == "reviewed"),
            "rewritten": sum(1 for i in distinct if review.get(i, {}).get("status") == "rewritten"),
            "by_test": {i: review[i]["status"] for i in distinct if i in review},
        },
    }


def render(data: dict) -> str:
    template = (HERE / "template.html").read_text(encoding="utf-8")
    fonts = (HERE / "fonts" / "fonts.css").read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("</", "<\\/").replace("<!--", "<\\!--")  # can't close the script element early
    html = template.replace("/*@@FONTS@@*/", fonts)
    html = html.replace("/*@@DATA@@*/", payload)
    if "@@" in re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.S):
        raise ValueError("an unreplaced placeholder is left in the template")
    return html


def build(out: Path, root: Path = ROOT) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    (out / "fonts").mkdir(exist_ok=True)
    for f in (HERE / "fonts").iterdir():
        if f.suffix in (".woff2", ".txt"):
            shutil.copy2(f, out / "fonts" / f.name)
    page = out / "index.html"
    page.write_text(render(collect(root)), encoding="utf-8")
    return page
