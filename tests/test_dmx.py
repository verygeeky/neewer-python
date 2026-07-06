"""Tests for :mod:`neewer.protocol.dmx` — the pure DMX personality / patch / rate core.

Golden-byte assertions against ``frames.*`` pin the DMX->frame conversions; the
rate-limiter is tested with an injected clock. No sockets, no radio.
"""
from __future__ import annotations

import asyncio

import pytest

from neewer.protocol import dmx, frames


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)

# --- personality conversions ----------------------------------------------

def test_hsi_endpoints():
    # hue MSB/LSB = 0 -> 0 deg; sat/intensity 255 -> 100
    assert dmx.slice_to_frame("hsi", [0, 0, 255, 255]) == frames.hsi(0, 100, 100)
    # hue 0xFFFF -> 359 deg; sat 0; intensity 128 -> 50
    assert dmx.slice_to_frame("hsi", [255, 255, 0, 128]) == frames.hsi(359, 0, 50)


def test_cct_endpoints():
    # intensity 255 -> 100; temp 0 -> CCT_MIN; gm 128 -> 50 (neutral)
    assert dmx.slice_to_frame("cct", [255, 0, 128]) == frames.cct(100, frames.CCT_MIN, 50)
    # temp 255 -> CCT_MAX; gm 255 -> 100
    assert dmx.slice_to_frame("cct", [0, 255, 255]) == frames.cct(0, frames.CCT_MAX, 100)


def test_rgb_primaries():
    # pure primaries: full saturation, full intensity, hue at 0/120/240
    assert dmx.slice_to_frame("rgb", [255, 0, 0]) == frames.hsi(0, 100, 100)
    assert dmx.slice_to_frame("rgb", [0, 255, 0]) == frames.hsi(120, 100, 100)
    assert dmx.slice_to_frame("rgb", [0, 0, 255]) == frames.hsi(240, 100, 100)


def test_rgb_white_and_black():
    # equal channels -> saturation 0 (white); full white keeps intensity 100
    assert dmx.slice_to_frame("rgb", [255, 255, 255]) == frames.hsi(0, 0, 100)
    # all-zero -> intensity 0 (black)
    assert dmx.slice_to_frame("rgb", [0, 0, 0]) == frames.hsi(0, 0, 0)


def test_rgb_footprint():
    assert dmx.PERSONALITIES["rgb"][0] == 3


# --- rgbw personality (by-MAC dedicated cold/warm white, 0xA9) ------------

_MAC = "AA:BB:CC:DD:EE:FF"


def test_rgbw_maps_white_to_both_cold_and_warm():
    # slots [R,G,B,W] -> rgbcw_by_mac(mac, bri=100, r,g,b, c=W, w=W); master full
    mac6 = frames.mac_bytes(_MAC)
    got = dmx.slice_to_frame("rgbw", [10, 20, 30, 40], mac6)
    assert got == frames.rgbcw_by_mac(mac6, 100, 10, 20, 30, 40, 40)


def test_rgbw_footprint():
    assert dmx.PERSONALITIES["rgbw"][0] == 4


def test_rgbw_without_mac_raises():
    with pytest.raises(ValueError, match="needs a target MAC"):
        dmx.slice_to_frame("rgbw", [1, 2, 3, 4])


def test_direct_personalities_unchanged_by_new_signature():
    # the optional mac6 param must not alter the direct (MAC-less) builders
    assert dmx.slice_to_frame("rgb", [255, 0, 0]) == frames.hsi(0, 100, 100)
    assert dmx.slice_to_frame("hsi", [0, 0, 255, 255]) == frames.hsi(0, 100, 100)
    assert dmx.slice_to_frame("cct", [255, 0, 128]) == frames.cct(100, frames.CCT_MIN, 50)
    # a passed mac6 is ignored by direct personalities (byte-identical result)
    assert dmx.slice_to_frame("rgb", [255, 0, 0], frames.mac_bytes(_MAC)) \
        == frames.hsi(0, 100, 100)


def test_slice_to_frame_pads_short_slices():
    # a universe packet that stopped early still yields a valid frame (zeros)
    assert dmx.slice_to_frame("hsi", [10]) == dmx.slice_to_frame("hsi", [10, 0, 0, 0])


# --- Patch + parse_patch --------------------------------------------------

def test_patch_footprint_and_slots():
    p = dmx.Patch("t1", universe=0, address=3, personality="hsi")
    assert p.footprint == 4
    # address 3 (1-based) -> slots[2:6]
    assert p.slots(list(range(10))) == [2, 3, 4, 5]


def test_parse_patch_valid():
    patches = dmx.parse_patch({
        "t1": {"universe": 0, "address": 1, "personality": "hsi"},
        "keys": {"universe": 1, "address": 20, "personality": "cct"},
    })
    assert {p.target for p in patches} == {"t1", "keys"}
    keys = next(p for p in patches if p.target == "keys")
    assert (keys.universe, keys.address, keys.footprint) == (1, 20, 3)


