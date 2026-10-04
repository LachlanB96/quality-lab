"""Moving, examining and interacting in the world: the server walks you, and refuses what isn't there."""
import time

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from qualitylab.spirescape import messages as m
from qualitylab.spirescape.messages import EntityKind

pytestmark = [pytest.mark.integration, pytest.mark.area("world")]

WATER, WALL = 2, 3


def walkable(mp: m.MapData, x: int, y: int) -> bool:
    return 0 <= x < mp.width and 0 <= y < mp.height and mp.tiles[y * mp.width + x] not in (WATER, WALL)


def own_moves(player, since: int) -> list[tuple[int, int]]:
    """The player's positions from every EntityMoves after log index `since`."""
    def collect(s):
        out = []
        for r in s.log[since:]:
            if isinstance(r.msg, m.EntityMoves) and s.player_id in r.msg.ids:
                i = r.msg.ids.index(s.player_id)
                out.append((r.msg.xs[i], r.msg.ys[i]))
        return out
    return player.read(collect)


def settle(player, budget, ticks=60):
    """Wait until the player hasn't moved for six ticks (a walk that never starts settles too)."""
    sent = time.monotonic()

    def still(s):
        mine = [r.at for r in s.log if isinstance(r.msg, m.EntityMoves) and s.player_id in r.msg.ids]
        last = max([sent] + [t for t in mine if t >= sent])
        return (time.monotonic() - last) * 1000 > budget.tick_ms * 6 + 300
    player.wait_until(still, budget.ticks(ticks), "the player to stop walking")


def test_move_to_walks_the_player_there(player, budget):
    player.send(m.MoveTo(x=12, y=16))
    player.wait_until(lambda s: (s.entities[s.player_id].x, s.entities[s.player_id].y) == (12, 16),
                      budget.ticks(20), "arrival at (12,16)")


def test_players_run_two_tiles_per_tick(player, budget):
    """Rules.PlayerTilesPerTick = 2: each EntityMoves update moves the player up to two tiles."""
    start = player.mark()
    player.send(m.MoveTo(x=15, y=16))
    player.wait_until(lambda s: s.entities[s.player_id].x == 15, budget.ticks(20), "arrival")
    path = [(7, 16)] + own_moves(player, start)
    jumps = [max(abs(b[0] - a[0]), abs(b[1] - a[1])) for a, b in zip(path, path[1:])]
    assert jumps and max(jumps) == 2 and all(1 <= j <= 2 for j in jumps), path


def test_out_of_bounds_move_is_ignored(player, budget):
    start = player.mark()
    player.send(m.MoveTo(x=-5, y=999))
    player.send(m.Chat(text="marker"))
    player.wait_until(lambda s: any(c.text == "marker" for c in s.chat), budget.ticks(10), "marker")
    me = player.me
    assert (me.x, me.y) == (7, 16)
    assert own_moves(player, start) == []


def test_clicking_water_next_to_the_shore_walks_to_the_shore(player, budget):
    """(1,16) is water but (2,16) is beside it, so the server paths to the neighbour instead."""
    player.send(m.MoveTo(x=1, y=16))
    player.wait_until(lambda s: (s.entities[s.player_id].x, s.entities[s.player_id].y) == (2, 16),
                      budget.ticks(20), "arrival at the shore")


def test_clicking_deep_water_is_refused(player, budget):
    """(0,16) has no walkable neighbour, so there's no path at all."""
    start = player.mark()
    player.send(m.MoveTo(x=0, y=16))
    player.wait_until(lambda s: "You can't reach that." in s.notices, budget.ticks(5), "refusal")
    assert own_moves(player, start) == []


@pytest.mark.property
@pytest.mark.local_only
@settings(max_examples=40)
@given(data=st.data())
def test_wherever_you_click_you_end_up_somewhere_walkable(connect, budget, data):
    """Property: after any MoveTo inside the map, the player stands on a walkable tile."""
    with connect() as c:
        mp = c.read(lambda s: s.map)
        x = data.draw(st.integers(min_value=0, max_value=mp.width - 1), label="x")
        y = data.draw(st.integers(min_value=0, max_value=mp.height - 1), label="y")
        c.send(m.MoveTo(x=x, y=y))
        settle(c, budget)
        me = c.me
        assert walkable(mp, me.x, me.y), (x, y, me.x, me.y)


def test_examine_an_npc_describes_it(player, budget):
    bram = player.find(EntityKind.NPC, "old_bram")
    seen = len(player.read(lambda s: s.notices))
    player.send(m.Interact(entity_id=bram.id, action="Examine"))
    text = player.wait_until(lambda s: s.notices[seen:] and s.notices[-1], budget.ticks(5), "examine text")
    assert "old man" in text


def test_interacting_with_something_that_isnt_there(player, budget):
    player.send(m.Interact(entity_id=987654, action="Talk-to"))
    player.wait_until(lambda s: "That isn't there any more." in s.notices, budget.ticks(5), "refusal")


def test_an_action_the_entity_doesnt_offer_is_ignored(player, budget):
    """Old Bram can't be attacked: the server ignores the request rather than starting a fight."""
    bram = player.find(EntityKind.NPC, "old_bram")
    player.send(m.Interact(entity_id=bram.id, action="Attack"))
    player.send(m.Chat(text="after attack"))
    player.wait_until(lambda s: any(c.text == "after attack" for c in s.chat), budget.ticks(10), "marker")
    assert player.read(lambda s: s.combat) is None


def test_the_boat_waits_until_bram_has_seen_you_after_the_rat(player, budget):
    boat = player.find(EntityKind.PORTAL, "boat")
    player.send(m.Interact(entity_id=boat.id, action="Board"))
    player.wait_until(lambda s: any("Bram" in n for n in s.notices), budget.ticks(80), "boat refusal")
    assert player.read(lambda s: s.map.map_id) == "landfall"


def test_npcs_wander_on_their_own(player, budget):
    chicken = player.find(EntityKind.CREATURE, "chicken")
    start = (chicken.x, chicken.y)
    player.wait_until(lambda s: (s.entities[chicken.id].x, s.entities[chicken.id].y) != start,
                      budget.ticks(400), "a chicken to wander")
