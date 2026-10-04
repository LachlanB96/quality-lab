"""SpireScape's wire protocol, declared as data.

Each struct or message lists its fields in wire order as (name, codec) pairs. The base class turns
that layout into a constructor, equality, encode and decode, so this file mirrors the server's
Messages.cs and Snapshots.cs field for field and nothing else. Protocol version 1.
"""
from __future__ import annotations

from enum import IntEnum

from .wire import BOOL, BYTES, I32, MAX_FRAME, STR, U8, ListOf, Reader, WireError, Writer

PROTOCOL_VERSION = 1
INVENTORY_SLOTS = 28
SKILL_COUNT = 16


class EntityKind(IntEnum):
    PLAYER = 0
    NPC = 1
    CREATURE = 2
    NODE = 3
    STATION = 4
    GROUND_ITEM = 5
    PORTAL = 6


class Skill(IntEnum):
    HITPOINTS = 0
    MINING = 1
    WOODCUTTING = 2
    FISHING = 3
    FARMING = 4
    SMITHING = 5
    COOKING = 6
    CRAFTING = 7
    RUNECRAFTING = 8
    PRAYER = 9
    SLAYER = 10
    STRENGTH = 11
    RANGED = 12
    MAGIC = 13
    DEFENCE = 14
    ENGINEERING = 15


class EquipSlot(IntEnum):
    NONE = 0
    WEAPON = 1
    HEAD = 2
    BODY = 3


EQUIP_ORDER = (EquipSlot.WEAPON, EquipSlot.HEAD, EquipSlot.BODY)  # Equipment.AllSlots


class CombatEventType(IntEnum):
    TURN_START = 0
    PLAY = 1
    HIT = 2
    MISS = 3
    BLOCK = 4
    HEAL = 5
    FIZZLE = 6
    DEATH = 7
    SHUFFLE = 8
    INFO = 9
    TURN_END = 10


class Struct:
    """A fixed sequence of fields. Subclasses set `layout`. Also usable as a codec."""

    layout: tuple = ()

    def __init__(self, **values) -> None:
        names = {n for n, _ in self.layout}
        unknown = set(values) - names
        if unknown:
            raise TypeError(f"{type(self).__name__} has no field(s) {sorted(unknown)}")
        for name, codec in self.layout:
            setattr(self, name, values[name] if name in values else codec.default())

    # codec protocol, so a Struct class can sit inside another layout or a ListOf
    @classmethod
    def read(cls, r: Reader):
        obj = cls.__new__(cls)
        for name, codec in cls.layout:
            setattr(obj, name, codec.read(r))
        return obj

    @classmethod
    def write(cls, w: Writer, obj) -> None:
        for name, codec in cls.layout:
            codec.write(w, getattr(obj, name))

    @classmethod
    def default(cls):
        return cls()

    def as_dict(self) -> dict:
        out = {}
        for name, _ in self.layout:
            v = getattr(self, name)
            if isinstance(v, Struct):
                v = v.as_dict()
            elif isinstance(v, list):
                v = [x.as_dict() if isinstance(x, Struct) else x for x in v]
            out[name] = v
        return out

    def __eq__(self, other) -> bool:
        return type(self) is type(other) and self.as_dict() == other.as_dict()

    def __repr__(self) -> str:
        body = ", ".join(f"{n}={getattr(self, n)!r}" for n, _ in self.layout)
        return f"{type(self).__name__}({body})"


# ------------------------------------------------------------------------------------------------
# nested structs (Snapshots.cs, CardDef.cs, RecipeDef.cs, CombatEvent.cs)
# ------------------------------------------------------------------------------------------------

class EntityInfo(Struct):
    layout = (("id", I32), ("kind", U8), ("name", STR), ("def_id", STR), ("x", I32), ("y", I32),
              ("in_combat", BOOL), ("depleted", BOOL), ("actions", ListOf(STR)))


