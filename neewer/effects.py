"""Effect engines — animations that run *inside* the daemon.

The official app's "FX Flow" feature is gated to pixel-stick hardware. We
approximate it at whole-tube granularity: each tube is one cell of a travelling
wave, and a per-tube phase offset makes colour appear to flow along the row.

An effect is an ``async`` function ``fn(core, tubes, **opts)`` that loops until
the task running it is cancelled (``core.cancel_effect``). It writes frames to
the tubes through the connections ``core`` already holds, so there is no
per-frame connect/disconnect hitch.

``tubes`` is a list of MAC strings in physical-position order, as returned by
``core.ordered()``.
"""
from __future__ import annotations

import asyncio
import math
import time
from typing import Callable

from .protocol import frames

#: Signature of the per-tube frame function passed to ``_run``:
#: ``(elapsed_seconds, tube_index, tube_count) -> frame bytes``.
FrameFn = Callable[[float, int, int], bytes]


async def _run(core, tubes: list[str], fps: float, frame_fn: FrameFn,
               duration: float | None = None) -> None:
    """Drive ``frame_fn`` across every tube at ``fps`` until cancelled.

    On each tick we compute the elapsed time, ask ``frame_fn`` for each tube's
    frame, and write them. Per-tube write errors are swallowed so one flaky link
    cannot kill the whole animation; the supervisor will reconnect it.

    Args:
        fps: frames per second per tube.
        duration: stop after this many seconds, or ``None`` to run forever
            (the usual case — the task is cancelled externally).
    """
    interval = 1.0 / fps
    started = time.perf_counter()
    while duration is None or time.perf_counter() - started < duration:
        elapsed = time.perf_counter() - started
        count = len(tubes)
        for index, mac in enumerate(tubes):
            try:
                await core.write(mac, frame_fn(elapsed, index, count))
            except Exception:
                # A dropped tube is expected and self-healing; keep animating.
                pass
        await asyncio.sleep(interval)


async def hue_flow(core, tubes, fps=15, speed=60.0, spread=1.0,
                   sat=100, bri=80, **_) -> None:
    """A rainbow that travels along the row.

    ``speed`` is degrees of hue per second; ``spread`` scales how much of the
    full 360 spectrum is spread across the row at any instant (1.0 == a full
    rainbow from first tube to last).
    """
    def frame(elapsed, index, count):
        hue = int((elapsed * speed + spread * index * (360 / count)) % 360)
        return frames.hsi(hue, sat, bri)

    await _run(core, tubes, fps, frame)


async def palette_flow(core, tubes, fps=15, speed=0.10, spread=0.18,
                       lo=240, hi=360, sat=100, bri=80, **_) -> None:
    """A gentle ping-pong through a hue band (default blue<->magenta<->red).

    A cosine drives each tube smoothly back and forth between hues ``lo`` and
    ``hi``; ``spread`` offsets each tube's phase so the band ripples along the row.
    """
    def frame(elapsed, index, count):
        # cosine in [-1, 1] -> normalised to [0, 1] -> mapped onto the hue band.
        phase = elapsed * speed - spread * index / max(count, 1)
        normalised = (math.cos(2 * math.pi * phase) + 1) / 2
        return frames.hsi(int(lo + (hi - lo) * normalised), sat, bri)

    await _run(core, tubes, fps, frame)


async def comet(core, tubes, fps=15, speed=0.5, hue=210, sat=100, **_) -> None:
    """A single bright band sweeping the row, dark everywhere else.

    Each tube brightens sharply as the comet passes its position and fades to a
    dim floor otherwise, producing a chase.
    """
    def frame(elapsed, index, count):
        phase = (elapsed * speed - index / max(count, 1)) % 1.0
        # Distance to the nearest pass (phase wraps), turned into a brightness
        # spike: bright at the pass, a dim floor of 4 elsewhere.
        nearness = max(0.0, 1.0 - 6 * min(phase, 1 - phase))
        brightness = int(4 + 76 * nearness)
        return frames.hsi(hue, sat, brightness)

    await _run(core, tubes, fps, frame)


