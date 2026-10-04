"""Crafting: D-072, level plus materials is the only gate, and the menu says exactly what's missing."""
import pytest

from qualitylab.spirescape import messages as m
from qualitylab.spirescape import play
from qualitylab.spirescape.messages import EntityKind

pytestmark = [pytest.mark.integration, pytest.mark.area("crafting")]


@pytest.mark.parametrize("station, recipes", [
    ("bench", {"cut_shafts", "craft_shortbow", "fletch_bronze_arrows"}),
    ("forge", {"smelt_bronze", "smith_arrowheads", "smith_dagger"}),
])
def test_each_station_offers_its_recipes(player, budget, station, recipes):
    menu = play.open_station(player, station, budget)
    assert set(menu.recipe_ids) == recipes


@pytest.mark.parametrize("station, recipe, reason", [
    ("bench", "cut_shafts", "Needs Logs"),
    ("bench", "fletch_bronze_arrows", "Needs 15 Arrow Shafts"),
    ("forge", "smelt_bronze", "Needs Copper Ore"),
    ("forge", "smith_arrowheads", "Needs a Hammer"),
])
def test_an_empty_handed_player_is_told_what_is_missing(player, budget, station, recipe, reason):
    menu = play.open_station(player, station, budget)
    i = menu.recipe_ids.index(recipe)
    assert menu.can_make[i] is False
    assert menu.reason[i] == reason


def test_menu_reasons_line_up_with_recipes(player, budget):
    """Three parallel arrays: one entry per recipe in each, and a reason only when it can't be made."""
    menu = play.open_station(player, "bench", budget)
    assert len(menu.recipe_ids) == len(menu.can_make) == len(menu.reason)
    for can, why in zip(menu.can_make, menu.reason):
        assert (why == "") == can


def test_crafting_away_from_the_station_is_refused(player, budget):
    """A Craft for a forge recipe with no forge nearby gets a warning, not an item."""
    player.send(m.Craft(recipe_id="smelt_bronze", count=1))
    player.wait_until(lambda s: "You need to be at a forge for that." in s.notices, budget.ticks(5), "refusal")


def test_an_unknown_recipe_is_ignored(player, budget):
    player.send(m.Craft(recipe_id="mithril_everything", count=99))
    player.send(m.Chat(text="after craft"))
    player.wait_until(lambda s: any(c.text == "after craft" for c in s.chat), budget.ticks(10), "marker")
    assert player.used_slots() == 0


def test_one_log_makes_fifteen_shafts(player, budget):
    play.get_starter_kit(player, budget)
    play.gather(player, EntityKind.NODE, "tree", "Chop", "logs", 1, budget)
    play.craft(player, "bench", "cut_shafts", "arrow_shafts", 15, budget)
    assert player.count("arrow_shafts") == 15


def test_bronze_needs_both_ores(player, budget):
    play.get_starter_kit(player, budget)
    play.gather(player, EntityKind.NODE, "copper_rock", "Mine", "copper_ore", 1, budget)
    menu = play.open_station(player, "forge", budget)
    i = menu.recipe_ids.index("smelt_bronze")
    assert (menu.can_make[i], menu.reason[i]) == (False, "Needs Tin Ore")


@pytest.mark.slow
def test_arrows_take_three_inputs_from_three_places(player, budget):
    """D-076: shafts, heads and feathers converge at the bench. All 45 inputs are spent for 15 arrows."""
    play.arrow_chain(player, budget)
    assert player.count("bronze_arrows") == 15
    for spent in ("arrow_shafts", "bronze_arrowheads", "feathers"):
        assert player.count(spent) == 0, spent
    assert player.count("hammer") == 1, "the hammer is a tool and survives smithing"
