"""Tests for :mod:`neewer.effects` — option parsing, registry, and the math
of the async engines.

The engines are driven against a fake core that records every frame written.
To stay deterministic we replace the module's ``time`` (so elapsed time is a
controlled clock) and ``asyncio.sleep`` (so each tick advances the clock by a
fixed step and the loop stops after a fixed number of ticks instead of running
forever). We then assert the recorded frames are valid HSI frames with the
expected per-tube phase offset and hue progression.
"""
from __future__ import annotations

import asyncio
import types

from neewer import effects
from neewer.protocol import frames

# --- parse_opts -----------------------------------------------------------

def test_parse_opts_default_mode_is_palette():
    mode, opts = effects.parse_opts([])
    assert mode == "palette"
    assert opts == {}


def test_parse_opts_mode_and_float_coercion():
    mode, opts = effects.parse_opts(["palette", "speed=.1", "spread=.08"])
    assert mode == "palette"
    assert opts == {"speed": 0.1, "spread": 0.08}


def test_parse_opts_non_numeric_stays_string():
    _, opts = effects.parse_opts(["multistop", "stops=green"])
    assert opts == {"stops": "green"}


def test_parse_opts_ignores_args_without_equals():
    # A bare token (no '=') is not an option and is silently skipped.
    _, opts = effects.parse_opts(["hue", "fast", "speed=30"])
    assert opts == {"speed": 30.0}


# --- registry -------------------------------------------------------------

def test_registry_contains_all_engines():
    assert set(effects.REGISTRY) == {"hue", "palette", "comet", "multistop", "tri"}


def test_tri_is_alias_for_multistop():
    assert effects.REGISTRY["tri"] is effects.REGISTRY["multistop"]
    assert effects.REGISTRY["multistop"] is effects.multistop_flow


# --- engine driver --------------------------------------------------------

class _StopLoop(Exception):
    """Raised by the fake sleep to break an otherwise-infinite engine loop."""


class FakeCore:
    """Records every (mac, frame) the engine writes."""

    def __init__(self):
        self.writes: list[tuple[str, bytes]] = []

    async def write(self, mac, frame):
        self.writes.append((mac, frame))
        return True


def _drive(monkeypatch, engine, tubes, ticks, dt=1.0, **opts):
    """Run ``engine`` for exactly ``ticks`` ticks with a controlled clock.

    Each tick advances the fake clock by ``dt`` seconds. Returns the FakeCore so
    the test can inspect ``core.writes``.
    """
    clock = {"t": 1000.0}
    state = {"ticks": 0}

    fake_time = types.SimpleNamespace(perf_counter=lambda: clock["t"])

    async def fake_sleep(_interval):
        state["ticks"] += 1
        if state["ticks"] >= ticks:
            raise _StopLoop
        clock["t"] += dt

    monkeypatch.setattr(effects, "time", fake_time)
    monkeypatch.setattr(effects, "asyncio", types.SimpleNamespace(sleep=fake_sleep))

    core = FakeCore()

    async def go():
        try:
            await engine(core, tubes, **opts)
        except _StopLoop:
            pass

    asyncio.run(go())
    return core


def _assert_valid_hsi(frame: bytes):
    """Every engine frame must be a well-formed HSI command frame."""
    assert frame[0] == frames.PREFIX
    assert frame[1] == frames.OP_HSI
    assert frame[2] == 0x04
    assert frame[-1] == (sum(frame[:-1]) & 0xFF)


def _hue_of(frame: bytes) -> int:
    """Decode the little-endian hue from an HSI frame."""
    return frame[3] | (frame[4] << 8)


def _bri_of(frame: bytes) -> int:
    return frame[6]


def test_hue_flow_phase_offset_across_tubes(monkeypatch):
    tubes = ["A", "B", "C"]
    # One tick at elapsed=0: hue = spread*index*(360/count) -> 0, 120, 240.
    core = _drive(monkeypatch, effects.hue_flow, tubes, ticks=1)
    assert len(core.writes) == 3
    hues = [_hue_of(f) for _, f in core.writes]
    assert hues == [0, 120, 240]
    for _, frame in core.writes:
        _assert_valid_hsi(frame)


def test_hue_flow_progresses_with_time(monkeypatch):
    tubes = ["A"]
    # dt=1s, speed default 60 deg/s -> tube A hue at ticks 0,1,2 is 0,60,120.
    core = _drive(monkeypatch, effects.hue_flow, tubes, ticks=3, dt=1.0)
    hues = [_hue_of(f) for _, f in core.writes]
    assert hues == [0, 60, 120]


def test_comet_brightness_floor_and_peak(monkeypatch):
    tubes = ["A", "B", "C", "D"]
    # elapsed=0: tube0 is exactly under the comet (peak); a tube half a period
    # away sits at the dim floor of 4.
    core = _drive(monkeypatch, effects.comet, tubes, ticks=1)
    bris = [_bri_of(f) for _, f in core.writes]
    assert bris[0] == 80              # 4 + 76 * 1.0 at the pass
    assert min(bris) == 4            # dim floor everywhere else
    # comet uses a fixed hue, not a rainbow.
    assert all(_hue_of(f) == 210 for _, f in core.writes)


def test_palette_flow_stays_within_hue_band(monkeypatch):
    tubes = ["A", "B"]
    core = _drive(monkeypatch, effects.palette_flow, tubes, ticks=4, dt=1.0,
                  lo=240, hi=360)
    for _, frame in core.writes:
        _assert_valid_hsi(frame)
        hue = _hue_of(frame)
        # The band spans 240..360; the top edge (360) wraps to 0 in frames.hsi.
        assert hue == 0 or 240 <= hue <= 359


def test_multistop_hits_first_stop_at_phase_zero(monkeypatch):
    tubes = ["A"]
    # At elapsed=0 the interpolation sits exactly on the first stop (120).
    core = _drive(monkeypatch, effects.multistop_flow, tubes, ticks=1,
                  stops=(120, 330, 60))
    assert _hue_of(core.writes[0][1]) == 120
