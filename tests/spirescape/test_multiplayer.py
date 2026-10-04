"""Two players in one world: they see each other, hear each other, and can take what the other drops."""
import pytest

from qualitylab.spirescape import messages as m
from qualitylab.spirescape import play
from qualitylab.spirescape.messages import EntityKind

pytestmark = [pytest.mark.integration, pytest.mark.area("multiplayer")]


def test_players_see_each_other_move(connect, budget):
    a, b = connect(), connect()
    a_id = a.read(lambda s: s.player_id)
    b.wait_until(lambda s: a_id in s.entities, budget.ticks(10), "b to see a")
    a.send(m.MoveTo(x=11, y=16))
    b.wait_until(lambda s: (s.entities[a_id].x, s.entities[a_id].y) == (11, 16), budget.ticks(20), "a's move")


def test_chat_reaches_everyone_in_the_region(connect, budget):
    a, b = connect(), connect()
    a.send(m.Chat(text="hello from a"))
    line = b.wait_until(lambda s: next((c for c in s.chat if c.text == "hello from a"), None), budget.ticks(10), "chat")
    assert line.from_ == a.me.name


@pytest.mark.parametrize("sent, heard", [
    ("  trimmed  ", "trimmed"),
    ("bell\x07 and\x1b escape", "bell and escape"),
    ("x" * 500, "x" * 200),
], ids=["trim", "control-chars", "length-cap"])
def test_chat_is_cleaned_before_it_is_broadcast(connect, budget, sent, heard):
    a, b = connect(), connect()
    a.send(m.Chat(text=sent))
    b.wait_until(lambda s: any(c.text == heard for c in s.chat), budget.ticks(10), f"cleaned chat {heard[:20]!r}")


def test_blank_chat_is_dropped(connect, budget):
    a, b = connect(), connect()
    a.send(m.Chat(text="   "))
    a.send(m.Chat(text="real line"))
    b.wait_until(lambda s: any(c.text == "real line" for c in s.chat), budget.ticks(10), "the real line")
    assert all(c.text.strip() for c in b.read(lambda s: s.chat))


def test_dropped_items_are_public(connect, budget):
    """D-119: a drop is instantly a pile anyone can take."""
    a, b = connect(), connect()
    play.get_starter_kit(a, budget)
    me = a.me
    since = b.mark()
    a.send(m.InventoryAction(slot=a.slot_of("bread"), action="Drop"))
    a.wait_until(lambda s: play._count(s, "bread") == 1, budget.ticks(10), "a's pack to lose one bread")
    # A drop on a tile that already has a pile (the live world keeps them 3 minutes) merges into it, so
    # watch for the pile spawn *or update* under a's feet rather than for a brand-new entity.
    spawn = b.wait_for(m.EntitySpawn, budget.ticks(10), "b to see the bread land under a", after=since,
                       where=lambda msg: msg.entity.kind == EntityKind.GROUND_ITEM
                       and (msg.entity.x, msg.entity.y) == (me.x, me.y))
    assert "Bread" in spawn.entity.name
    b.send(m.Interact(entity_id=spawn.entity.id, action="Take"))
    b.wait_until(lambda s: play._count(s, "bread") >= 1, budget.ticks(80), "b to take it")
