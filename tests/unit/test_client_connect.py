"""GameClient's connect: an upgrade the server never answers fails as a named ConnectTimeout carrying /healthz's
word from the same moment, and open_client retries it once, only where the caller says that cause is known."""
import http.server
import socket
import threading
import time

import pytest
from websockets.sync.server import serve

from qualitylab.spirescape.client import UPGRADE_TIMEOUT_TEXT, ConnectTimeout, GameClient, open_client

pytestmark = [pytest.mark.unit, pytest.mark.area("client")]


class StallingServer:
    """A real WebSocket server whose first `stall` upgrades are answered too late for the client: from the
    client's side, no answer at all, which is BUG-008's shape."""

    def __init__(self, stall: int, delay: float = 1.5) -> None:
        self.stall, self.delay, self.requests = stall, delay, 0

    def process_request(self, connection, request):
        self.requests += 1
        if self.requests <= self.stall:
            time.sleep(self.delay)
        return None

    @staticmethod
    def handler(ws) -> None:
        try:
            for _ in ws:
                pass
        except Exception:  # noqa: BLE001 - the client closing is the normal end
            pass


@pytest.fixture
def stalling():
    servers = []

    def start(stall: int):
        srv = StallingServer(stall)
        server = serve(StallingServer.handler, "127.0.0.1", 0, process_request=srv.process_request)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return srv, f"ws://127.0.0.1:{server.socket.getsockname()[1]}/ws"

    yield start
    for s in servers:
        s.shutdown()


@pytest.fixture
def healthz():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"ok 0 online"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/healthz"
    httpd.shutdown()


def closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_an_unanswered_upgrade_is_a_named_timeout_with_the_servers_health(stalling, healthz):
    _, url = stalling(stall=1)
    with pytest.raises(ConnectTimeout) as info:
        GameClient(url, connect_timeout=0.3, health_url=healthz)
    e = info.value
    assert (e.phase, e.attempts) == ("upgrade", 1)
    assert 0.3 <= e.elapsed < 1.0
    assert str(e.__cause__) == UPGRADE_TIMEOUT_TEXT, "the phase is read from the library's wording"
    assert "TCP and TLS connected, then the WebSocket upgrade got no answer in 0." in str(e)
    assert "/healthz answered 'ok 0 online' in" in str(e)


def test_a_health_check_that_fails_is_reported_not_raised(stalling):
    _, url = stalling(stall=1)
    with pytest.raises(ConnectTimeout) as info:
        GameClient(url, connect_timeout=0.3, health_url=f"http://127.0.0.1:{closed_port()}/healthz")
    assert "/healthz did not answer" in str(info.value)


def test_without_a_health_url_there_is_no_health_clause(stalling):
    _, url = stalling(stall=1)
    with pytest.raises(ConnectTimeout) as info:
        GameClient(url, connect_timeout=0.3)
    assert info.value.health is None and "healthz" not in str(info.value)


def test_a_refused_connection_is_not_a_connect_timeout():
    # Windows retries a refused loopback SYN for about a second before it reports the refusal
    with pytest.raises(OSError) as info:
        GameClient(f"ws://127.0.0.1:{closed_port()}/ws", connect_timeout=5)
    assert not isinstance(info.value, ConnectTimeout)


def test_a_clean_connect_reports_how_long_it_took(stalling):
    _, url = stalling(stall=0)
    with GameClient(url, connect_timeout=1) as c:
        assert 0 < c.connected_in < 1


def test_open_client_retries_a_stalled_upgrade_once_when_told_to(stalling):
    srv, url = stalling(stall=1)
    seen: list[ConnectTimeout] = []
    c = open_client(url, connect_timeout=0.3, retry_upgrade_stall=True, on_stall=seen.append)
    try:
        assert srv.requests == 2
        assert [s.phase for s in seen] == ["upgrade"]
        assert c.connected_in < 0.3
    finally:
        c.close()


def test_open_client_does_not_retry_unless_told(stalling):
    srv, url = stalling(stall=1)
    with pytest.raises(ConnectTimeout) as info:
        open_client(url, connect_timeout=0.3)
    assert srv.requests == 1 and info.value.attempts == 1


def test_two_stalls_in_a_row_fail_with_both_counted(stalling):
    srv, url = stalling(stall=2)
    with pytest.raises(ConnectTimeout) as info:
        open_client(url, connect_timeout=0.3, retry_upgrade_stall=True)
    assert srv.requests == 2
    assert info.value.attempts == 2 and "2 attempts in a row" in str(info.value)
