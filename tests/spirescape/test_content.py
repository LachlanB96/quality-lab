"""The content catalog every client receives at login, checked as data.

Each check walks the whole catalog and reports every violation at once, so one run lists them all.
"""
import pytest

from qualitylab.spirescape import messages as m
from qualitylab.spirescape import play
from qualitylab.spirescape.messages import EntityKind

pytestmark = [pytest.mark.data, pytest.mark.area("content")]

STARTER_KIT = ("bronze_pickaxe", "bronze_axe", "hammer", "shortbow")


@pytest.fixture(scope="module")
def catalog(target):
    from qualitylab.spirescape.client import GameClient
    with GameClient(target.ws_url) as c:
        c.login("qa-catalog")
        return c.read(lambda s: (dict(s.cards), dict(s.recipes)))


def test_recipes_only_reference_real_cards(catalog):
    cards, recipes = catalog
    missing = []
    for r in recipes.values():
        refs = [i.card_id for i in r.inputs] + [r.output.card_id] + ([r.tool] if r.tool else [])
        missing += [f"{r.id} -> {ref}" for ref in refs if ref not in cards]
    assert missing == []


def test_every_card_has_a_name_and_examine_text(catalog):
    """D-016: the examine text is how players learn what a card is for."""
    cards, _ = catalog
    blank = [c.id for c in cards.values() if not c.name.strip() or not c.examine.strip()]
    assert blank == []


def test_accuracy_is_a_percentage(catalog):
    cards, _ = catalog
    bad = [(c.id, c.accuracy) for c in cards.values() if not 0 <= c.accuracy <= 100]
    assert bad == []


def test_costs_and_counts_are_positive(catalog):
    cards, recipes = catalog
    bad = [c.id for c in cards.values() if c.cost < 1]
    bad += [r.id for r in recipes.values() if r.output.count < 1 or any(i.count < 1 for i in r.inputs)]
    bad += [r.id for r in recipes.values() if r.duration_ticks < 1]
    assert bad == []


def test_ammo_and_bulk_materials_stack(catalog):
    """D-023: arrows and the like stack. Anything a recipe makes or needs fifteen of must stack, or 15
    shafts would need 15 of the 28 slots."""
    cards, recipes = catalog
    bulk = {r.output.card_id for r in recipes.values() if r.output.count > 1}
    bulk |= {i.card_id for r in recipes.values() for i in r.inputs if i.count > 1}
    assert [cid for cid in sorted(bulk) if not cards[cid].stackable] == []


def test_equipment_points_at_real_slots(catalog):
    cards, _ = catalog
    bad = [c.id for c in cards.values() if c.equip_slot not in (s.value for s in m.EquipSlot)]
    bad += [c.id for c in cards.values() if c.equip_inject_card_id and c.equip_inject_card_id not in cards]
    assert bad == []


@pytest.mark.parametrize("tool", [
    "shortbow",
    pytest.param("bronze_axe", marks=pytest.mark.xfail(reason="BUG-001: tutorial soft-lock", strict=True)),
    pytest.param("bronze_pickaxe", marks=pytest.mark.xfail(reason="BUG-001: tutorial soft-lock", strict=True)),
    pytest.param("hammer", marks=pytest.mark.xfail(reason="BUG-001: tutorial soft-lock", strict=True)),
])
def test_a_lost_starter_tool_can_be_made_again(catalog, tool):
    """Old Bram gives the kit once. If a kit item can't be crafted, losing it (dropped, or left in a
    death pile that despawns) leaves the tutorial impossible to finish."""
    _, recipes = catalog
    makers = [r.id for r in recipes.values() if r.output.card_id == tool]
    assert makers, f"nothing makes a {tool}, and Old Bram won't give another"


@pytest.mark.local_only
@pytest.mark.e2e
@pytest.mark.area("tutorial")
@pytest.mark.xfail(reason="BUG-001: tutorial soft-lock", strict=True)
def test_dropping_the_hammer_does_not_strand_the_player(player, budget):
    """The behaviour behind BUG-001: drop the hammer, let the pile despawn (300 ticks), ask Bram again."""
    play.get_starter_kit(player, budget)
    player.send(m.InventoryAction(slot=player.slot_of("hammer"), action="Drop"))
    pile = player.wait_until(lambda s: next((e for e in s.entities.values() if e.kind == EntityKind.GROUND_ITEM), None),
                             budget.ticks(10), "the hammer on the ground")
    player.wait_until(lambda s: pile.id not in s.entities, budget.ticks(330), "the pile to despawn")
    play.talk(player, "old_bram", budget)
    menu = play.open_station(player, "forge", budget)
    reason = menu.reason[menu.recipe_ids.index("smith_arrowheads")]
    assert player.count("hammer") == 1 or reason != "Needs a Hammer", \
        "no hammer and no way to get one: smith_arrowheads is blocked for good"
