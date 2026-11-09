"""The raw upgrade probe tells an answered upgrade from one the server never answers and one it drops."""
import socket
import threading

import pytest

from qualitylab.spirescape import upgrade_probe as up

pytestmark = [pytest.mark.unit, pytest.mark.area("client")]


@pytest.fixture
def raw_server():
    """A TCP server that reads the request and then does what `mode` says: answer 101, stay silent, or hang up."""
    sockets = []

    def start(mode: str) -> int:
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        sockets.append(srv)

        def serve():
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                with conn:
                    conn.settimeout(2)
                    try:
                        conn.recv(4096)
                    except OSError:
                        continue
                    if mode == "answer":
                        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\r\n")
                    elif mode == "silent":
                        try:
                            conn.recv(1)  # until the client gives up
                        except OSError:
                            pass

        threading.Thread(target=serve, daemon=True).start()
        return srv.getsockname()[1]

    yield start
    for s in sockets:
        s.close()


def test_an_answered_upgrade_reports_its_status_line(raw_server):
    port = raw_server("answer")
    answer, took = up.probe("127.0.0.1", port, tls=False, timeout=2)
    assert answer == up.ANSWERED and took < 1


def test_silence_is_no_answer_after_the_timeout(raw_server):
    port = raw_server("silent")
    answer, took = up.probe("127.0.0.1", port, tls=False, timeout=0.3)
    assert answer == "no answer" and 0.25 <= took < 1.5


def test_a_server_that_hangs_up_is_reported_as_closed(raw_server):
    port = raw_server("close")
    answer, _ = up.probe("127.0.0.1", port, tls=False, timeout=2)
    assert answer == "closed"


def test_main_tallies_and_exits_1_unless_every_upgrade_was_answered(raw_server, capsys):
    ok = raw_server("answer")
    assert up.main(["127.0.0.1", "--port", str(ok), "--no-tls", "--n", "3", "--timeout", "2"]) == 0
    assert "  3 of 3  HTTP/1.1 101 Switching Protocols" in capsys.readouterr().out
    silent = raw_server("silent")
    assert up.main(["127.0.0.1", "--port", str(silent), "--no-tls", "--n", "2", "--timeout", "0.2"]) == 1
    assert "  2 of 2  no answer" in capsys.readouterr().out
