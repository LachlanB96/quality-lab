"""`ql`: run the suite into history, then analyse, evaluate and build the report.

    ql run --target local|live [--workers 4] [--label why]   one recorded run
    ql build [--out build/]                                   the report page from history
    ql analyse                                                Claude reviews the latest runs (see analyst.py)
    ql eval email                                             the email-triage evals (see evals/)
    ql review <test-id-substring> --status reviewed|rewritten [--note ...]
    ql suggestion <id> --status accepted|rejected|done [--note ...]
    ql nightly                                                run local + live, eval, analyse, build
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HISTORY = ROOT / "history"
RUNS = HISTORY / "runs"


def cmd_run(args: argparse.Namespace) -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = RUNS / f"{stamp}-{args.target}.json"
    paths = ["tests"] if args.target == "local" else ["tests/spirescape"]
    cmd = [sys.executable, "-m", "pytest", *paths, "--target", args.target, "-q",
           "-W", "ignore::DeprecationWarning", f"--ql-out={out}", f"--ql-label={args.label}"]
    if args.workers > 1:
        cmd += ["-n", str(args.workers), "--dist", "loadgroup"]
    cmd += [a for i, a in enumerate(args.pytest_args) if not (i == 0 and a == "--")]
    print("$ " + " ".join(cmd[2:]))
    code = subprocess.call(cmd, cwd=ROOT)
    if out.exists():
        run = json.loads(out.read_text(encoding="utf-8"))
        print(f"recorded {out.relative_to(ROOT)}: {run['counts']} in {run['wall']}s")
    return code


def cmd_build(args: argparse.Namespace) -> int:
    from .report import build
    path = build.build(Path(args.out))
    print(f"built {path}")
    return 0


def cmd_analyse(args: argparse.Namespace) -> int:
    from . import analyst
    return analyst.main(model=args.model)


def cmd_eval(args: argparse.Namespace) -> int:
    from .evals import email_triage
    return email_triage.main(repeats=args.repeats, variants=args.variants)


def _edit_json(path: Path, update) -> None:
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    update(data)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def cmd_review(args: argparse.Namespace) -> int:
    """Record that the owner reviewed (or rewrote) tests whose id contains a substring."""
    from . import history
    runs = history.load_runs(RUNS)
    ids = sorted({t["id"] for r in runs for t in r["tests"] if args.match in t["id"]})
    if not ids:
        print(f"no recorded test matches {args.match!r}")
        return 1
    today = datetime.now().strftime("%Y-%m-%d")

    def upd(data: dict) -> None:
        for i in ids:
            data[i] = {"status": args.status, "date": today, **({"note": args.note} if args.note else {})}
    _edit_json(ROOT / "review.json", upd)
    print(f"{args.status}: {len(ids)} test(s)")
    return 0


def cmd_suggestion(args: argparse.Namespace) -> int:
    path = ROOT / "suggestions.json"
    found = False

    def upd(data: dict) -> None:
        nonlocal found
        for s in data.get("suggestions", []):
            if s["id"] == args.id:
                s["status"], s["decided"] = args.status, datetime.now().strftime("%Y-%m-%d")
                if args.note:
                    s["note"] = args.note
                found = True
    _edit_json(path, upd)
    print(f"{args.id}: {args.status}" if found else f"no suggestion {args.id}")
    return 0 if found else 1


def cmd_nightly(args: argparse.Namespace) -> int:
    codes = [cmd_run(argparse.Namespace(target="local", workers=4, label="nightly", pytest_args=[])),
             cmd_run(argparse.Namespace(target="live", workers=4, label="nightly", pytest_args=[]))]
    if not args.no_eval:
        codes.append(cmd_eval(argparse.Namespace(repeats=2, variants=["production", "full-body"])))
    if not args.no_ai:
        codes.append(cmd_analyse(argparse.Namespace(model="sonnet")))
    codes.append(cmd_build(argparse.Namespace(out=str(ROOT / "build"))))
    return max(codes)


def main(argv: list[str] | None = None) -> int:
    os.environ.setdefault("PYTHONUTF8", "1")
    p = argparse.ArgumentParser(prog="ql", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="one recorded test run")
    r.add_argument("--target", choices=("local", "live"), default="local")
    r.add_argument("--workers", type=int, default=4)
    r.add_argument("--label", default="manual")
    r.add_argument("pytest_args", nargs=argparse.REMAINDER)
    r.set_defaults(fn=cmd_run)

    b = sub.add_parser("build", help="build the report page")
    b.add_argument("--out", default=str(ROOT / "build"))
    b.set_defaults(fn=cmd_build)

    a = sub.add_parser("analyse", help="Claude's overview and suggestions for the latest runs")
    a.add_argument("--model", default="sonnet")
    a.set_defaults(fn=cmd_analyse)

    e = sub.add_parser("eval", help="AI evals")
    e.add_argument("suite", choices=("email",))
    e.add_argument("--repeats", type=int, default=2)
    e.add_argument("--variants", nargs="+", default=["production", "full-body"])
    e.set_defaults(fn=cmd_eval)

    rv = sub.add_parser("review", help="mark tests as reviewed by the owner")
    rv.add_argument("match")
    rv.add_argument("--status", choices=("reviewed", "rewritten"), default="reviewed")
    rv.add_argument("--note", default="")
    rv.set_defaults(fn=cmd_review)

    sg = sub.add_parser("suggestion", help="decide on an AI suggestion")
    sg.add_argument("id")
    sg.add_argument("--status", choices=("accepted", "rejected", "done", "open"), required=True)
    sg.add_argument("--note", default="")
    sg.set_defaults(fn=cmd_suggestion)

    n = sub.add_parser("nightly", help="run everything and rebuild the page")
    n.add_argument("--no-ai", action="store_true")
    n.add_argument("--no-eval", action="store_true")
    n.set_defaults(fn=cmd_nightly)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
