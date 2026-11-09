"""CI's run records, pulled from GitHub Actions artifacts into history/runs.

Each Tests run on GitHub uploads a `live-run` artifact: live.xml and history/runs/*-live.json, the records the
checkout had plus the night's own. CI can't commit (the records are the page's data, and the page is built here),
so this is a night's way onto the page: `fetch` downloads one folder per finished run with gh, `merge` copies the
records history/runs doesn't have yet, and `ql build` draws them.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path

ARTIFACT = "live-run"


def fetch(repo: str, *, limit: int = 40, since: str = "", workflow: str = "Tests") -> Iterator[Path]:
    """One downloaded artifact folder per finished run of the workflow, newest first.

    `since` (YYYY-MM-DD) leaves out older runs. A run without the artifact (its unit job failed first, or the
    artifact expired after GitHub's 90 days) is skipped. Needs `gh`, logged in."""
    listed = subprocess.run(["gh", "run", "list", "-R", repo, "--workflow", workflow, "--status", "completed",
                             "--limit", str(limit), "--json", "databaseId,createdAt"],
                            capture_output=True, text=True, check=True)
    runs = [r for r in json.loads(listed.stdout) if r["createdAt"][:10] >= since]
    with tempfile.TemporaryDirectory(prefix="ql-pull-") as tmp:
        for r in runs:
            dest = Path(tmp) / str(r["databaseId"])
            got = subprocess.run(["gh", "run", "download", str(r["databaseId"]), "-R", repo, "-n", ARTIFACT,
                                  "-D", str(dest)], capture_output=True, text=True)
            if got.returncode == 0:
                yield dest


def is_live_record(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and data.get("schema") == 1 and data.get("target") == "live" and "tests" in data


def merge(runs_dir: Path, folders: Iterable[Path]) -> list[str]:
    """Copy every live run record under <folder>/history/runs that runs_dir lacks. The names added, sorted."""
    added: list[str] = []
    runs_dir.mkdir(parents=True, exist_ok=True)
    for folder in folders:
        for f in sorted(folder.glob("history/runs/*-live.json")):
            target = runs_dir / f.name
            if target.exists() or not is_live_record(f):
                continue
            shutil.copyfile(f, target)
            added.append(f.name)
    return sorted(added)
