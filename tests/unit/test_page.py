"""The report page's script: page.ts type-checks with TypeScript 7, and esbuild's bundle of it lands in the built
page once, after the data it reads. Needs `npm ci` (esbuild and TypeScript in node_modules)."""
import subprocess

import pytest

from qualitylab.report import build

pytestmark = [pytest.mark.unit, pytest.mark.area("report")]


def test_page_ts_type_checks():
    r = subprocess.run([build.tool("tsc"), "--noEmit", "-p", str(build.HERE / "tsconfig.json")],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_built_page_carries_the_bundled_script_after_its_data(tmp_path):
    html = build.render(build.collect(tmp_path))   # an empty lab: no runs, findings or reviews
    assert "@@PAGE@@" not in html
    data, script = html.index('<script type="application/json" id="ql-data">'), html.index("function renderAll()")
    assert data < script, "the script reads #ql-data, so it has to come after it"
    assert html.count("function renderAll()") == 1
    assert "page.ts" in html, "esbuild's bundle names its source"


def variant(chars, found, bands=()):
    return {"name": "full" if chars is None else str(chars), "body_chars": chars, "chars_sent": 100.0,
            "scores": {"event_recall": 0.5}, "tokens": {"input_per_mail": 2600.0},
            "calibration": {"bands": list(bands), "brier": 0.03},
            "per_case": {cid: {"event_found": f} for cid, f in found.items()}, "misses": ["not for the page"]}


def lab(tmp_path, cases):
    import json
    (tmp_path / "evals" / "email_triage").mkdir(parents=True)
    (tmp_path / "evals" / "email_triage" / "cases.json").write_text(
        json.dumps({"received": "2026-10-02T09:30:00+10:00", "cases": cases}), encoding="utf-8")
    return tmp_path


def case(cid, body, evidence, event=True, optional=False):
    gold = {"event": {"date": "2026-10-09", "time": "14:00"} if event else None}
    if optional:
        gold["event_optional"] = True
    return {"id": cid, "body": body, "evidence": evidence, "gold": gold}


def test_the_staircase_has_the_dates_past_the_first_cut_in_order_of_how_far_in(tmp_path):
    root = lab(tmp_path, [case("far", "x" * 1500 + " Friday 2pm", "Friday 2pm"),
                          case("near", "x" * 700 + " Friday 2pm", "Friday 2pm"),
                          case("short", "Friday 2pm", "Friday 2pm"),
                          case("optional", "x" * 900 + " Friday 2pm", "Friday 2pm", optional=True),
                          case("no-date", "x" * 900 + " no date", "no date", event=False)])
    found = {"far": 0.0, "near": 0.0, "short": 0.8, "optional": 0.4}
    record = {"title": "t", "repeats": 5, "variants": [variant(600, found), variant(1000, {**found, "near": 1.0}),
                                                       variant(None, {**found, "near": 1.0, "far": 1.0})]}
    got = build.shape_evals(record, root)
    assert [s["id"] for s in got["staircase"]] == ["near", "far"], "short ones can't be cut off; optional ones aren't required"
    assert got["staircase"][0] == {"id": "near", "deciding_at": 711, "found": [0.0, 1.0, 1.0]}
    assert "misses" not in json_text(got) and got["variants"][2]["input_per_mail"] == 2600.0


def json_text(x):
    import json
    return json.dumps(x)


def test_two_cuts_get_no_staircase_and_need_no_dataset(tmp_path):
    got = build.shape_evals({"variants": [variant(600, {}), variant(None, {})]}, tmp_path)
    assert "staircase" not in got and len(got["variants"]) == 2


def test_the_certainty_bands_pool_across_cuts():
    a = variant(600, {}, [{"band": "90-94", "entries": 10, "certainty": 90.0, "right": 1.0}])
    b = variant(None, {}, [{"band": "90-94", "entries": 30, "certainty": 94.0, "right": 0.9},
                           {"band": "0-59", "entries": 10, "certainty": 40.0, "right": 1.0}])
    got = build.pooled_calibration([a, b])
    assert got["entries"] == 50 and not got["all_right"]
    assert got["bands"] == [{"band": "0-59", "entries": 10, "certainty": 40.0, "right": 1.0},
                            {"band": "90-94", "entries": 40, "certainty": 93.0, "right": 0.925}]
    assert got["ece"] == round(10 / 50 * 0.6 + 40 / 50 * abs(0.925 - 0.93), 4)
    assert build.pooled_calibration([variant(600, {})]) is None


def test_a_missing_toolchain_says_how_to_fix_it(tmp_path):
    with pytest.raises(RuntimeError, match="npm ci"):
        build.tool("esbuild", tmp_path)