def test_parse_patch_defaults_personality_and_address():
    (p,) = dmx.parse_patch({"all": {"universe": 0}})
    assert (p.personality, p.address) == ("hsi", 1)


def test_parse_patch_unknown_personality_raises():
    with pytest.raises(ValueError, match="unknown personality"):
        dmx.parse_patch({"t1": {"personality": "bogus"}})


def test_parse_patch_address_overflow_raises():
    # hsi footprint 4 at address 510 -> needs 510..513, past 512
    with pytest.raises(ValueError, match="past DMX channel"):
        dmx.parse_patch({"t1": {"address": 510, "personality": "hsi"}})


# --- RateLimiter ----------------------------------------------------------

def test_rate_limiter_first_send_allowed():
    rl = dmx.RateLimiter(0.04)
    assert rl.should_send("AA", b"x", now=0.0) is True


def test_rate_limiter_unchanged_frame_suppressed():
    rl = dmx.RateLimiter(0.04)
    rl.record("AA", b"x", now=0.0)
    assert rl.should_send("AA", b"x", now=1.0) is False   # identical -> skip


def test_rate_limiter_changed_frame_respects_interval():
    rl = dmx.RateLimiter(0.04)
    rl.record("AA", b"x", now=0.0)
    # changed but too soon -> suppressed
    assert rl.should_send("AA", b"y", now=0.02) is False
    # changed and interval elapsed -> allowed
    assert rl.should_send("AA", b"y", now=0.05) is True


def test_rate_limiter_is_per_key():
    rl = dmx.RateLimiter(0.04)
    rl.record("AA", b"x", now=0.0)
    assert rl.should_send("BB", b"x", now=0.0) is True    # different tube


# --- send_tick: per-MAC frame construction for by-MAC personalities -------

class _FakeCore:
    """Records writes and effect-cancels; resolves targets from a fixed map.

    Mirrors the daemon's artnet/sacn test harness, but resolves to *real* MAC
    strings so the by-MAC (rgbw) path can embed them.
    """

    def __init__(self, mapping):
        self.mapping = mapping
        self.writes: list[tuple[str, bytes]] = []
        self.cancels = 0

    def resolve(self, target):
        return list(self.mapping.get(target, []))

    async def write(self, mac, frame):
        self.writes.append((mac, frame))
        return True

    async def cancel_effect(self):
        self.cancels += 1


def test_send_tick_rgbw_embeds_each_targets_own_mac():
    # a group -> 2 MACs; each tube must get a frame carrying ITS OWN mac bytes.
    mac_a, mac_b = "AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"
    core = _FakeCore({"keys": [mac_a, mac_b]})
    patches = dmx.parse_patch({"keys": {"universe": 0, "address": 1, "personality": "rgbw"}})
    latest = {0: [10, 20, 30, 40] + [0] * 508}

    written = _run(dmx.send_tick(
        core, patches, latest, dmx.RateLimiter(), {"owning": False}, now=1.0))

    # the parallel path returned the written list, in resolve order
    assert [mac for mac, _ in written] == [mac_a, mac_b]
    # each frame is the by-MAC rgbcw frame for its own tube (white -> cold+warm)
    frame_a = frames.rgbcw_by_mac(frames.mac_bytes(mac_a), 100, 10, 20, 30, 40, 40)
    frame_b = frames.rgbcw_by_mac(frames.mac_bytes(mac_b), 100, 10, 20, 30, 40, 40)
    assert written == [(mac_a, frame_a), (mac_b, frame_b)]
    # the two frames differ (only the embedded MAC bytes differ)
    assert frame_a != frame_b
    assert core.cancels == 1


# --- send_tick + WriteGovernor: drop-newest, never queue (#46) --------------

def _hsi_universe(intensity):
    """A 512-slot universe: hsi at address 1, hue 0, sat max, given intensity."""
    return [0, 0, 255, intensity] + [0] * 508


def test_send_tick_governor_drop_newest_skips_over_demanded_tube():
    """An over-demanded tube is SKIPPED for the tick, not queued: the deferred
    frame is never written later — the next allowed tick carries the newest look."""
    mac = "AA:BB:CC:DD:EE:01"
    core = _FakeCore({"t1": [mac]})
    patches = dmx.parse_patch({"t1": {"universe": 0, "address": 1, "personality": "hsi"}})
    limiter = dmx.RateLimiter(0.0)              # no interval floor; governor gates
    governors = dmx.GovernorBook(rate_init=1.0, rate_min=1.0)   # 1 write/s
    state = {"owning": False}

    # Tick 1: first write passes (fresh token).
    latest = {0: _hsi_universe(10)}
    w1 = _run(dmx.send_tick(core, patches, latest, limiter, state, 1.0, governors))
    assert [m for m, _ in w1] == [mac]

    # Tick 2, 33 ms later with a CHANGED frame: token bucket empty -> deferred.
    latest[0] = _hsi_universe(20)
    w2 = _run(dmx.send_tick(core, patches, latest, limiter, state, 1.033, governors))
    assert w2 == []
    assert governors[mac].deferred == 1
    assert len(core.writes) == 1                # nothing queued behind the scenes

    # Tick 3, past the refill, with an even NEWER frame: exactly one write goes
    # out and it is the NEWEST frame — the tick-2 look was dropped, not queued.
    latest[0] = _hsi_universe(30)
    w3 = _run(dmx.send_tick(core, patches, latest, limiter, state, 2.1, governors))
    assert w3 == [(mac, frames.hsi(0, 100, round(30 * 100 / 255)))]
    assert len(core.writes) == 2                # 1st + 3rd look only; 2nd never sent


