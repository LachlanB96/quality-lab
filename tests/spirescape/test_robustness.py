"""Hostile input. The rule: a bad client may lose its own connection, never the server or anyone else's game.

All local_only: these deliberately send garbage, which isn't fair to the public demo.
"""
import struct
import time

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from qualitylab.spirescape import messages as m
from qualitylab.spirescape import server as srv
from qualitylab.spirescape.client import GameClient

pytestmark = [pytest.mark.robustness, pytest.mark.area("protocol"), pytest.mark.local_only]


def frame(type_id: int, body: bytes) -> bytes:
    return struct.pack("<i", len(body) + 1) + bytes([type_id]) + body


def assert_world_unharmed(target, bystander: GameClient, budget) -> None:
    assert srv.health(target.health_url).startswith("ok"), "server stopped answering"
    bystander.send(m.Chat(text="still here"))
    bystander.wait_until(lambda s: any(c.text == "still here" for c in s.chat), budget.ticks(20),
                         "the bystander's game to keep working")


@pytest.mark.parametrize("payload, why", [
    (frame(200, b""), "unknown message type"),
    (frame(m.MoveTo.type_id, b"\x01\x00"), "body cut short"),
    (frame(m.Chat.type_id, b"\xff\xff\xff\xff\x0f"), "string length of 4 GB"),
    (frame(m.CombatCommit.type_id, struct.pack("<i", -1)), "negative list count"),
], ids=["unknown-type", "truncated", "huge-string", "negative-count"])
def test_a_malformed_frame_costs_only_the_sender_their_connection(connect, target, budget, payload, why):
    bystander = connect()
    attacker = connect()
    attacker.send_raw(payload)
    assert attacker.closed.wait(10), f"server kept a connection that sent: {why}"
    assert_world_unharmed(target, bystander, budget)


def test_an_oversized_frame_is_cut_off(connect, target, budget):
    bystander = connect()
    attacker = connect()
    attacker.send_raw(frame(m.Chat.type_id, b"\x00" * (m.MAX_FRAME + 10)))
    assert attacker.closed.wait(10)
    assert_world_unharmed(target, bystander, budget)


def test_a_tiny_frame_is_ignored_not_fatal(connect, budget):
    """Under five bytes can't hold a length and a type; the server skips it and keeps the connection."""
    c = connect()
    c.send_raw(b"\x01\x00")
    c.send(m.Chat(text="after tiny"))
    c.wait_until(lambda s: any(x.text == "after tiny" for x in s.chat), budget.ticks(10), "the next message")


def test_a_client_that_says_goodbye_is_let_go_promptly(target):
    """Client-initiated close: the server answers the close frame and the connection is gone at once."""
    c = GameClient(target.ws_url, close_timeout=10).login("qa-goodbye")
    t0 = time.monotonic()
    c.close()
    assert time.monotonic() - t0 < 2


@pytest.mark.xfail(reason="BUG-006: when the server kicks a client, it never finishes the close handshake",
                   strict=True)
def test_a_kicked_client_is_fully_disconnected(target, record_property):
    """RFC 6455 7.1.1: after the closing handshake the server SHOULD close TCP at once. When the server
    starts the close (here, after a bad frame) its read loop has already exited, so nothing reads the
    client's reply: a patient client (10 s close timeout) waits the whole timeout."""
    c = GameClient(target.ws_url, close_timeout=10).login("qa-kicked")
    t0 = time.monotonic()
    c.send_raw(frame(200, b""))
    c.closed.wait(15)
    elapsed = time.monotonic() - t0
    record_property("metric", {"name": "kick_close_s", "value": round(elapsed, 2), "unit": "s"})
    assert elapsed < 2, f"the connection lingered {elapsed:.1f} s after the server's close frame"


@pytest.mark.xfail(reason="BUG-004: over WebSocket the server never checks the length prefix", strict=True)
def test_a_lying_length_prefix_is_rejected(connect, budget):
    c = connect()
    good = m.encode(m.Chat(text="liar"))
    c.send_raw(struct.pack("<i", 9999) + good[4:])
    assert c.closed.wait(5), "a frame whose length prefix doesn't match its size was accepted"


@pytest.mark.property
@settings(max_examples=30)
@given(junk=st.binary(min_size=5, max_size=300))
def test_random_bytes_never_hurt_the_server(connect, target, budget, junk):
    """Property: whatever bytes arrive, /healthz still answers and a bystander still plays."""
    with connect() as attacker:
        attacker.send_raw(junk)
    with connect() as bystander:
        assert_world_unharmed(target, bystander, budget)


@pytest.mark.property
@settings(max_examples=30)
@given(msg=st.one_of(
    st.builds(m.MoveTo, x=st.integers(-2**31, 2**31 - 1), y=st.integers(-2**31, 2**31 - 1)),
    st.builds(m.Interact, entity_id=st.integers(-2**31, 2**31 - 1), action=st.text(max_size=20)),
    st.builds(m.DialogueChoice, index=st.integers(-2**31, 2**31 - 1)),
    st.builds(m.Craft, recipe_id=st.text(max_size=20), count=st.integers(-2**31, 2**31 - 1)),
    st.builds(m.InventoryAction, slot=st.integers(-2**31, 2**31 - 1), action=st.text(max_size=12)),
    st.builds(m.Unequip, slot=st.integers(0, 255)),
    st.builds(m.CombatCommit, card_instance_ids=st.lists(st.integers(-2**31, 2**31 - 1), max_size=8)),
))
def test_well_formed_nonsense_is_handled(connect, target, budget, msg):
    """Property: valid frames with absurd values (slot -7, count 2^31) never break the player's session."""
    with connect() as c:
        c.send(msg)
        c.send(m.Chat(text="survived"))
        c.wait_until(lambda s: any(x.text == "survived" for x in s.chat), budget.ticks(20), "the session to survive")


@pytest.mark.xfail(reason="BUG-005: no rate limit, so one client can flood everyone's chat", strict=True)
def test_chat_is_rate_limited(connect, budget):
    spammer, victim = connect(), connect()
    for i in range(300):
        spammer.send(m.Chat(text=f"spam {i}"))
    spammer.send(m.Chat(text="spam end"))
    victim.wait_until(lambda s: any(c.text == "spam end" for c in s.chat) or spammer.closed.is_set(),
                      budget.ticks(200), "the flood to arrive")
    delivered = sum(1 for c in victim.read(lambda s: s.chat) if c.text.startswith("spam"))
    assert delivered < 50, f"{delivered} of 301 spam lines reached another player"
