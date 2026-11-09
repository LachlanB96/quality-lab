"""Send raw WebSocket upgrades and count the ones that are never answered: how BUG-008 was measured.

Standard library only, so it runs as a plain file on any box with python3, next to the server as well as
from outside. From here:        python upgrade_probe.py spirescape.lachlanb.com
On the box, past the proxy:     python3 upgrade_probe.py 127.0.0.1 --port 8080 --no-tls --host-header 127.0.0.1:8080
On the box, through the proxy:  python3 upgrade_probe.py 127.0.0.1 --sni spirescape.lachlanb.com
Exit 0 when every upgrade was answered 101, else 1.
"""
from __future__ import annotations

import argparse
import base64
import os
import socket
import ssl
import sys
import time

REQUEST = ("GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n"
           "Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: {key}\r\n\r\n")
ANSWERED = "HTTP/1.1 101 Switching Protocols"


def probe(host: str, port: int, *, path: str = "/ws", tls: bool = True, sni: str | None = None,
          host_header: str | None = None, timeout: float = 5.0) -> tuple[str, float]:
    """One upgrade on a fresh connection: the response's status line, "no answer" after `timeout` seconds of
    silence, or "closed" when the server hung up without a word, and how many seconds it took."""
    started = time.perf_counter()
    s = socket.create_connection((host, port), timeout=timeout)
    try:
        if tls:
            s = ssl.create_default_context().wrap_socket(s, server_hostname=sni or host)
        s.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")  # a fresh nonce each time, as a browser sends
        s.sendall(REQUEST.format(path=path, host=host_header or sni or host, key=key).encode("ascii"))
        try:
            data = s.recv(1024)
        except socket.timeout:
            return "no answer", time.perf_counter() - started
        line = data.split(b"\r\n", 1)[0].decode("ascii", "replace")
        return (line or "closed"), time.perf_counter() - started
    finally:
        s.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("host")
    ap.add_argument("--port", type=int, default=443)
    ap.add_argument("--path", default="/ws")
    ap.add_argument("--no-tls", action="store_true", help="plain TCP (the listener itself, not the proxy)")
    ap.add_argument("--sni", help="TLS server name and Host header when they differ from the address")
    ap.add_argument("--host-header", help="the Host header, when the server insists on one")
    ap.add_argument("--n", type=int, default=20, help="how many fresh connections (default 20)")
    ap.add_argument("--timeout", type=float, default=5.0, help="seconds of silence that count as no answer")
    a = ap.parse_args(argv)
    tally: dict[str, int] = {}
    for i in range(a.n):
        answer, took = probe(a.host, a.port, path=a.path, tls=not a.no_tls, sni=a.sni, host_header=a.host_header,
                             timeout=a.timeout)
        tally[answer] = tally.get(answer, 0) + 1
        print(f"{i + 1:3} {took:6.2f}s  {answer}", flush=True)
    print("---")
    for answer, n in sorted(tally.items(), key=lambda kv: -kv[1]):
        print(f"{n:3} of {a.n}  {answer}")
    return 0 if set(tally) <= {ANSWERED} else 1


if __name__ == "__main__":
    sys.exit(main())
