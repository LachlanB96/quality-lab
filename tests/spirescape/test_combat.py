"""Combat: your pack is your deck, both sides commit at once, and the server checks every commit."""
import pytest

from qualitylab.spirescape import messages as m
from qualitylab.spirescape import play
from qualitylab.spirescape.messages import CombatEventType, EntityKind

# Landfall has two rats. On the live demo, tests that fight run on one worker (xdist_group, with
# --dist loadgroup) so they don't take each other's rats.
pytestmark = [pytest.mark.integration, pytest.mark.area("combat"), pytest.mark.xdist_group("rats")]


def start_fight(c, budget, creature="rat") -> m.CombatSnapshot:
    return play.start_fight(c, creature, budget)


@pytest.fixture
def armed(player, budget):
    """A player with Old Bram's kit: six cards in the deck (pickaxe, axe, hammer, bow, two bread)."""
    play.get_starter_kit(player, budget)
    return player


def test_attacking_starts_combat_visible_to_the_world(armed, budget):
    """D-028: both the player and the rat show as in combat on the world map."""
    start_fight(armed, budget)
    me = armed.me
    enemy_id = armed.read(lambda s: next(r.msg.enemy_entity_id for r in s.log if isinstance(r.msg, m.CombatStart)))
    armed.wait_until(lambda s: s.entities[me.id].in_combat and s.entities[enemy_id].in_combat,
                     budget.ticks(5), "in-combat flags")


def test_opening_hand_is_five_cards_with_two_energy(armed, budget):
    """D-022 hand of 5, D-024 energy held at 2. Six cards in the deck, so one is left to draw."""
    snap = start_fight(armed, budget)
    assert len(snap.player.hand) == 5
    assert (snap.player.energy, snap.player.max_energy) == (2, 2)
    assert snap.player.draw_count == 1


def test_your_draw_pile_is_browsable_but_not_its_order(armed, budget):
    """D-153: you can see what's in your draw pile, sorted, so its order gives nothing away."""
    snap = start_fight(armed, budget)
    names = [armed.read(lambda s: s.cards[i].name) for i in snap.player.draw_pile_def_ids]
    assert names == sorted(names)
    assert len(snap.player.draw_pile_def_ids) == snap.player.draw_count


def test_the_enemy_hand_is_hidden(armed, budget):
    """D-103: hidden intents. Only counts are sent for the enemy, never its cards."""
    snap = start_fight(armed, budget)
    assert snap.enemy.hand == [] and snap.enemy.draw_pile_def_ids == [] and snap.enemy.discard_def_ids == []
    assert snap.enemy.hand_count > 0


def test_a_commit_over_your_energy_is_rejected(armed, budget):
    """Two cards costing 2 each (pickaxe, axe) need 4 energy; you have 2."""
    snap = start_fight(armed, budget)
    costly = [h.instance_id for h in snap.player.hand if armed.read(lambda s: s.cards[h.def_id].cost) == 2][:2]
    if len(costly) < 2:
        pytest.skip("the opening hand didn't hold two 2-cost cards")
    seen = armed.read(lambda s: s.combat_updates)
    armed.send(m.CombatCommit(card_instance_ids=costly))
    armed.wait_until(lambda s: s.combat_updates > seen, budget.ticks(5), "a combat update")
    events = armed.read(lambda s: s.last_combat_events)
    assert [e.type for e in events] == [CombatEventType.INFO], "the turn should not resolve"
    assert armed.read(lambda s: s.combat.turn) == snap.turn


def test_a_card_you_dont_hold_is_rejected(armed, budget):
    snap = start_fight(armed, budget)
    seen = armed.read(lambda s: s.combat_updates)
    armed.send(m.CombatCommit(card_instance_ids=[999_999]))
    armed.wait_until(lambda s: s.combat_updates > seen, budget.ticks(5), "a combat update")
    assert [e.type for e in armed.read(lambda s: s.last_combat_events)] == [CombatEventType.INFO]
    assert armed.read(lambda s: s.combat.turn) == snap.turn


