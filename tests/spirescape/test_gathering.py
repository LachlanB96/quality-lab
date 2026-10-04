"""Gathering: short world verbs that need the right tool, give XP, and deplete nodes that come back."""
import pytest

from qualitylab.spirescape import messages as m
from qualitylab.spirescape import play
from qualitylab.spirescape.messages import EntityKind, Skill

pytestmark = [pytest.mark.integration, pytest.mark.area("gathering")]


def test_plucking_a_chicken_gives_feathers_and_leaves_it_alive(player, budget):
    """D-075: plucking is non-lethal. Fifteen feathers per pluck, and the chicken stays."""
    chicken = player.nearest(EntityKind.CREATURE, "chicken", lambda e: not e.in_combat)
    play.gather(player, EntityKind.CREATURE, "chicken", "Pluck", "feathers", 15, budget)
    assert player.count("feathers") == 15
    assert player.read(lambda s: chicken.id in s.entities), "the chicken should still be there"
    assert any("unharmed" in n for n in player.read(lambda s: s.notices))


@pytest.mark.parametrize("kind, def_id, verb, text, ticks", [
    (EntityKind.CREATURE, "chicken", "Pluck", "Plucking...", 4),
    (EntityKind.NODE, "copper_rock", "Mine", "Mining...", 5),
    pytest.param(EntityKind.NODE, "tree", "Chop", "Chopping...", 5,
                 marks=pytest.mark.xfail(reason="BUG-003: the status line reads 'Choping...'", strict=True)),
], ids=["pluck", "mine", "chop"])
def test_gathering_shows_a_progress_status(player, budget, kind, def_id, verb, text, ticks):
    play.get_starter_kit(player, budget)
    c = player
    c.wait_until(lambda s: any(e.def_id == def_id and not e.depleted for e in s.entities.values()),
                 budget.ticks(60), f"an available {def_id}")
    c.send(m.Interact(entity_id=c.nearest(kind, def_id, lambda e: not e.depleted).id, action=verb))
    status = c.wait_until(lambda s: s.action, budget.ticks(60), "an action status")
    assert (status.text, status.ticks) == (text, ticks)


def test_chopping_without_an_axe_is_refused(player, budget):
    tree = player.nearest(EntityKind.NODE, "tree")
    player.send(m.Interact(entity_id=tree.id, action="Chop"))
    player.wait_until(lambda s: "You need a Bronze Axe to chop that." in s.notices, budget.ticks(60), "refusal")
    assert player.count("logs") == 0


def test_chopping_gives_logs_and_woodcutting_xp(player, budget):
    play.get_starter_kit(player, budget)
    play.gather(player, EntityKind.NODE, "tree", "Chop", "logs", 1, budget)
    player.wait_until(lambda s: s.skills.xp[Skill.WOODCUTTING] >= 25, budget.ticks(5), "woodcutting xp")


def test_tools_are_not_used_up(player, budget):
    """D-013: a tool is carried, never consumed."""
    play.get_starter_kit(player, budget)
    play.gather(player, EntityKind.NODE, "copper_rock", "Mine", "copper_ore", 1, budget)
    assert player.count("bronze_pickaxe") == 1


def test_a_copper_rock_depletes_after_one_ore(player, budget):
    """D-106: copper rocks have a 100% deplete chance, so one ore empties the rock."""
    play.get_starter_kit(player, budget)
    play.gather(player, EntityKind.NODE, "copper_rock", "Mine", "copper_ore", 1, budget)
    depleted = player.wait_until(
        lambda s: next((e for e in s.entities.values() if e.def_id == "copper_rock" and e.depleted), None),
        budget.ticks(5), "a depleted copper rock")
    assert depleted.actions == ["Examine"], "a depleted rock only offers Examine"


@pytest.mark.local_only
def test_a_depleted_rock_comes_back(player, budget):
    """Respawn is 5 ticks for copper: about a tenth of a second on the fast local tick."""
    play.get_starter_kit(player, budget)
    play.gather(player, EntityKind.NODE, "copper_rock", "Mine", "copper_ore", 1, budget)
    rock = player.wait_until(
        lambda s: next((e for e in s.entities.values() if e.def_id == "copper_rock" and e.depleted), None),
        budget.ticks(5), "a depleted copper rock")
    player.wait_until(lambda s: not s.entities[rock.id].depleted, budget.ticks(20), "the rock to respawn")


def test_walking_away_cancels_gathering(player, budget):
    play.get_starter_kit(player, budget)
    player.send(m.Interact(entity_id=player.nearest(EntityKind.CREATURE, "chicken").id, action="Pluck"))
    player.wait_until(lambda s: s.action, budget.ticks(60), "plucking to start")
    player.send(m.MoveTo(x=7, y=16))
    player.wait_until(lambda s: s.action is None, budget.ticks(5), "the action to cancel")
    assert player.count("feathers") == 0
