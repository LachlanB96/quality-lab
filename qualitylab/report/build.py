"""Build the report page: run history in, one self-contained index.html (plus fonts/) out.

The page loads nothing from other origins. All data is embedded as JSON and rendered by the page's
own script, so the build is a data-shaping step; the look lives in template.html, and the script is
page.ts (TypeScript), which esbuild bundles into the page here. `npm ci` once after cloning installs it.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
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


def pooled_calibration(variants: list[dict]) -> dict | None:
    """Every variant's certainty bands added together: entries, mean certainty and share right per band, and the
    expected calibration error over all of them (exact from the bands, since it's a weighted mean of band gaps)."""
    bands: dict[str, list[float]] = {}
    for v in variants:
        for b in (v.get("calibration") or {}).get("bands", []):
            n, cert, right = bands.setdefault(b["band"], [0, 0.0, 0.0]), b["certainty"], b["right"]
            n[0] += b["entries"]
            n[1] += cert * b["entries"]
            n[2] += right * b["entries"]
    total = sum(n for n, _, _ in bands.values())
    if not total:
        return None
    rows = [{"band": k, "entries": int(n), "certainty": round(c / n, 1), "right": round(r / n, 4)}
            for k, (n, c, r) in sorted(bands.items(), key=lambda kv: int(kv[0].split("-")[0]))]
    ece = sum(r["entries"] / total * abs(r["right"] - r["certainty"] / 100) for r in rows)
    return {"entries": int(total), "bands": rows, "ece": round(ece, 4), "all_right": all(r["right"] == 1 for r in rows)}


def shape_evals(record: dict | None, root: Path = ROOT) -> dict | None:
    """The newest eval record, cut to what the page shows. With more than two cuts it adds the staircase: each
    case with a date it must find whose deciding text ends past the first cut, so the cut can hide it, with where
    that text ends and the share of runs that caught the date at each cut, in order of how far in it sits."""
    if record is None:
        return None
    from ..evals import email_triage as et
    variants = record.get("variants", [])
    out = {k: record.get(k) for k in ("title", "summary", "legend", "baseline", "candidate", "metrics", "triage",
                                      "repeats", "cases", "model")}
    out["variants"] = [{"name": v["name"], "body_chars": v.get("body_chars"), "chars_sent": v.get("chars_sent"),
                        "scores": v["scores"], "input_per_mail": (v.get("tokens") or {}).get("input_per_mail")}
                       for v in variants]
    if len(variants) < 3:
        return out
    first = variants[0].get("body_chars")
    stairs = []
    for c in et.load_cases(root / "evals" / "email_triage" / "cases.json")[0]:
        end = et.evidence_end(c)
        if not c["gold"].get("event") or c["gold"].get("event_optional") or first is None or end <= first:
            continue
        found = [v.get("per_case", {}).get(c["id"], {}).get("event_found") for v in variants]
        if any(f is not None for f in found):
            stairs.append({"id": c["id"], "deciding_at": end, "found": found})
    out["staircase"] = sorted(stairs, key=lambda s: (s["deciding_at"], s["id"]))
    out["calibration"] = pooled_calibration(variants)
    return out


def collect(root: Path = ROOT) -> dict:
    runs = h.load_runs(root / "history" / "runs")
    targets = {t: shape_target(r) for t in ("local", "live") if (r := h.latest(runs, t))}
    findings = _load(root / "findings.json", {"findings": []})["findings"]
    live = h.latest(runs, "live")
    for f in findings:  # status comes from the latest runs: still pinned by an xfail, or now passing
        pinned = [b for tgt in targets.values() for k, b in tgt["bugs"].items() if k == f["id"]]
        f["tests"] = sorted({tid for b in pinned for tid in b["tests"]})
        if f.get("metrics"):
            # an intermittent defect can't be pinned by a strict xfail: it's open when the latest live run saw it
            # (a test recorded one of its metrics, or xfailed with its id) and "unseen" when that run didn't
            seen = [(t["id"], mt["value"]) for t in (live["tests"] if live else [])
                    for mt in t.get("metrics", []) if mt["name"] in f["metrics"] and mt["value"]]
            f["tests"] = sorted(set(f["tests"]) | {tid for tid, _ in seen})
            f["seen"] = int(sum(v for _, v in seen))
            f["status"] = "fixed" if f.get("fixed") else ("open" if (pinned or seen) else "unseen")
            continue
        # a `fixed` date is the owner closing the finding once the marker came off; an XPASS is the fix showing up
        f["status"] = "fixed" if f.get("fixed") or any(b["fixed"] for b in pinned) else ("open" if pinned else "untested")
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
        "evals": shape_evals(latest_json(root / "history" / "evals"), root),
        "review": {
            "total": len(distinct),
            "reviewed": sum(1 for i in distinct if review.get(i, {}).get("status") == "reviewed"),
            "rewritten": sum(1 for i in distinct if review.get(i, {}).get("status") == "rewritten"),
            "by_test": {i: review[i]["status"] for i in distinct if i in review},
        },
    }


def tool(name: str, root: Path = ROOT) -> str:
    """A tool `npm ci` installed in node_modules/.bin (esbuild, tsc): the .cmd shim on Windows."""
    found = shutil.which(name, path=str(root / "node_modules" / ".bin"))
    if not found:
        raise RuntimeError(f"{name} isn't installed: run `npm ci` in {root} first")
    return found


def page_script(root: Path = ROOT) -> str:
    """page.ts as one plain script for the page, bundled by esbuild."""
    r = subprocess.run([tool("esbuild", root), str(HERE / "page.ts"), "--bundle", "--format=iife", "--target=es2022", "--log-level=warning"],
                       capture_output=True, text=True, encoding="utf-8")
    if r.returncode or not r.stdout.strip():
        raise RuntimeError(f"esbuild couldn't bundle page.ts (exit {r.returncode}):\n{r.stderr.strip()}")
    if "</script" in r.stdout.lower():
        raise ValueError("the bundled page script would close its script element early")
    return r.stdout


def render(data: dict) -> str:
    template = (HERE / "template.html").read_text(encoding="utf-8")
    fonts = (HERE / "fonts" / "fonts.css").read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("</", "<\\/").replace("<!--", "<\\!--")  # can't close the script element early
    html = template.replace("/*@@FONTS@@*/", fonts)
    html = html.replace("/*@@DATA@@*/", payload)
    html = html.replace("<!--@@PAGE@@-->", "<script>\n" + page_script() + "</script>")
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
