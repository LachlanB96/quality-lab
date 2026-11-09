"""Logging in: names, protocol versions, duplicate hellos, capacity, and leaving."""
import time
import uuid

import pytest

from qualitylab.spirescape import messages as m
from qualitylab.spirescape import server as srv
from qualitylab.spirescape.client import ConnectTimeout, GameClient
from qualitylab.spirescape.messages import EntityKind

pytestmark = [pytest.mark.integration, pytest.mark.area("session")]

LANDFALL_SPAWN = (7, 16)


def test_new_player_spawns_at_landfall(player):
    me = player.me
    assert (me.x, me.y) == LANDFALL_SPAWN
    assert "Old Bram" in player.read(lambda s: s.objective)


def test_new_player_starts_with_an_empty_pack(player):
    assert player.used_slots() == 0


@pytest.mark.parametrize("sent, expected", [
    ("<b>x</b>", "bxb"),            # markup characters are stripped
    ("   ", "Adventurer"),          # nothing left: the default name
    ("a" * 40, "a" * 12),           # capped at 12 characters
    ("  pad  ", "pad"),             # trimmed
    ("tab\there", "tabhere"),        # control characters stripped
], ids=["markup", "blank", "long", "padded", "control"])
def test_names_are_sanitised(connect, sent, expected):
    c = connect(login=False)
    c.send(m.Hello(name=sent))
    c.wait_until(lambda s: s.player_id and s.player_id in s.entities, 30, "login")
    # on the live demo someone may already hold the name, so the server may add a number
    assert c.me.name.rstrip("0123456789") == expected


def test_a_taken_name_gets_a_number(connect):
    base = "dup" + uuid.uuid4().hex[:5]
    first = connect(base)
    second = connect(base)
    assert first.me.name == base
    assert second.me.name == base + "2"


def test_wrong_protocol_version_is_refused(connect):
    c = connect(login=False)
    c.send(m.Hello(name="old-client", version=0))
    c.wait_until(lambda s: any("Protocol mismatch" in n for n in s.notices), 15, "mismatch notice")
    assert c.closed.wait(10), "server kept the connection open"
    assert c.read(lambda s: s.player_id) == 0


def test_a_second_hello_is_ignored(player):
    before = len(player.messages(m.Welcome))
    player.send(m.Hello(name="again"))
    player.send(m.Chat(text="ping"))  # a later message proves the Hello was processed
    player.wait_until(lambda s: any(c.text == "ping" for c in s.chat), 15, "own chat echo")
    assert len(player.messages(m.Welcome)) == before


def test_intents_before_hello_are_ignored(connect):
    c = connect(login=False)
    c.send(m.MoveTo(x=9, y=16))
    c.send(m.Chat(text="before hello"))
    c.login("early" + uuid.uuid4().hex[:5])
    assert c.read(lambda s: [x for x in s.chat if x.text == "before hello"]) == []


@pytest.mark.xfail(reason="BUG-002: the duplicate-name number is added after the 12-character cap", strict=True)
def test_names_never_exceed_twelve_characters(connect):
    base = uuid.uuid4().hex[:12]
    connect(base)
    second = connect(base)
    assert len(second.me.name) <= 12, second.me.name


def test_leaving_despawns_the_player_for_others(connect):
    watcher = connect()
    leaver = connect()
    leaver_id = leaver.read(lambda s: s.player_id)
    watcher.wait_until(lambda s: leaver_id in s.entities, 30, "watcher to see the new player")
    leaver.close()
    watcher.wait_until(lambda s: leaver_id not in s.entities, 30, "leaver to despawn")


@pytest.mark.local_only
def test_a_full_server_turns_new_players_away():
    """A dedicated server with room for two: the third player gets a notice and is disconnected."""
    server = srv.LocalServer(srv.find_repo(), tick_ms=20, max_players=2).start()
    try:
        url = server.target.ws_url
        with GameClient(url) as a, GameClient(url) as b, GameClient(url) as c:
            a.login("cap-a")
            b.login("cap-b")
            c.send(m.Hello(name="cap-c"))
            c.wait_until(lambda s: any("server is full" in n for n in s.notices), 15, "full notice")
            assert c.closed.wait(10)
            assert srv.health(server.target.health_url) == "ok 2 online"
    finally:
        server.stop()


@pytest.mark.xfail_live("BUG-009: after a client's goodbye the live demo answers the close frame but leaves the "
                        "connection open, or drops the connection without a close frame")
def test_a_client_that_says_goodbye_is_let_go_properly(target):
    """RFC 6455 7.1.1, the client-initiated close: the server answers the close frame and then closes the
    connection, at once. Locally both happen. The live demo does one or the other (BUG-009), so a patient
    client waits out its close timeout, or never sees the server's close frame."""
    c = GameClient(target.ws_url, close_timeout=10, health_url=target.health_url).login("qa-goodbye")
    t0 = time.monotonic()
    c.close()
    elapsed = time.monotonic() - t0
    assert c.close_code == 1000, f"the connection ended with close code {c.close_code}: no close frame from the server"
    assert elapsed < 2, f"the connection lingered {elapsed:.1f} s after the close handshake"


def test_health_endpoint_counts_players(target, connect):
    connect()
    status = srv.health(target.health_url)
    assert status.startswith("ok ") and status.endswith(" online")
    assert int(status.split()[1]) >= 1


def test_every_upgrade_is_answered(target, record_property):
    """Ten fresh connections, 5 s each. An answered upgrade takes well under a second from anywhere, and one
    the server drops never answers at all (BUG-008 on the live demo, measured out to 90 s), so 5 s tells them
    apart. Records how many stalled and the slowest answer. On the live target a stall is the known bug: an
    xfail with its id, so the run stays green and the bug stays on the page. Anywhere else it's a failure."""
    answered_ms, stalls, last = [], 0, None
    for _ in range(10):
        try:
            with GameClient(target.ws_url, connect_timeout=5, health_url=target.health_url) as c:
                answered_ms.append(c.connected_in * 1000)
        except ConnectTimeout as e:
            if e.phase != "upgrade":
                raise
            stalls, last = stalls + 1, e
    record_property("metric", {"name": "handshake_stalls_of_10", "value": stalls, "unit": "stalls"})
    if answered_ms:
        record_property("metric", {"name": "upgrade_max_ms", "value": round(max(answered_ms), 1), "unit": "ms"})
    if stalls and target.is_live:
        pytest.xfail(f"BUG-008: {stalls} of 10 WebSocket upgrades never answered; the last: {last}")
    assert stalls == 0, f"{stalls} of 10 WebSocket upgrades never answered; the last: {last}"
    assert max(answered_ms) < (3000 if target.is_live else 1000), f"slowest answered upgrade {max(answered_ms):.0f} ms"
