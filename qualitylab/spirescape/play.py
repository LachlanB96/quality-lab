"""Gameplay steps built on GameClient, so tests read as what a player does rather than as packets.

Every wait is budgeted in server ticks, then turned into seconds for the target: a step that takes
40 ticks is under a second locally (20 ms tick) and about half a minute on the live demo (600 ms).
"""
from __future__ import annotations

from dataclasses import dataclass

from . import messages as m
from .client import GameClient
from .messages import EntityKind


@dataclass(frozen=True)
class Budget:
    tick_ms: int
    margin_s: float = 5.0  # network and scheduling slack on top of the ticks

    def ticks(self, n: int) -> float:
        return n * self.tick_ms / 1000 * 1.5 + self.margin_s


def talk(c: GameClient, npc_def: str, budget: Budget, *, first_choice: int = -1, max_lines: int = 12) -> list[str]:
    """Talk to an NPC and click through to the end. Returns every line of dialogue seen."""
    npc = c.find(EntityKind.NPC, npc_def)
    assert npc is not None, f"no {npc_def} in {c.read(lambda s: s.map.map_id)}"
    c.send(m.Interact(entity_id=npc.id, action="Talk-to"))
    first = c.wait_until(lambda s: s.dialogue, budget.ticks(60), f"{npc_def} to speak")
    lines = [first.text]
    if first_choice >= 0:
        seen = c.read(lambda s: s.dialogue_count)
        c.send(m.DialogueChoice(index=first_choice))
        c.wait_until(lambda s: s.dialogue is None or s.dialogue_count > seen, budget.ticks(5), "dialogue after choice")
        if c.read(lambda s: s.dialogue):
            lines.append(c.read(lambda s: s.dialogue.text))
    for _ in range(max_lines):
        if c.read(lambda s: s.dialogue) is None:
            break
        seen = c.read(lambda s: s.dialogue_count)
        c.send(m.DialogueChoice(index=-1))
        c.wait_until(lambda s: s.dialogue is None or s.dialogue_count > seen, budget.ticks(5), "dialogue to advance")
        d = c.read(lambda s: s.dialogue)
        if d is not None:
            lines.append(d.text)
    c.wait_until(lambda s: s.dialogue is None, budget.ticks(5), "dialogue to close")
    return lines


def get_starter_kit(c: GameClient, budget: Budget) -> None:
    talk(c, "old_bram", budget, first_choice=1)
    c.wait_until(lambda s: _count(s, "shortbow") == 1, budget.ticks(5), "starter kit")


# What the server says when someone else got there first. On the live demo other players (and other
# test workers) share the world, so a rock can deplete or a rat die while you walk to it.
LOST_THE_RACE = ("That isn't there any more.", "There's nothing left here right now.", "It's a bit busy right now.",
                 "Someone else is already fighting that.", "You can't reach that.")


_RETRY = object()


def _attempt_outcome(start: int, done):
    """done's value once it holds, _RETRY (lost a race, or the action was cancelled under us), or None (still going)."""
    def check(s):
        value = done(s)
        if value:
            return value
        started = False
        for r in s.log[start:]:
            msg = r.msg
            if isinstance(msg, m.Notice) and msg.text in LOST_THE_RACE:
                return _RETRY
            if isinstance(msg, m.ActionStatus):
                if msg.ticks > 0:
                    started = True
                elif started:
                    return _RETRY  # our verb began, then was cancelled: the node depleted under us
        return None
    return check


def interact(c: GameClient, kind: EntityKind, def_id: str, verb: str, done, budget: Budget, what: str, *,
             ticks: int = 120, attempts: int = 4):
    """Use `verb` on the nearest free `def_id` until done(state) holds, and return done's value.

    On the live demo someone else can get there first, so a lost race tries again on whichever one is free.
    Only nodes deplete (and respawn); only creatures fight.
    """
    for _ in range(attempts):
        c.wait_until(lambda s: any(e.kind == kind and e.def_id == def_id and not e.depleted and not e.in_combat
                                   for e in s.entities.values()), budget.ticks(60), f"an available {def_id}")
        target = c.nearest(kind, def_id, lambda e: not e.depleted and not e.in_combat)
        start = c.mark()
        c.send(m.Interact(entity_id=target.id, action=verb))
        outcome = c.wait_until(_attempt_outcome(start, done), budget.ticks(ticks), what)
        if outcome is not _RETRY:
            return outcome
    raise AssertionError(f"lost the race for a {def_id} {attempts} times running")


