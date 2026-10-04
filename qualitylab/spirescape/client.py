"""A headless SpireScape player for tests.

Connects over WebSocket (the same transport the WebGL demo uses), keeps the world state the server
sends, and lets a test wait for a condition instead of sleeping. A background thread reads frames;
every update notifies a Condition, so `wait_until` wakes the moment state changes.

The state model follows the server: entities upsert on EntitySpawn, move on EntityMoves, leave on
EntityDespawn; MapData means a new region, so every entity is dropped.
"""
from __future__ import annotations

import contextlib
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import ClientConnection, connect

from . import messages as m
from .messages import EntityKind


class WaitTimeout(AssertionError):
    """A wait_until that ran out of time. An AssertionError, so pytest reports it as a test failure."""


# Protocol coverage for the whole process: which message types every client sent and received.
# conftest writes it out per worker at the end of a session; the run record merges the workers.
TRAFFIC: dict[str, dict[str, int]] = {"sent": {}, "received": {}}
_traffic_lock = threading.Lock()


def _count_traffic(direction: str, name: str) -> None:
    with _traffic_lock:
        TRAFFIC[direction][name] = TRAFFIC[direction].get(name, 0) + 1


@dataclass
class Received:
    at: float  # time.monotonic() when the frame arrived
    msg: m.Message


@dataclass
class State:
    player_id: int = 0
    tick_ms: int = 0
    server_name: str = ""
    cards: dict = field(default_factory=dict)       # id -> CardDef
    recipes: dict = field(default_factory=dict)     # id -> RecipeDef
    map: m.MapData | None = None
    entities: dict = field(default_factory=dict)    # id -> EntityInfo
    inventory: m.InventoryState | None = None
    skills: m.SkillsState | None = None
    dialogue: m.Dialogue | None = None
    dialogue_count: int = 0
    notices: list = field(default_factory=list)     # Notice texts, oldest first
    action: m.ActionStatus | None = None
    craft_menu: m.CraftMenu | None = None
    combat: m.CombatSnapshot | None = None
    combat_updates: int = 0
    combat_end: m.CombatEnd | None = None
    last_combat_events: list = field(default_factory=list)
    objective: str = ""
    chat: list = field(default_factory=list)        # ChatLine
    log: list = field(default_factory=list)         # Received, every frame in order