class CardDef(Struct):
    layout = (("id", STR), ("name", STR), ("kind", U8), ("cost", I32), ("damage", I32), ("accuracy", I32),
              ("block", I32), ("heal", I32), ("stackable", BOOL), ("innate", BOOL), ("consumed", BOOL),
              ("requires_bow", BOOL), ("equip_slot", U8), ("equip_enables_ammo", BOOL),
              ("equip_ammo_damage_bonus", I32), ("equip_block_bonus", I32), ("equip_inject_card_id", STR),
              ("equip_inject_count", I32), ("value", I32), ("text", STR), ("examine", STR))


class ItemStack(Struct):
    layout = (("card_id", STR), ("count", I32))


class RecipeDef(Struct):
    layout = (("id", STR), ("name", STR), ("skill", U8), ("level", I32), ("station", STR), ("tool", STR),
              ("inputs", ListOf(ItemStack)), ("output", ItemStack), ("xp", I32), ("duration_ticks", I32))


class CombatCardView(Struct):
    layout = (("instance_id", I32), ("def_id", STR), ("count", I32))


class CombatantView(Struct):
    layout = (("name", STR), ("hp", I32), ("max_hp", I32), ("block", I32), ("energy", I32), ("max_energy", I32),
              ("has_bow", BOOL), ("ammo_damage_bonus", I32), ("hand", ListOf(CombatCardView)),
              ("draw_pile_def_ids", ListOf(STR)), ("discard_def_ids", ListOf(STR)),
              ("hand_count", I32), ("draw_count", I32), ("discard_count", I32))


class CombatSnapshot(Struct):
    layout = (("player", CombatantView), ("enemy", CombatantView), ("turn", I32), ("over", BOOL),
              ("player_won", BOOL))


class CombatEvent(Struct):
    layout = (("type", U8), ("by_player", BOOL), ("card_name", STR), ("amount", I32), ("pair", I32),
              ("text", STR))


# ------------------------------------------------------------------------------------------------
# messages (Messages.cs)
# ------------------------------------------------------------------------------------------------

class Message(Struct):
    type_id: int = 0
    direction: str = ""  # "c2s" or "s2c"


REGISTRY: dict[int, type[Message]] = {}


def _register(cls):
    REGISTRY[cls.type_id] = cls
    return cls


@_register
class Hello(Message):
    type_id, direction = 1, "c2s"
    layout = (("name", STR), ("version", I32))

    def __init__(self, **values) -> None:
        values.setdefault("version", PROTOCOL_VERSION)
        super().__init__(**values)


@_register
class MoveTo(Message):
    type_id, direction = 2, "c2s"
    layout = (("x", I32), ("y", I32))


@_register
class Interact(Message):
    type_id, direction = 3, "c2s"
    layout = (("entity_id", I32), ("action", STR))


@_register
class DialogueChoice(Message):
    type_id, direction = 4, "c2s"
    layout = (("index", I32),)

    def __init__(self, **values) -> None:
        values.setdefault("index", -1)
        super().__init__(**values)


@_register
class Craft(Message):
    type_id, direction = 5, "c2s"
    layout = (("recipe_id", STR), ("count", I32))


@_register
class InventoryAction(Message):
    type_id, direction = 6, "c2s"
    layout = (("slot", I32), ("action", STR))


@_register
class Unequip(Message):
    type_id, direction = 7, "c2s"
    layout = (("slot", U8),)


@_register
class CombatCommit(Message):
    type_id, direction = 8, "c2s"
    layout = (("card_instance_ids", ListOf(I32)),)


@_register
class Chat(Message):
    type_id, direction = 9, "c2s"
    layout = (("text", STR),)


@_register
class Welcome(Message):
    type_id, direction = 50, "s2c"
    layout = (("player_entity_id", I32), ("tick_ms", I32), ("server_name", STR))


@_register
class Catalog(Message):
    type_id, direction = 51, "s2c"
    layout = (("cards", ListOf(CardDef)), ("recipes", ListOf(RecipeDef)))


