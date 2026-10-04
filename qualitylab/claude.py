"""One headless Claude call through the Claude Code CLI (`claude -p`), with structured output.

The long context goes in on stdin (no command-line length limit), the instruction is the prompt, and
a JSON Schema constrains the answer. No tools, no settings, no MCP servers, no saved session: the
model only sees what it's given here.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass


class ClaudeError(RuntimeError):
    pass


@dataclass
class Reply:
    data: dict
    model: str        # the canonical model id that answered, e.g. "claude-sonnet-5-5"
    cost_usd: float
    seconds: float


def find_claude() -> str:
    found = shutil.which("claude")
    if found:
        return found
    raise ClaudeError("the claude CLI isn't on PATH")


def ask(instruction: str, context: str, schema: dict, *, model: str = "sonnet", timeout: int = 900) -> Reply:
    env = dict(os.environ)
    env["CLAUDE_SUMMARY_WORKER"] = "1"   # skip the global hooks for this worker call
    env.pop("ANTHROPIC_API_KEY", None)   # run on the Claude Code login, not a stray key
    cmd = [find_claude(), "-p", instruction, "--model", model, "--output-format", "json",
           "--json-schema", json.dumps(schema), "--tools", "", "--safe-mode", "--strict-mcp-config",
           "--no-session-persistence"]
    try:
        proc = subprocess.run(cmd, input=context, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise ClaudeError(f"no answer within {timeout}s") from e
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise ClaudeError(f"unreadable reply (exit {proc.returncode}): {proc.stderr[:300] or proc.stdout[:300]}") from e
    if out.get("is_error") or out.get("subtype") != "success":
        raise ClaudeError(f"claude reported an error: {out.get('subtype')} {str(out.get('result'))[:300]}")
    data = out.get("structured_output")
    if data is None:
        try:
            data = json.loads(out.get("result") or "")
        except json.JSONDecodeError as e:
            raise ClaudeError("reply had no structured output") from e
    usage = out.get("modelUsage") or {}
    main = max(usage.items(), key=lambda kv: kv[1].get("outputTokens", 0), default=(model, {}))
    return Reply(data=data, model=main[1].get("canonicalModel") or main[0], cost_usd=float(out.get("total_cost_usd") or 0),
                 seconds=round((out.get("duration_ms") or 0) / 1000, 1))
