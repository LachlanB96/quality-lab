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


def test_a_missing_toolchain_says_how_to_fix_it(tmp_path):
    with pytest.raises(RuntimeError, match="npm ci"):
        build.tool("esbuild", tmp_path)