async def multistop_flow(core, tubes, fps=15, speed=0.055, spread=0.18,
                         stops=(120, 330, 60), sat=100, bri=80, **_) -> None:
    """Rove smoothly through an arbitrary list of hue stops, looping forever.

    Default stops are green (120) -> pink (330) -> yellow (60). Tubes lag each
    other by ``spread`` so the palette flows along the row while neighbouring
    tubes stay close in hue.
    """
    stops = list(stops)
    stop_count = len(stops)

    def hue_at(phase: float) -> float:
        """Interpolate the hue at a fractional position through the stop list."""
        position = (phase % 1.0) * stop_count
        index = int(position) % stop_count
        frac = position - int(position)
        frac = frac * frac * (3 - 2 * frac)              # smoothstep easing
        start, end = stops[index], stops[(index + 1) % stop_count]
        # Travel the shortest way around the colour wheel between the two stops.
        delta = ((end - start + 540) % 360) - 180
        return (start + delta * frac) % 360

    def frame(elapsed, index, count):
        phase = elapsed * speed - spread * index / max(count, 1)
        return frames.hsi(int(hue_at(phase)), sat, bri)

    await _run(core, tubes, fps, frame)


#: Effect name -> engine. ``tri`` is an alias for the default ``multistop``.
REGISTRY: dict[str, Callable] = {
    "hue": hue_flow,
    "palette": palette_flow,
    "comet": comet,
    "multistop": multistop_flow,
    "tri": multistop_flow,
}


def _opt(name: str, default, lo, hi, unit: str, kind: str = "float") -> dict:
    """One flow-option spec: name, default, advisory UI range, unit, value type.

    These are *host-side* engine knobs, so the ranges are sensible UI bounds,
    not protocol facts — the engines themselves don't clamp.
    """
    return {"name": name, "default": default, "min": lo, "max": hi,
            "unit": unit, "type": kind}


#: Common frame-rate knob shared by every engine (per-tube BLE writes per second).
_FPS = _opt("fps", 15, 1, 60, "frames/s", "int")

#: The multistop/tri option list, shared because ``tri`` aliases ``multistop``.
_MULTISTOP_PARAMS = [
    _FPS,
    _opt("speed", 0.055, 0.0, 2.0, "cycles/s"),
    _opt("spread", 0.18, 0.0, 2.0, "row-fraction"),
    _opt("stops", [120, 330, 60], 0, 360, "deg", "hue_list"),
    _opt("sat", 100, 0, 100, "%", "int"),
    _opt("bri", 80, 0, 100, "%", "int"),
]

#: Per-mode option metadata for discovery (name, default, range, unit) — the
#: machine-readable companion to :data:`REGISTRY`, surfaced through
#: :mod:`neewer.catalog` so UIs can build per-mode option panels instead of
#: requiring users to know the ``key=value`` spellings by heart. Defaults here
#: are pinned to the engine signatures by a test, so they cannot drift.
PARAMS: dict[str, list[dict]] = {
    "hue": [
        _FPS,
        _opt("speed", 60.0, 0.0, 360.0, "deg/s"),
        _opt("spread", 1.0, 0.0, 4.0, "rainbows/row"),
        _opt("sat", 100, 0, 100, "%", "int"),
        _opt("bri", 80, 0, 100, "%", "int"),
    ],
    "palette": [
        _FPS,
        _opt("speed", 0.10, 0.0, 2.0, "cycles/s"),
        _opt("spread", 0.18, 0.0, 2.0, "row-fraction"),
        _opt("lo", 240, 0, 360, "deg", "int"),
        _opt("hi", 360, 0, 360, "deg", "int"),
        _opt("sat", 100, 0, 100, "%", "int"),
        _opt("bri", 80, 0, 100, "%", "int"),
    ],
    "comet": [
        _FPS,
        _opt("speed", 0.5, 0.0, 5.0, "sweeps/s"),
        _opt("hue", 210, 0, 360, "deg", "int"),
        _opt("sat", 100, 0, 100, "%", "int"),
    ],
    "multistop": _MULTISTOP_PARAMS,
    "tri": _MULTISTOP_PARAMS,
}


def parse_opts(args: list[str]) -> tuple[str, dict]:
    """Split effect args into a mode name and a keyword-option dict.

    ``['palette', 'speed=.1', 'spread=.08']`` becomes
    ``('palette', {'speed': 0.1, 'spread': 0.08})``. Values that parse as floats
    are floats; anything else is left as a string. The first arg is the mode
    name and defaults to ``'palette'`` when omitted.
    """
    mode = args[0] if args else "palette"
    opts: dict = {}
    for arg in args[1:]:
        if "=" not in arg:
            continue
        key, value = arg.split("=", 1)
        try:
            opts[key] = float(value)
        except ValueError:
            opts[key] = value
    return mode, opts