def gather(c: GameClient, kind: EntityKind, def_id: str, verb: str, card: str, at_least: int, budget: Budget,
           ticks: int = 120, attempts: int = 4) -> None:
    interact(c, kind, def_id, verb, lambda s: _count(s, card) >= at_least, budget, f"{at_least}+ {card}",
             ticks=ticks, attempts=attempts)


def open_station(c: GameClient, station: str, budget: Budget) -> m.CraftMenu:
    ent = c.find(EntityKind.STATION, station)
    assert ent is not None, f"no {station} here"
    verb = "Smith" if station == "forge" else "Craft"
    start = c.mark()
    c.send(m.Interact(entity_id=ent.id, action=verb))
    return c.wait_for(m.CraftMenu, budget.ticks(80), f"{station} menu", after=start,
                      where=lambda msg: msg.station == station and len(msg.recipe_ids) > 0)


def craft(c: GameClient, station: str, recipe: str, card: str, at_least: int, budget: Budget) -> None:
    menu = open_station(c, station, budget)
    assert recipe in menu.recipe_ids, f"{recipe} not offered at the {station}: {menu.recipe_ids}"
    i = menu.recipe_ids.index(recipe)
    assert menu.can_make[i], f"{recipe} blocked: {menu.reason[i]}"
    c.send(m.Craft(recipe_id=recipe, count=1))
    c.wait_until(lambda s: _count(s, card) >= at_least, budget.ticks(40), f"{at_least}+ {card}")


def equip(c: GameClient, card: str, budget: Budget) -> None:
    c.send(m.InventoryAction(slot=c.slot_of(card), action="Equip"))
    c.wait_until(lambda s: card in s.inventory.equip_card_ids, budget.ticks(5), f"{card} equipped")


def arrow_chain(c: GameClient, budget: Budget) -> None:
    """Old Bram's kit to 15 bronze arrows: feathers, logs, shafts, ore, bar, heads, arrows."""
    get_starter_kit(c, budget)
    gather(c, EntityKind.CREATURE, "chicken", "Pluck", "feathers", 15, budget)
    gather(c, EntityKind.NODE, "tree", "Chop", "logs", 1, budget)
    craft(c, "bench", "cut_shafts", "arrow_shafts", 15, budget)
    gather(c, EntityKind.NODE, "copper_rock", "Mine", "copper_ore", 1, budget)
    gather(c, EntityKind.NODE, "tin_rock", "Mine", "tin_ore", 1, budget)
    craft(c, "forge", "smelt_bronze", "bronze_bar", 1, budget)
    craft(c, "forge", "smith_arrowheads", "bronze_arrowheads", 15, budget)
    craft(c, "bench", "fletch_bronze_arrows", "bronze_arrows", 15, budget)


def pick_cards(c: GameClient, snap: m.CombatSnapshot) -> list[int]:
    """A simple fighter: arrows first, then anything that hits or blocks, within this turn's energy."""
    energy = snap.player.energy
    cards = c.read(lambda s: s.cards)
    chosen = []
    for card in sorted(snap.player.hand, key=lambda h: 0 if h.def_id == "bronze_arrows" else 1):
        d = cards[card.def_id]
        if d.cost <= energy and (d.damage > 0 or d.block > 0):
            chosen.append(card.instance_id)
            energy -= d.cost
    return chosen


def start_fight(c: GameClient, creature: str, budget: Budget, attempts: int = 4) -> m.CombatSnapshot:
    """Attack the nearest free creature, retrying if someone else gets it first."""
    return interact(c, EntityKind.CREATURE, creature, "Attack",
                    lambda s: s.combat_end is None and s.combat, budget, "combat to start", attempts=attempts)


def fight(c: GameClient, creature: str, budget: Budget, *, strategy=pick_cards, max_turns: int = 40) -> m.CombatEnd:
    start_fight(c, creature, budget)
    for _ in range(max_turns):
        if c.read(lambda s: s.combat_end is not None or s.combat.over):
            break
        snap = c.read(lambda s: s.combat)
        seen = c.read(lambda s: s.combat_updates)
        c.send(m.CombatCommit(card_instance_ids=strategy(c, snap)))
        c.wait_until(lambda s: s.combat_updates > seen, budget.ticks(10), "combat turn to resolve")
    return c.wait_until(lambda s: s.combat_end, budget.ticks(10), "combat to end")


def _count(s, card: str) -> int:
    inv = s.inventory
    if inv is None:
        return 0
    return sum(n for cid, n in zip(inv.slot_card_ids, inv.slot_counts) if cid == card)