def test_a_turn_resolves_and_advances(armed, budget):
    snap = start_fight(armed, budget)
    seen = armed.read(lambda s: s.combat_updates)
    armed.send(m.CombatCommit(card_instance_ids=[]))
    armed.wait_until(lambda s: s.combat_updates > seen, budget.ticks(5), "a combat update")
    after = armed.read(lambda s: s.combat)
    assert after.turn == snap.turn + 1 or after.over
    kinds = {e.type for e in armed.read(lambda s: s.last_combat_events)}
    assert CombatEventType.TURN_START in kinds or after.over


def test_you_cant_move_while_fighting(armed, budget):
    start_fight(armed, budget)
    me = armed.me
    armed.send(m.MoveTo(x=me.x + 3, y=me.y))
    armed.send(m.Chat(text="after move"))
    armed.wait_until(lambda s: any(c.text == "after move" for c in s.chat), budget.ticks(10), "marker")
    now = armed.me
    assert (now.x, now.y) == (me.x, me.y)


def test_someone_elses_rat_cant_be_attacked(connect, budget):
    """D-107: single combat. A rat already fighting is off limits to a second player."""
    a, b = connect(), connect()
    play.get_starter_kit(a, budget)
    start_fight(a, budget)
    rat_id = a.read(lambda s: next(r.msg.enemy_entity_id for r in s.log if isinstance(r.msg, m.CombatStart)))
    b.wait_until(lambda s: rat_id in s.entities and s.entities[rat_id].in_combat, budget.ticks(10), "b sees the fight")
    b.send(m.Interact(entity_id=rat_id, action="Attack"))
    b.wait_until(lambda s: "Someone else is already fighting that." in s.notices, budget.ticks(120), "refusal")


def test_leaving_mid_fight_frees_the_creature(connect, budget):
    a, b = connect(), connect()
    play.get_starter_kit(a, budget)
    start_fight(a, budget)
    rat_id = a.read(lambda s: next(r.msg.enemy_entity_id for r in s.log if isinstance(r.msg, m.CombatStart)))
    b.wait_until(lambda s: rat_id in s.entities and s.entities[rat_id].in_combat, budget.ticks(10), "fight visible")
    a.close()
    b.wait_until(lambda s: rat_id in s.entities and not s.entities[rat_id].in_combat, budget.ticks(10), "rat freed")


@pytest.mark.slow
def test_losing_keeps_your_three_most_valuable_items(armed, budget):
    """D-042/D-122: stand still and lose. Keep the bow (60), pickaxe (40) and axe (40); drop the rest."""
    start_fight(armed, budget)
    for _ in range(60):
        if armed.read(lambda s: s.combat_end is not None):
            break
        seen = armed.read(lambda s: s.combat_updates)
        armed.send(m.CombatCommit(card_instance_ids=[]))
        armed.wait_until(lambda s: s.combat_updates > seen or s.combat_end, budget.ticks(10), "a turn")
    end = armed.wait_until(lambda s: s.combat_end, budget.ticks(10), "the fight to end")
    assert end.won is False
    armed.wait_until(lambda s: any("you are dead" in n for n in s.notices), budget.ticks(10), "death notice")
    armed.wait_until(lambda s: sum(1 for c in s.inventory.slot_card_ids if c) == 3, budget.ticks(10), "respawn")
    assert {cid for cid in armed.read(lambda s: s.inventory.slot_card_ids) if cid} == \
        {"shortbow", "bronze_pickaxe", "bronze_axe"}
    me = armed.me
    assert (me.x, me.y) == (7, 16), "respawn at Landfall's spawn during the tutorial"
    pile = armed.wait_until(lambda s: next((e for e in s.entities.values() if e.kind == EntityKind.GROUND_ITEM), None),
                            budget.ticks(10), "the death pile")
    assert "Take" in pile.actions
