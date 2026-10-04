"""The server's side of the protocol contract: what a client is promised on login and in every update."""
import pytest

from qualitylab.spirescape import messages as m
from qualitylab.spirescape.messages import EntityKind

pytestmark = [pytest.mark.contract, pytest.mark.area("protocol")]

LOGIN_SEQUENCE = [m.Welcome, m.Catalog, m.MapData]


def test_login_starts_with_welcome_catalog_then_map(player):
    """The first three messages are always Welcome, Catalog, MapData, in that order."""
    first = [type(r.msg) for r in player.messages()[:3]]
    assert first == LOGIN_SEQUENCE


def test_login_ends_with_inventory_skills_and_objective(player):
    """After the map and its entities come the player's own state, then the tutorial objective."""
    types = [type(r.msg) for r in player.messages()]
    tail = [t for t in types if t in (m.InventoryState, m.SkillsState, m.Objective)][:3]
    assert tail == [m.InventoryState, m.SkillsState, m.Objective]
    assert types.index(m.Objective) > types.index(m.MapData)


def test_welcome_reports_the_tick_this_target_runs_at(player, target):
    assert player.read(lambda s: s.tick_ms) == target.tick_ms
    assert player.read(lambda s: s.server_name) == "SpireScape prototype"


def test_player_entity_is_spawned_for_its_owner(player):
    me = player.me
    assert me is not None, "the server never sent the player's own entity"
    assert me.kind == EntityKind.PLAYER
    assert me.actions == ["Examine"]


def test_inventory_has_28_slots_and_3_equipment_slots(player):
    inv = player.read(lambda s: s.inventory)
    assert len(inv.slot_card_ids) == m.INVENTORY_SLOTS
    assert len(inv.slot_counts) == m.INVENTORY_SLOTS
    assert len(inv.equip_card_ids) == len(m.EQUIP_ORDER)


def test_empty_slots_are_empty_strings_with_zero_counts(player):
    inv = player.read(lambda s: s.inventory)
    for cid, n in zip(inv.slot_card_ids, inv.slot_counts):
        assert (cid == "") == (n == 0)


def test_skills_cover_all_sixteen(player):
    skills = player.read(lambda s: s.skills)
    assert len(skills.xp) == m.SKILL_COUNT
    assert skills.current_hp > 0


def test_map_tiles_match_its_dimensions(player):
    mp = player.read(lambda s: s.map)
    assert mp.map_id == "landfall"
    assert len(mp.tiles) == mp.width * mp.height
    assert set(mp.tiles) <= {0, 1, 2, 3, 4, 5}, "tile bytes outside TileType"


def test_every_entity_is_on_the_map(player):
    mp = player.read(lambda s: s.map)
    for e in player.read(lambda s: list(s.entities.values())):
        assert 0 <= e.x < mp.width and 0 <= e.y < mp.height, e


def test_every_entity_offers_examine(player):
    """Examine is on every entity's action list (the client's right-click always has it)."""
    for e in player.read(lambda s: list(s.entities.values())):
        assert "Examine" in e.actions, e


def test_entity_ids_are_unique_and_positive(player):
    ids = [e.id for e in player.read(lambda s: list(s.entities.values()))]
    assert len(ids) == len(set(ids))
    assert all(i > 0 for i in ids)


def test_every_server_frame_decodes_strictly(player):
    """The strict decoder (length prefix exact, no trailing bytes) accepted everything the server sent."""
    player.send(m.MoveTo(x=8, y=16))
    player.wait_until(lambda s: any(isinstance(r.msg, m.EntityMoves) for r in s.log), 10, "a movement update")
    assert player.decode_errors == []
