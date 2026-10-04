"""Logging in: names, protocol versions, duplicate hellos, capacity, and leaving."""
import uuid

import pytest

from qualitylab.spirescape import messages as m
from qualitylab.spirescape import server as srv
from qualitylab.spirescape.client import GameClient
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


def test_health_endpoint_counts_players(target, connect):
    connect()
    status = srv.health(target.health_url)
    assert status.startswith("ok ") and status.endswith(" online")
    assert int(status.split()[1]) >= 1
