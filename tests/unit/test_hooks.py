"""The git hooks hand over to the machine's own hooks first, on a clone that sets namemask.mode, and block when
that can't happen. Each test builds a scratch repo with this repo's .githooks and scripts/machine-hook.sh, a
fake global git config whose core.hooksPath is a folder of fake machine hooks, and a stand-in leak check, so it
needs git and bash but not gitleaks or the blocklist."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.area("hooks")]

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ("pre-commit", "commit-msg", "pre-push")

FAKE_MACHINE_HOOK = """#!/bin/sh
echo "machine {hook} $*" >>"$QL_TEST_LOG"
if [ {hook} = pre-push ]; then sed 's/^/stdin /' >>"$QL_TEST_LOG"; fi
[ "$QL_TEST_FAIL" = {hook} ] && exit 1
exit 0
"""

FAKE_LEAK_CHECK = """#!/usr/bin/env bash
echo "leak-check $1" >>"$QL_TEST_LOG"
"""


class Clone:
    def __init__(self, tmp: Path):
        self.log = tmp / "log.txt"
        self.machine = tmp / "machine-hooks"
        self.machine.mkdir()
        for hook in HOOKS:
            (self.machine / hook).write_text(FAKE_MACHINE_HOOK.format(hook=hook), newline="\n")
        self.global_config = tmp / "global.gitconfig"
        self.global_config.write_text(
            f"[core]\n\thooksPath = {self.machine.as_posix()}\n[user]\n\tname = test\n\temail = test@example.invalid\n")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update(GIT_CONFIG_GLOBAL=str(self.global_config), GIT_CONFIG_NOSYSTEM="1",
                        QL_TEST_LOG=str(self.log), QL_TEST_FAIL="")
        self.dir = tmp / "repo"
        self.git("init", "-q", "-b", "main", str(self.dir), cwd=tmp)
        shutil.copytree(ROOT / ".githooks", self.dir / ".githooks")
        (self.dir / "scripts").mkdir()
        shutil.copy2(ROOT / "scripts" / "machine-hook.sh", self.dir / "scripts")
        (self.dir / "scripts" / "leak-check.sh").write_text(FAKE_LEAK_CHECK, newline="\n")
        self.git("config", "core.hooksPath", ".githooks")
        self.git("init", "-q", "--bare", str(tmp / "remote.git"), cwd=tmp)
        self.git("remote", "add", "origin", (tmp / "remote.git").as_posix())
        self.commits = 0

    def git(self, *args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
        r = subprocess.run(["git", *args], cwd=cwd or self.dir, env=self.env,
                           capture_output=True, text=True, encoding="utf-8")
        assert not check or r.returncode == 0, r.stdout + r.stderr
        return r

    def run(self, *args: str) -> tuple[subprocess.CompletedProcess, list[str]]:
        """Runs a git command that fires hooks, and returns it with the lines the hooks logged."""
        self.log.write_text("")
        r = self.git(*args, check=False)
        return r, self.log.read_text().splitlines()

    def commit(self):
        self.commits += 1
        (self.dir / "f.txt").write_text(f"{self.commits}\n")
        self.git("add", "f.txt")
        return self.run("commit", "-q", "-m", f"commit {self.commits}")


@pytest.fixture
def clone(tmp_path) -> Clone:
    return Clone(tmp_path)


def test_without_namemask_only_the_leak_check_runs(clone):
    r, log = clone.commit()
    assert r.returncode == 0, r.stderr
    assert log == ["leak-check --staged", "leak-check --msg"]


def test_with_namemask_each_machine_hook_runs_before_the_leak_check(clone):
    clone.git("config", "namemask.mode", "block")
    r, log = clone.commit()
    assert r.returncode == 0, r.stderr
    assert log == ["machine pre-commit ", "leak-check --staged", "machine commit-msg .git/COMMIT_EDITMSG",
                   "leak-check --msg"]


@pytest.mark.parametrize("hook, ran", [
    ("pre-commit", ["machine pre-commit "]),
    ("commit-msg", ["machine pre-commit ", "leak-check --staged", "machine commit-msg .git/COMMIT_EDITMSG"]),
])
def test_a_machine_hook_that_refuses_blocks_the_commit(clone, hook, ran):
    clone.git("config", "namemask.mode", "block")
    clone.env["QL_TEST_FAIL"] = hook
    r, log = clone.commit()
    assert r.returncode != 0
    assert log == ran
    assert clone.git("rev-list", "--all").stdout == "", "nothing was committed"


def test_namemask_without_a_global_hooks_folder_blocks(clone):
    clone.git("config", "namemask.mode", "block")
    clone.global_config.write_text("[user]\n\tname = test\n\temail = test@example.invalid\n")
    r, log = clone.commit()
    assert r.returncode != 0
    assert "no core.hooksPath" in r.stderr
    assert log == []


def test_namemask_with_a_machine_hook_missing_blocks(clone):
    clone.git("config", "namemask.mode", "block")
    (clone.machine / "commit-msg").unlink()
    r, log = clone.commit()
    assert r.returncode != 0
    assert "commit-msg hook is missing" in r.stderr
    assert log == ["machine pre-commit ", "leak-check --staged"]


def test_pre_push_hands_the_machine_hook_the_remote_and_the_refs(clone):
    clone.commit()
    clone.git("config", "namemask.mode", "block")
    head = clone.git("rev-parse", "HEAD").stdout.strip()
    r, log = clone.run("push", "-q", "origin", "main")
    assert r.returncode == 0, r.stderr
    assert log[0].startswith("machine pre-push origin ")
    assert log[1:] == [f"stdin refs/heads/main {head} refs/heads/main {'0' * 40}", "leak-check --all"]


def test_a_machine_pre_push_that_refuses_blocks_the_push(clone):
    clone.commit()
    clone.git("config", "namemask.mode", "block")
    clone.env["QL_TEST_FAIL"] = "pre-push"
    r, log = clone.run("push", "-q", "origin", "main")
    assert r.returncode != 0
    assert "leak-check --all" not in log
    assert clone.git("ls-remote", "origin").stdout == "", "nothing reached the remote"


def test_pre_push_without_namemask_runs_the_leak_check_alone(clone):
    clone.commit()
    r, log = clone.run("push", "-q", "origin", "main")
    assert r.returncode == 0, r.stderr
    assert log == ["leak-check --all"]