def test_send_tick_governor_gates_per_tube_while_gather_still_runs():
    """One paced-out tube must not hold back the others in the same tick."""
    fast, slow = "AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"
    core = _FakeCore({"keys": [fast, slow]})
    patches = dmx.parse_patch({"keys": {"universe": 0, "address": 1, "personality": "hsi"}})
    limiter = dmx.RateLimiter(0.0)
    governors = dmx.GovernorBook()
    governors[slow] = dmx.WriteGovernor(rate_init=1.0, rate_min=1.0)
    governors[slow].allow(0.999)                # drain the slow tube's only token
    state = {"owning": False}

    latest = {0: _hsi_universe(10)}
    written = _run(dmx.send_tick(core, patches, latest, limiter, state, 1.0, governors))

    assert [m for m, _ in written] == [fast]    # fast tube written this tick...
    assert governors[slow].deferred == 1        # ...slow one deferred, not queued
    assert [m for m, _ in core.writes] == [fast]


def test_send_tick_unchanged_frame_consumes_no_governor_token():
    """Change-detection runs BEFORE the governor: a static look costs no tokens."""
    mac = "AA:BB:CC:DD:EE:01"
    core = _FakeCore({"t1": [mac]})
    patches = dmx.parse_patch({"t1": {"universe": 0, "address": 1, "personality": "hsi"}})
    limiter = dmx.RateLimiter(0.0)
    governors = dmx.GovernorBook(rate_init=1.0, rate_min=1.0)
    state = {"owning": False}
    latest = {0: _hsi_universe(10)}

    _run(dmx.send_tick(core, patches, latest, limiter, state, 1.0, governors))
    # Same frame again: suppressed by change-detection, so neither sent nor deferred.
    _run(dmx.send_tick(core, patches, latest, limiter, state, 1.033, governors))
    assert governors[mac].sent == 1 and governors[mac].deferred == 0


def test_send_tick_successful_writes_feed_the_delivery_estimate():
    """Each delivered write is a bw sample: the governor learns the issued rate."""
    mac = "AA:BB:CC:DD:EE:01"
    core = _FakeCore({"t1": [mac]})
    patches = dmx.parse_patch({"t1": {"universe": 0, "address": 1, "personality": "hsi"}})
    limiter = dmx.RateLimiter(0.0)
    governors = dmx.GovernorBook(rate_init=50.0)
    state = {"owning": False}

    now = 1.0
    for i in range(20):                         # a changing look at 10 ticks/s
        # step the hue MSB so every tick's frame is genuinely distinct (the
        # intensity channel's 0-255 -> 0-100 rounding would alias neighbours)
        latest = {0: [i * 5, 0, 255, 128] + [0] * 508}
        _run(dmx.send_tick(core, patches, latest, limiter, state, now, governors))
        now += 0.1
    assert governors[mac].bw > 0                # seeded from the delivered writes
    assert abs(governors[mac].bw - 10.0) < 2.0  # ~the observed 10 writes/s


class _FailingCore(_FakeCore):
    """A core whose writes always fail (half-open link path)."""

    async def write(self, mac, frame):
        self.writes.append((mac, frame))
        return False


def test_send_tick_failed_write_is_not_a_delivery_sample():
    mac = "AA:BB:CC:DD:EE:01"
    core = _FailingCore({"t1": [mac]})
    patches = dmx.parse_patch({"t1": {"universe": 0, "address": 1, "personality": "hsi"}})
    governors = dmx.GovernorBook()
    written = _run(dmx.send_tick(core, patches, {0: _hsi_universe(10)},
                                 dmx.RateLimiter(0.0), {"owning": False}, 1.0, governors))
    assert written == []
    assert governors[mac].bw == 0.0             # no delivery was recorded


def test_send_tick_without_governors_is_unchanged():
    # governors=None (every existing caller): identical pre-governor behaviour.
    mac = "AA:BB:CC:DD:EE:01"
    core = _FakeCore({"t1": [mac]})
    patches = dmx.parse_patch({"t1": {"universe": 0, "address": 1, "personality": "hsi"}})
    limiter = dmx.RateLimiter(0.0)
    state = {"owning": False}
    for i, now in enumerate((1.0, 1.01, 1.02)):     # far faster than any pacer
        latest = {0: [i * 50, 0, 255, 128] + [0] * 508}     # distinct hue per tick
        written = _run(dmx.send_tick(core, patches, latest, limiter, state, now))
        assert len(written) == 1                    # nothing gates the writes
    assert len(core.writes) == 3
