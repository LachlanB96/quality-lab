"""Timing: the 600 ms tick (D-167) holds, and the server answers intents between ticks, not on them.

Each test records its measurements as metrics, so the report can chart them across runs.
"""
import statistics
import time

import pytest

from qualitylab.spirescape import messages as m
from qualitylab.spirescape.client import GameClient
from qualitylab.spirescape.messages import EntityKind

pytestmark = [pytest.mark.perf, pytest.mark.area("performance")]


def metric(record_property, name: str, value: float, unit: str) -> None:
    record_property("metric", {"name": name, "value": round(value, 2), "unit": unit})


def percentile(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def test_tick_cadence_matches_the_announced_tick(player, target, record_property):
    """Gaps between the player's own movement updates on a long walk should sit on the tick."""
    start = player.mark()
    player.send(m.MoveTo(x=27, y=8))
    player.wait_until(lambda s: (s.entities[s.player_id].x, s.entities[s.player_id].y) == (27, 8),
                      target.tick_ms / 1000 * 60 + 10, "the long walk")
    pid = player.read(lambda s: s.player_id)
    times = [r.at for r in player.read(lambda s: s.log[start:]) if isinstance(r.msg, m.EntityMoves) and pid in r.msg.ids]
    gaps = [(b - a) * 1000 for a, b in zip(times, times[1:])]
    assert len(gaps) >= 5, f"too few movement updates to measure ({len(gaps)})"
    median = statistics.median(gaps)
    metric(record_property, "tick_median_ms", median, "ms")
    metric(record_property, "tick_p95_ms", percentile(gaps, 95), "ms")
    tolerance = max(15.0, target.tick_ms * 0.15)  # Windows timer granularity dominates the 20 ms local tick
    assert abs(median - target.tick_ms) <= tolerance, f"median gap {median:.0f} ms vs tick {target.tick_ms} ms"


def test_examine_round_trip(player, target, record_property):
    """Examine is answered from the inbound queue (every 5 ms), so it costs a network round trip, not a tick."""
    bram = player.find(EntityKind.NPC, "old_bram")
    rtts = []
    for _ in range(20):
        seen = len(player.read(lambda s: s.notices))
        t0 = time.monotonic()
        player.send(m.Interact(entity_id=bram.id, action="Examine"))
        player.wait_until(lambda s: len(s.notices) > seen, 10, "examine reply")
        rtts.append((time.monotonic() - t0) * 1000)
    p50, p95 = statistics.median(rtts), percentile(rtts, 95)
    metric(record_property, "examine_rtt_p50_ms", p50, "ms")
    metric(record_property, "examine_rtt_p95_ms", p95, "ms")
    budget_ms = 1500 if target.is_live else 100
    assert p95 < budget_ms, f"p95 {p95:.0f} ms over the {budget_ms} ms budget"


def test_login_time(target, record_property):
    """Connect, Hello, and everything up to the first objective."""
    t0 = time.monotonic()
    with GameClient(target.ws_url) as c:
        c.login("qa-timer")
        elapsed = (time.monotonic() - t0) * 1000
        login_bytes = sum(len(m.encode(r.msg)) for r in c.messages())
    metric(record_property, "login_ms", elapsed, "ms")
    metric(record_property, "login_kb", login_bytes / 1024, "KB")
    assert elapsed < (5000 if target.is_live else 1500)
    assert login_bytes < 64 * 1024, "the login burst should stay well under a frame's 256 KB limit"
