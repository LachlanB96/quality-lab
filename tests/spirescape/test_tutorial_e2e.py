"""The whole tutorial as a player plays it, checking each objective as it changes."""
import pytest

from qualitylab.spirescape import messages as m
from qualitylab.spirescape import play
from qualitylab.spirescape.messages import EntityKind, Skill

pytestmark = [pytest.mark.e2e, pytest.mark.area("tutorial"), pytest.mark.slow, pytest.mark.xdist_group("rats")]


def expect_objective(c, budget, words: str) -> None:
    c.wait_until(lambda s: words in s.objective, budget.ticks(10), f"objective containing {words!r}")


def test_full_tutorial_from_landfall_to_wardenhall(player, budget, record_property):
    c = player
    expect_objective(c, budget, "Old Bram")

    play.get_starter_kit(c, budget)
    expect_objective(c, budget, "Pluck feathers")

    play.gather(c, EntityKind.CREATURE, "chicken", "Pluck", "feathers", 15, budget)
    expect_objective(c, budget, "Chop a tree")

    play.gather(c, EntityKind.NODE, "tree", "Chop", "logs", 1, budget)
    expect_objective(c, budget, "arrow shafts")

    play.craft(c, "bench", "cut_shafts", "arrow_shafts", 15, budget)
    expect_objective(c, budget, "Mine a copper ore")

    play.gather(c, EntityKind.NODE, "copper_rock", "Mine", "copper_ore", 1, budget)
    play.gather(c, EntityKind.NODE, "tin_rock", "Mine", "tin_ore", 1, budget)
    expect_objective(c, budget, "Smelt")

    play.craft(c, "forge", "smelt_bronze", "bronze_bar", 1, budget)
    expect_objective(c, budget, "Smith bronze arrowheads")

    play.craft(c, "forge", "smith_arrowheads", "bronze_arrowheads", 15, budget)
    expect_objective(c, budget, "Fletch arrows")

    play.craft(c, "bench", "fletch_bronze_arrows", "bronze_arrows", 15, budget)
    expect_objective(c, budget, "Equip the shortbow")

    play.equip(c, "shortbow", budget)
    expect_objective(c, budget, "giant rat")

    end = play.fight(c, "rat", budget)
    assert end.won, "lost the rat fight: " + " | ".join(c.read(lambda s: s.notices[-8:]))
    assert c.count("bronze_arrows") < 15, "arrows are spent when fired"
    assert c.xp(Skill.RANGED) > 0, "D-047: Ranged XP from arrow damage"
    expect_objective(c, budget, "back to Old Bram")

    pile = c.wait_until(lambda s: next((e for e in s.entities.values() if e.kind == EntityKind.GROUND_ITEM
                                        and "Rat" in e.name), None), budget.ticks(10), "the rat's drops")
    c.send(m.Interact(entity_id=pile.id, action="Take"))
    c.wait_until(lambda s: "rat_bone" in s.inventory.slot_card_ids, budget.ticks(80), "taking the drops")

    play.talk(c, "old_bram", budget)
    expect_objective(c, budget, "Board the boat")

    c.send(m.Interact(entity_id=c.find(EntityKind.PORTAL, "boat").id, action="Board"))
    c.wait_until(lambda s: s.map and s.map.map_id == "wardenhall", budget.ticks(120), "arrival at Wardenhall")
    c.wait_until(lambda s: s.objective == "", budget.ticks(10), "the tutorial to finish")
    assert c.find(EntityKind.NPC, "old_bram") is not None, "Bram is waiting in Wardenhall"

    c.send(m.InventoryAction(slot=c.slot_of("rat_bone"), action="Bury"))
    c.wait_until(lambda s: s.skills.xp[Skill.PRAYER] > 0, budget.ticks(10), "D-031: Prayer XP for burying")
    record_property("metric", {"name": "tutorial_messages", "value": len(c.messages()), "unit": "messages"})