@_register
class MapData(Message):
    type_id, direction = 52, "s2c"
    layout = (("map_id", STR), ("name", STR), ("width", I32), ("height", I32), ("tiles", BYTES))


@_register
class EntitySpawn(Message):
    type_id, direction = 53, "s2c"
    layout = (("entity", EntityInfo),)


@_register
class EntityMoves(Message):
    type_id, direction = 54, "s2c"
    layout = (("ids", ListOf(I32)), ("xs", ListOf(I32)), ("ys", ListOf(I32)))


@_register
class EntityDespawn(Message):
    type_id, direction = 55, "s2c"
    layout = (("id", I32),)


@_register
class InventoryState(Message):
    type_id, direction = 56, "s2c"
    layout = (("slot_card_ids", ListOf(STR)), ("slot_counts", ListOf(I32)), ("equip_card_ids", ListOf(STR)))


@_register
class SkillsState(Message):
    type_id, direction = 57, "s2c"
    layout = (("xp", ListOf(I32)), ("current_hp", I32))


@_register
class Dialogue(Message):
    type_id, direction = 58, "s2c"
    layout = (("npc_id", I32), ("npc_name", STR), ("text", STR), ("choices", ListOf(STR)))


@_register
class DialogueClose(Message):
    type_id, direction = 59, "s2c"
    layout = ()


@_register
class Notice(Message):
    type_id, direction = 60, "s2c"
    layout = (("text", STR), ("style", U8))


@_register
class ActionStatus(Message):
    type_id, direction = 61, "s2c"
    layout = (("text", STR), ("ticks", I32))


@_register
class CraftMenu(Message):
    type_id, direction = 62, "s2c"
    layout = (("station", STR), ("recipe_ids", ListOf(STR)), ("can_make", ListOf(BOOL)), ("reason", ListOf(STR)))


@_register
class CombatStart(Message):
    type_id, direction = 63, "s2c"
    layout = (("enemy_entity_id", I32), ("snapshot", CombatSnapshot))


@_register
class CombatUpdate(Message):
    type_id, direction = 64, "s2c"
    layout = (("snapshot", CombatSnapshot), ("events", ListOf(CombatEvent)))


@_register
class CombatEnd(Message):
    type_id, direction = 65, "s2c"
    layout = (("won", BOOL), ("text", STR))


@_register
class Objective(Message):
    type_id, direction = 66, "s2c"
    layout = (("text", STR),)


@_register
class ChatLine(Message):
    type_id, direction = 67, "s2c"
    layout = (("from_", STR), ("text", STR))


# ------------------------------------------------------------------------------------------------
# framing
# ------------------------------------------------------------------------------------------------

def encode(msg: Message) -> bytes:
    """A whole frame: [int32 length][type][body]. Over WebSocket, one frame per binary message."""
    w = Writer()
    w.u8(msg.type_id)
    type(msg).write(w, msg)
    body = bytes(w.buf)
    return len(body).to_bytes(4, "little", signed=True) + body


def decode(frame: bytes) -> Message:
    """Decode one whole frame. Strict: the length prefix must match and nothing may be left over."""
    if len(frame) < 5:
        raise WireError(f"frame too short ({len(frame)} bytes)")
    declared = int.from_bytes(frame[:4], "little", signed=True)
    if declared <= 0 or declared > MAX_FRAME:
        raise WireError(f"bad frame length {declared}")
    if declared != len(frame) - 4:
        raise WireError(f"length prefix says {declared}, frame carries {len(frame) - 4}")
    r = Reader(frame, 4)
    type_id = r.u8()
    cls = REGISTRY.get(type_id)
    if cls is None:
        raise WireError(f"unknown message type {type_id}")
    msg = cls.read(r)
    if r.remaining():
        raise WireError(f"{cls.__name__}: {r.remaining()} trailing bytes")
    return msg


CLIENT_MESSAGES = [c for c in REGISTRY.values() if c.direction == "c2s"]
SERVER_MESSAGES = [c for c in REGISTRY.values() if c.direction == "s2c"]