class GameClient:
    def __init__(self, url: str, *, connect_timeout: float = 15.0, close_timeout: float = 1.0,
                 origin: str | None = None) -> None:
        """close_timeout: how long to wait for the server to drop TCP after the close handshake. The
        server never does (BUG-006), so the websockets default of 10 s would cost every disconnect 10 s."""
        self.url = url
        self.state = State()
        self.closed = threading.Event()
        self.close_reason = ""
        self.decode_errors: list[str] = []
        self._cv = threading.Condition()
        self._stack = contextlib.ExitStack()
        self._ws: ClientConnection = self._stack.enter_context(
            connect(url, open_timeout=connect_timeout, close_timeout=close_timeout, max_size=m.MAX_FRAME * 2,
                    origin=origin, compression=None))
        self._reader = threading.Thread(target=self._read_loop, name="spirescape-reader", daemon=True)
        self._reader.start()

    # -------------------------------------------------------------------------------------------- io

    def send(self, msg: m.Message) -> None:
        self._ws.send(m.encode(msg))
        _count_traffic("sent", type(msg).__name__)

    def send_raw(self, data: bytes) -> None:
        """Bytes exactly as given, for robustness tests. Not a frame the codec would ever produce."""
        self._ws.send(data)

    def close(self) -> None:
        try:
            self._stack.close()
        finally:
            self.closed.wait(5)

    def __enter__(self) -> "GameClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _read_loop(self) -> None:
        try:
            for frame in self._ws:
                if isinstance(frame, str):
                    self.decode_errors.append("text frame from server")
                    continue
                try:
                    msg = m.decode(frame)
                except m.WireError as e:
                    with self._cv:
                        self.decode_errors.append(str(e))
                        self._cv.notify_all()
                    continue
                _count_traffic("received", type(msg).__name__)
                with self._cv:
                    self._apply(msg)
                    self.state.log.append(Received(time.monotonic(), msg))
                    self._cv.notify_all()
        except ConnectionClosed as e:
            self.close_reason = f"{e.rcvd.code if e.rcvd else 'no close frame'}"
        except Exception as e:  # noqa: BLE001 - a reader crash must surface as a closed client
            self.close_reason = f"reader error: {e!r}"
        finally:
            with self._cv:
                self.closed.set()
                self._cv.notify_all()

    # -------------------------------------------------------------------------------------------- state

    def _apply(self, msg: m.Message) -> None:
        s = self.state
        if isinstance(msg, m.Welcome):
            s.player_id, s.tick_ms, s.server_name = msg.player_entity_id, msg.tick_ms, msg.server_name
        elif isinstance(msg, m.Catalog):
            s.cards = {c.id: c for c in msg.cards}
            s.recipes = {r.id: r for r in msg.recipes}
        elif isinstance(msg, m.MapData):
            s.map = msg
            s.entities = {}
        elif isinstance(msg, m.EntitySpawn):
            s.entities[msg.entity.id] = msg.entity
        elif isinstance(msg, m.EntityMoves):
            for i, x, y in zip(msg.ids, msg.xs, msg.ys):
                e = s.entities.get(i)
                if e is not None:
                    e.x, e.y = x, y
        elif isinstance(msg, m.EntityDespawn):
            s.entities.pop(msg.id, None)
        elif isinstance(msg, m.InventoryState):
            s.inventory = msg
        elif isinstance(msg, m.SkillsState):
            s.skills = msg
        elif isinstance(msg, m.Dialogue):
            s.dialogue = msg
            s.dialogue_count += 1
        elif isinstance(msg, m.DialogueClose):
            s.dialogue = None
        elif isinstance(msg, m.Notice):
            s.notices.append(msg.text)
        elif isinstance(msg, m.ActionStatus):
            s.action = msg if msg.ticks > 0 else None
        elif isinstance(msg, m.CraftMenu):
            s.craft_menu = msg
        elif isinstance(msg, m.CombatStart):
            s.combat, s.combat_end = msg.snapshot, None
            s.last_combat_events = []
        elif isinstance(msg, m.CombatUpdate):
            s.combat = msg.snapshot
            s.combat_updates += 1
            s.last_combat_events = msg.events
        elif isinstance(msg, m.CombatEnd):
            s.combat_end = msg
        elif isinstance(msg, m.Objective):
            s.objective = msg.text
        elif isinstance(msg, m.ChatLine):
            s.chat.append(msg)

    def read(self, fn: Callable[[State], object]):
        """Read state under the lock, so a test never sees half an update."""
        with self._cv:
            return fn(self.state)

    def wait_until(self, pred: Callable[[State], bool], timeout: float, what: str):
        """Block until pred(state) is truthy, and return its value. Fails with context, never hangs."""
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                value = pred(self.state)
                if value:
                    return value
                left = deadline - time.monotonic()
                if left <= 0 or self.closed.is_set():
                    break
                self._cv.wait(min(left, 1.0))
        raise WaitTimeout(self._describe_timeout(what, timeout))

    def wait_for(self, msg_type: type[m.Message], timeout: float, what: str, *, after: int = 0,
                 where: Callable[[m.Message], bool] = lambda _: True) -> m.Message:
        """The first message of a type received at log index >= after that matches `where`."""
        def find(s: State):
            for rec in s.log[after:]:
                if isinstance(rec.msg, msg_type) and where(rec.msg):
                    return rec.msg
            return None
        return self.wait_until(find, timeout, what)

    def mark(self) -> int:
        """A position in the message log, for wait_for(after=...)."""
        with self._cv:
            return len(self.state.log)

    def _describe_timeout(self, what: str, timeout: float) -> str:
        s = self.state
        bits = [f"timed out after {timeout:.0f}s waiting for {what}"]
        if self.closed.is_set():
            bits.append(f"connection closed ({self.close_reason})")
        if s.objective:
            bits.append(f"objective: {s.objective!r}")
        if s.notices:
            bits.append("last notices: " + " | ".join(s.notices[-4:]))
        if s.action:
            bits.append(f"busy: {s.action.text}")
        return "; ".join(bits)

    # -------------------------------------------------------------------------------------------- helpers

    def login(self, name: str, timeout: float = 30.0) -> "GameClient":
        self.send(m.Hello(name=name))
        self.wait_until(lambda s: s.map is not None and s.inventory is not None and s.objective, timeout, "login")
        return self

    @property
    def me(self) -> m.EntityInfo | None:
        return self.read(lambda s: s.entities.get(s.player_id))

    def find(self, kind: EntityKind, def_id: str = "", where: Callable[[m.EntityInfo], bool] | None = None):
        def pick(s: State):
            for e in s.entities.values():
                if e.kind == kind and (not def_id or e.def_id == def_id) and (where is None or where(e)):
                    return e
            return None
        return self.read(pick)

    def nearest(self, kind: EntityKind, def_id: str = "", where: Callable[[m.EntityInfo], bool] | None = None):
        def pick(s: State):
            me = s.entities.get(s.player_id)
            best, best_d = None, None
            for e in s.entities.values():
                if e.kind != kind or (def_id and e.def_id != def_id) or (where and not where(e)):
                    continue
                d = 0 if me is None else max(abs(e.x - me.x), abs(e.y - me.y))
                if best is None or d < best_d:
                    best, best_d = e, d
            return best
        return self.read(pick)

    def count(self, card_id: str) -> int:
        def c(s: State):
            inv = s.inventory
            if inv is None:
                return 0
            return sum(n for cid, n in zip(inv.slot_card_ids, inv.slot_counts) if cid == card_id)
        return self.read(c)

    def slot_of(self, card_id: str) -> int:
        inv = self.read(lambda s: s.inventory)
        return inv.slot_card_ids.index(card_id)

    def used_slots(self) -> int:
        return self.read(lambda s: sum(1 for cid in s.inventory.slot_card_ids if cid))

    def equipped(self, slot: m.EquipSlot) -> str:
        inv = self.read(lambda s: s.inventory)
        return inv.equip_card_ids[m.EQUIP_ORDER.index(slot)]

    def xp(self, skill: m.Skill) -> int:
        return self.read(lambda s: s.skills.xp[skill] if s.skills else 0)

    def notices_since(self, start: int) -> list[str]:
        return self.read(lambda s: s.notices[start:])

    def messages(self, msg_type: type[m.Message] | None = None) -> Iterable[Received]:
        return self.read(lambda s: [r for r in s.log if msg_type is None or isinstance(r.msg, msg_type)])
