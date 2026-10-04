"""Where the suite points: a local SpireScape server it starts itself, or the live public demo.

Local runs build the server from a SpireScape checkout (SPIRESCAPE_REPO) and start one process per
pytest worker with a short tick and a fixed seed, so walking takes milliseconds and fights replay
the same way. Live runs talk to spirescape.lachlanb.com at its real 600 ms tick.
"""
from __future__ import annotations

import collections
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

LIVE_HOST = "spirescape.lachlanb.com"


@dataclass(frozen=True)
class Target:
    name: str          # "local" or "live"
    ws_url: str
    health_url: str
    tick_ms: int       # what the server was told, or the production tick
    seeded: bool       # fights replay the same way

    @property
    def is_live(self) -> bool:
        return self.name == "live"


def live_target() -> Target:
    return Target("live", f"wss://{LIVE_HOST}/ws", f"https://{LIVE_HOST}/healthz", 600, False)


def health(url: str, timeout: float = 5.0) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 - fixed http(s) URLs only
        return r.read().decode("utf-8", "replace").strip()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def find_repo() -> Path:
    raw = os.environ.get("SPIRESCAPE_REPO", "")
    if not raw:
        raise RuntimeError("Set SPIRESCAPE_REPO to a SpireScape checkout to run the local target.")
    repo = Path(raw).expanduser()
    if not (repo / "src" / "SpireScape.Server").is_dir():
        raise RuntimeError(f"SPIRESCAPE_REPO does not look like a SpireScape checkout: {repo}")
    return repo


def build(repo: Path) -> Path:
    """dotnet build the server in Release and return the dll. Run once per session, not per worker."""
    dotnet = shutil.which("dotnet") or "dotnet"
    subprocess.run([dotnet, "build", str(repo / "src" / "SpireScape.Server"), "-c", "Release", "--nologo",
                    "-v", "quiet"], check=True, capture_output=True, text=True)
    return server_dll(repo)


def server_dll(repo: Path) -> Path:
    hits = sorted(repo.glob("build/SpireScape.Server/bin/Release/*/SpireScape.Server.dll"))
    if not hits:
        hits = sorted(repo.glob("src/SpireScape.Server/bin/Release/*/SpireScape.Server.dll"))
    if not hits:
        raise RuntimeError("SpireScape.Server.dll not found after the build")
    return hits[-1]


class LocalServer:
    def __init__(self, repo: Path, *, tick_ms: int = 20, seed: int = 7, max_players: int = 30) -> None:
        self.repo = repo
        self.tick_ms = tick_ms
        self.seed = seed
        self.max_players = max_players
        self.ws_port = 0
        self.proc: subprocess.Popen | None = None
        self.log: collections.deque[str] = collections.deque(maxlen=400)
        self.errors: list[str] = []  # every "Handler error" / "Tick error" line, never trimmed

    @property
    def target(self) -> Target:
        base = f"127.0.0.1:{self.ws_port}"
        return Target("local", f"ws://{base}/ws", f"http://{base}/healthz", self.tick_ms, True)

    def start(self, timeout: float = 60.0) -> "LocalServer":
        dll = server_dll(self.repo)
        self.ws_port = free_port()
        args = [shutil.which("dotnet") or "dotnet", str(dll), "--port", "0", "--ws-port", str(self.ws_port),
                "--ws-host", "127.0.0.1", "--tick-ms", str(self.tick_ms), "--seed", str(self.seed),
                "--max-players", str(self.max_players), "--content", str(self.repo / "content")]
        self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     encoding="utf-8", errors="replace")
        threading.Thread(target=self._drain, daemon=True).start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("SpireScape server exited during startup:\n" + "\n".join(self.log))
            try:
                if health(self.target.health_url, 2).startswith("ok"):
                    return self
            except OSError:
                time.sleep(0.2)
        self.stop()
        raise RuntimeError("SpireScape server did not answer /healthz:\n" + "\n".join(self.log))

    def _drain(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.rstrip()
            self.log.append(line)
            if "Handler error" in line or "Tick error" in line:
                self.errors.append(line)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
