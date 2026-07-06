"""Pure DMX-over-IP core: personalities, the patch model, and DMX->frame maths.

No sockets, asyncio, or BLE live here — the Art-Net / sACN *modules* own the I/O
and call into this. Keeping the conversion + patch logic pure makes it unit-test
like ``frames.py`` (golden bytes in, golden bytes out).

A **patch** assigns a DMX (universe, start address) to a control **target** (a
tube MAC / alias / group / ``all`` — anything ``core.resolve`` understands) with a
**personality** that says how many DMX channels the fixture consumes and what they
mean. DMX slot values are 0-255; the personalities scale them onto the grammar's
ranges (hue 0-359, sat/intensity 0-100, CCT 32-85 hundreds-of-K, GM 0-100).

Rate handling: :class:`RateLimiter` here does only the cheap, always-correct part
— drop a write when the frame is unchanged, and hold to a per-target minimum
interval. On top of that, :class:`WriteGovernor` (#46) is a per-tube BBR-style
adaptive pacer: ``write-without-response`` has no backpressure, so issuing faster
than a tube's *measured* delivery rate piles frames unbounded in BlueZ's
per-connection TX queue. The governor keeps the issue rate at-or-below a
continuously re-probed delivery estimate and tells the send pass to **drop-newest**
(skip a tube for a tick) instead of queueing.
"""
from __future__ import annotations

import asyncio
import colorsys
from collections import deque
from dataclasses import dataclass

from . import frames

#: A full DMX universe is 512 slots (1-based addressing: channel 1 == slots[0]).
UNIVERSE_SIZE = 512

#: 16-bit max, for the two-byte (fine) hue channel.
_U16_MAX = 65535


def _scale(dmx: int, hi: int) -> int:
    """Scale a 0-255 DMX value onto 0..``hi`` (rounded)."""
    return round(max(0, min(255, dmx)) * hi / 255)


def _hsi_frame(slots: list[int], mac6: bytes | None = None) -> bytes:
    """HSI personality (4 ch): hue MSB, hue LSB, saturation, intensity.

    Hue is 16-bit (two channels) so a slow console fade doesn't visibly step; the
    other two are plain 0-255 -> 0-100.

    ``mac6`` is accepted (so every builder shares one signature) but ignored — this
    is a *direct* frame that works on every fixture without addressing.
    """
    hue16 = (slots[0] << 8) | slots[1]
    hue = round(hue16 * 359 / _U16_MAX)
    return frames.hsi(hue, _scale(slots[2], 100), _scale(slots[3], 100))


def _cct_frame(slots: list[int], mac6: bytes | None = None) -> bytes:
    """CCT personality (3 ch): intensity, colour temperature, green/magenta.

    Temp maps 0-255 across the hardware range (3200 K..8500 K); DMX 128 lands
    GM at ~50 (neutral).

    ``mac6`` is accepted for signature parity but ignored (direct frame).
    """
    bri = _scale(slots[0], 100)
    temp = frames.CCT_MIN + round(slots[1] * (frames.CCT_MAX - frames.CCT_MIN) / 255)
    return frames.cct(bri, temp, _scale(slots[2], 100))


def _rgb_frame(slots: list[int], mac6: bytes | None = None) -> bytes:
    """RGB personality (3 ch): red, green, blue (each 0-255).

    An RGB Art-Net source (e.g. LedFx, 3 channels/pixel) can't speak our native
    HSI, so we convert. stdlib ``colorsys.rgb_to_hsv`` takes 0..1 floats and hands
    back hue/sat/value in 0..1; we scale onto the grammar's ranges (hue 0-359,
    sat/intensity 0-100) and emit the *direct* 0x86 HSI frame — byte-identical to
    the ``hsi`` personality, so it works on every fixture.

    Falls out naturally: all-zero (0,0,0) -> value 0 -> intensity 0 (black); an
    equal-channel grey/white -> saturation 0 (colourless white).

    Note: RGB's dedicated cold/warm-white can't be expressed in a direct HSI frame
    (that needs the by-MAC 0xA9 RGBCW op — see the ``rgbw`` personality), so mapping
    RGB->HSI is intentional here: a portable direct frame, dedicated-white out of scope.

    ``mac6`` is accepted for signature parity but ignored (direct frame).
    """
    r, g, b = slots[0] / 255, slots[1] / 255, slots[2] / 255
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    hue = round(h * 360) % 360      # colorsys hue is 0..1; 360 wraps to 0
    return frames.hsi(hue, round(s * 100), round(v * 100))


def _rgbw_frame(slots: list[int], mac6: bytes) -> bytes:
    """RGBW personality (4 ch): red, green, blue, white — **by-MAC** (0xA9 RGBCW).

    An RGBW Art-Net source (e.g. LedFx with its white channel enabled, 4 bytes/pixel)
    carries a dedicated white level that a plain HSI/RGB frame can't reach: those only
    mix colour on the RGB emitters. The TL120C's ``0xA9`` RGBCW op drives the physical
    Cold- and Warm-white emitters directly, giving a true high-CRI white — but it
    **embeds the target MAC** (the direct 0xA8 form is dropped by this fixture), so this
    builder *requires* ``mac6`` and the DMX layer must build it per target tube.

    Mapping: ``slots=[R,G,B,W]`` -> ``rgbcw_by_mac(mac6, bri=100, r=R, g=G, b=B, c=W, w=W)``.
    LedFx emits a single white channel, so we drive BOTH the cold (``c``) and warm (``w``)
    emitters from it (``c = w = W``) — a neutral dedicated white, the sensible default
    when the source doesn't distinguish colour temperature. ``bri=100`` (master full)
    because the R/G/B/C/W channel values already carry the per-emitter levels; scaling
    them again with a partial master would just dim the source's own dynamics.
    """
    if mac6 is None:
        raise ValueError("rgbw personality needs a target MAC")
    return frames.rgbcw_by_mac(
        mac6, bri=100,
        r=slots[0], g=slots[1], b=slots[2],
        c=slots[3], w=slots[3],     # one LedFx white -> both cold+warm = neutral white
    )


#: personality name -> (channel footprint, frame builder taking (slots, mac6)).
#: ``mac6`` is passed to every builder; direct personalities ignore it, by-MAC ones
#: (``rgbw``) embed it, so the frame must be built per target tube (see send_tick).
PERSONALITIES: dict[str, tuple[int, "callable"]] = {
    "hsi": (4, _hsi_frame),
    "cct": (3, _cct_frame),
    "rgb": (3, _rgb_frame),
    "rgbw": (4, _rgbw_frame),
}


@dataclass(frozen=True)
class Patch:
    """One DMX patch: a target driven from ``(universe, address)`` via a personality."""

    target: str
    universe: int
    address: int          # 1-based DMX start channel
    personality: str

    @property
    def footprint(self) -> int:
        """Number of DMX channels this patch consumes."""
        return PERSONALITIES[self.personality][0]

    def slots(self, universe_data: list[int]) -> list[int]:
        """Extract this patch's channel slice from a full universe's slot list."""
        start = self.address - 1
        return list(universe_data[start:start + self.footprint])


def slice_to_frame(personality: str, slots: list[int], mac6: bytes | None = None) -> bytes:
    """Build the BLE frame for ``personality`` from its channel ``slots``.

    Short slices (a universe packet that didn't include all our channels) are
    zero-padded to the footprint so we always produce a valid frame.

    ``mac6`` is the target tube's 6-byte MAC. Direct personalities ignore it; by-MAC
    ones (``rgbw``) embed it, so the caller must pass the per-target MAC (and a by-MAC
    personality raises ``ValueError`` if it's missing).
    """
    footprint, builder = PERSONALITIES[personality]
    if len(slots) < footprint:
        slots = list(slots) + [0] * (footprint - len(slots))
    return builder(slots, mac6)


def parse_patch(cfg_patch: dict) -> list[Patch]:
    """Parse a ``[modules.*.patch]`` table into validated :class:`Patch` objects.

    Each entry is ``target = {universe, address, personality}``. Raises
    ``ValueError`` on an unknown personality or an address whose footprint would
    run past channel 512 — a config typo shouldn't silently mis-address a fixture.
    """
    patches: list[Patch] = []
    for target, spec in cfg_patch.items():
        personality = str(spec.get("personality", "hsi"))
        if personality not in PERSONALITIES:
            raise ValueError(
                f"patch {target!r}: unknown personality {personality!r} "
                f"(known: {', '.join(sorted(PERSONALITIES))})"
            )
        address = int(spec.get("address", 1))
        footprint = PERSONALITIES[personality][0]
        if address < 1 or address + footprint - 1 > UNIVERSE_SIZE:
            raise ValueError(
                f"patch {target!r}: address {address} + {footprint} channels "
                f"runs past DMX channel {UNIVERSE_SIZE}"
            )
        patches.append(Patch(target, int(spec.get("universe", 0)), address, personality))
    return patches


class RateLimiter:
    """Decides whether a freshly-computed frame should actually be written.

    Two cheap, always-correct rules: never re-send an unchanged frame (a static
    look costs zero BLE traffic), and never exceed a per-key minimum interval.
    ``key`` is a tube MAC. The clock is injected (``now`` seconds, monotonic) so
    this is testable without real time.

    Deliberately minimal — the global write-budget / round-robin fairness /
    latest-wins coalescing across many fixtures is the deferred part of #24.
    """

    def __init__(self, min_interval: float = 0.04):
        self.min_interval = min_interval
        self._last: dict[str, tuple[bytes, float]] = {}

    def should_send(self, key: str, frame: bytes, now: float) -> bool:
        """True if ``frame`` differs from the last recorded one for ``key`` and the
        minimum interval has elapsed."""
        prev = self._last.get(key)
        if prev is None:
            return True
        prev_frame, prev_ts = prev
        if frame == prev_frame:
            return False
        return (now - prev_ts) >= self.min_interval

    def record(self, key: str, frame: bytes, now: float) -> None:
        """Remember that ``frame`` was written to ``key`` at ``now``."""
        self._last[key] = (frame, now)


# --- BBR-style per-tube write governor (#46) --------------------------------
#
# Tuning constants for :class:`WriteGovernor`. These are internal (the config
# surface is the constructor kwargs); each is here so the *reason* for its value
# survives the code.

#: Token-bucket cap, in tokens. 1.0 = strict pacing with no stored burst: the send
#: pass is drop-newest (a skipped tube resends its freshest frame next tick), so a
#: burst allowance would only let the downstream BlueZ queue form — the very thing
#: the governor exists to prevent.
_TOKEN_CAP = 1.0

#: STARTUP ramp: double the rate every half second (BBR's startup gain is ~2x per
#: RTT; our control loop has no per-write RTT, so a fixed short step stands in).
_STARTUP_GAIN = 2.0
_STARTUP_STEP = 0.5

#: STARTUP exits on "plateau": ``bw`` failed to grow by 25% over 3 consecutive
#: steps (BBR's own full-pipe heuristic: three rounds without 25% growth).
_STARTUP_PLATEAU_GROWTH = 1.25
_STARTUP_PLATEAU_STEPS = 3

#: Delivered-rate measurement: each ``on_delivery`` computes an instantaneous rate
#: over the trailing 2 s of deliveries (short enough to track a probe step, long
#: enough to smooth per-write jitter)...
_BW_MEASURE_WINDOW = 2.0
#: ...and ``bw`` (BtlBw) is the **max** of those samples over a 10 s window, so a
#: momentary stall doesn't crater the estimate but a real ceiling change ages in.
_BW_WINDOW = 10.0

#: ``min_rtt`` (RTprop) is the windowed **min** of RTT samples over 30 s — long,
#: because the true propagation floor changes slowly; ProbeRTT refreshes it anyway.
_RTT_WINDOW = 30.0

#: How long ``mode`` reads PROBE_BW after an additive-increase step before it
#: settles back to CRUISE (cosmetic/observability — control is timer-driven).
_PROBE_SETTLE = 1.0

#: Sustained-deferral congestion signal: >=10 deferrals within 2 s *while the rate
#: is already >=25% above the measured delivery rate*. A deferral alone only proves
#: demand > rate (normal drop-newest shaping at any healthy rate); combined with
#: rate well above ``bw`` it means we are issuing past the measured ceiling and
#: would be backlogging without the bucket.
_DEFER_WINDOW = 2.0
_DEFER_SUSTAIN = 10
_DEFER_RATE_GUARD = 1.25

#: At most one multiplicative decrease per second, so overlapping congestion
#: signals (rtt + backlog + deferrals from one event) don't collapse the rate to
#: ``rate_min`` in a single burst.
_DECREASE_COOLDOWN = 1.0

#: ProbeRTT quiesce rate (writes/s): low enough that the per-connection queue
#: drains and the canary measures an empty-queue RTT, high enough that the light
#: still visibly tracks a moving look during the brief probe window.
_PROBE_RTT_RATE = 2.0


class WriteGovernor:
    """Per-tube adaptive pacer for ``write-without-response`` traffic (BBR-style).

    One instance per tube MAC, held by the send loop. Pure and clock-injected:
    every method takes ``now`` (monotonic seconds), so the whole controller runs
    under a fake clock in tests — no asyncio, no I/O, no BLE.

    The model is BBR-flavoured AIMD. ``bw`` is the *measured* delivery rate
    (BtlBw, a windowed max filter over :meth:`on_delivery` samples); ``min_rtt``
    is the windowed min of RTT samples (RTprop); ``rate`` is the currently allowed
    issue rate, enforced by a token bucket in :meth:`allow`. Modes:

    * **STARTUP** — double ``rate`` every :data:`_STARTUP_STEP` until ``bw``
      plateaus, a congestion signal fires, or ``rate_max`` is hit; then settle to
      CRUISE at ≈ ``bw``.
    * **CRUISE** — steady state; every ``probe_interval`` take one additive-
      increase step (``rate *= increase_factor``) hunting for freed headroom.
    * **PROBE_BW** — the brief post-increase window (observability label).
    * **PROBE_RTT** — every ``probe_rtt_interval``, quiesce to
      :data:`_PROBE_RTT_RATE` for ``probe_rtt_duration`` and *re-baseline*
      ``min_rtt`` from samples taken in that window, so an elevated baseline is
      never mistaken for congestion. Rate is restored afterwards.

    Congestion (any of: an RTT sample > ``min_rtt * rtt_k``, a caller-supplied
    backlog-growing flag via :meth:`on_backlog`, or sustained deferrals while
    issuing well above ``bw``) triggers a **multiplicative decrease to ≈ bw**
    (set-to-BtlBw). Because ``bw`` is itself a continuously re-probed max filter —
    never a stored verdict — a tube that heals sees its deliveries speed up, ``bw``
    rise, and the periodic additive increase ratchet ``rate`` back up: no scar.
    """

    STARTUP = "startup"
    PROBE_BW = "probe_bw"
    CRUISE = "cruise"
    PROBE_RTT = "probe_rtt"

    def __init__(self, *, rate_init: float = 8.0, rate_min: float = 1.0,
                 rate_max: float = 80.0, probe_interval: float = 4.0,
                 increase_factor: float = 1.15, probe_rtt_interval: float = 10.0,
                 probe_rtt_duration: float = 0.25, rtt_k: float = 2.0):
        # Config. rate_init is conservative (a slow tube was measured at ~4/s);
        # rate_max 80/s is comfortably above the fastest single tube seen (~40/s)
        # while still bounding a runaway probe. increase_factor +15% per 4 s probe
        # and rtt_k≈2 come straight from the plan (§Design.1).
        self.rate_min = float(rate_min)
        self.rate_max = float(rate_max)
        self.probe_interval = float(probe_interval)
        self.increase_factor = float(increase_factor)
        self.probe_rtt_interval = float(probe_rtt_interval)
        self.probe_rtt_duration = float(probe_rtt_duration)
        self.rtt_k = float(rtt_k)

        # Control state.
        self.rate = self._clamp(float(rate_init))   # allowed issue rate, writes/s
        self.bw = 0.0                               # BtlBw estimate, writes/s (0 = unknown)
        self.min_rtt: float | None = None           # RTprop estimate, seconds
        self.mode = self.STARTUP
        self.tokens = 1.0                           # first write goes out immediately
        self.sent = 0                               # allow() granted
        self.deferred = 0                           # allow() refused (drop-newest)

        # Filter windows (small deques; evicted on every event).
        self._delivery_times: deque[float] = deque()        # raw delivery stamps
        self._bw_samples: deque[tuple[float, float]] = deque()   # (t, rate sample)
        self._rtt_samples: deque[tuple[float, float]] = deque()  # (t, rtt sample)
        self._defer_times: deque[float] = deque()            # recent deferrals

        # Timers. None = not armed yet; the first event arms them (the governor
        # has no idea what "now" is until a caller tells it).
        self._last_refill: float | None = None
        self._next_startup_step: float | None = None
        self._next_probe_rtt: float | None = None
        self._next_probe_bw = 0.0        # set on every STARTUP exit / decrease
        self._probe_rtt_end = 0.0
        self._saved_rate = self.rate     # rate to restore after PROBE_RTT
        self._last_increase = float("-inf")
        self._last_decrease = float("-inf")
        self._bw_at_last_step = 0.0      # bw at the previous STARTUP step
        self._plateau_steps = 0          # consecutive no-growth STARTUP steps

    # -- public API --------------------------------------------------------
    def allow(self, now: float) -> bool:
        """Refill the token bucket at ``rate`` and try to take one token.

        True: the caller may issue one write now (counted in ``sent``). False:
        the tube is at its paced ceiling — the caller must **skip it this tick**
        (drop-newest, counted in ``deferred``), never queue.
        """
        self._advance(now)
        if self._last_refill is None:
            self._last_refill = now
        elapsed = max(0.0, now - self._last_refill)     # tolerate a clock hiccup
        self._last_refill = now
        self.tokens = min(_TOKEN_CAP, self.tokens + elapsed * self.rate)
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            self.sent += 1
            return True
        self.deferred += 1
        self._defer_times.append(now)
        while self._defer_times and now - self._defer_times[0] > _DEFER_WINDOW:
            self._defer_times.popleft()
        # Sustained deferrals only count as congestion when we are also issuing
        # well above the measured delivery rate (see _DEFER_RATE_GUARD comment)
        # AND a full measure window has passed since the last rate increase —
        # right after a probe step the delivered-rate estimate necessarily lags
        # the new rate, and clamping on that lag would undo every probe (the
        # governor would converge to its scar instead of climbing out of it).
        if (len(self._defer_times) >= _DEFER_SUSTAIN
                and self.bw > 0 and self.rate > self.bw * _DEFER_RATE_GUARD
                and now - self._last_increase >= _BW_MEASURE_WINDOW):
            self._decrease(now)
        return False

    def on_delivery(self, now: float, rtt: float | None = None) -> None:
        """Record a delivery (and optionally a round-trip) sample.

        This is how BtlBw / RTprop are learned: each call stamps one delivered
        write; the instantaneous delivered rate over the trailing
        :data:`_BW_MEASURE_WINDOW` becomes a ``bw`` sample (max-filtered over
        :data:`_BW_WINDOW`). An ``rtt`` (seconds, e.g. from the notify canary)
        min-filters into ``min_rtt``; a sample above ``min_rtt * rtt_k`` is the
        queue-building congestion signal.
        """
        self._advance(now)
        self._delivery_times.append(now)
        while self._delivery_times and now - self._delivery_times[0] > _BW_MEASURE_WINDOW:
            self._delivery_times.popleft()
        if len(self._delivery_times) >= 2:
            span = self._delivery_times[-1] - self._delivery_times[0]
            if span > 0:
                self._bw_samples.append((now, (len(self._delivery_times) - 1) / span))
                self._refresh_bw(now)
        if rtt is not None:
            self._rtt_samples.append((now, float(rtt)))
            self._refresh_min_rtt(now)
            # Quiesced PROBE_RTT samples are the baseline itself, never congestion.
            if (self.mode != self.PROBE_RTT and self.min_rtt is not None
                    and rtt > self.min_rtt * self.rtt_k):
                self._decrease(now)

    def on_backlog(self, now: float, growing: bool) -> None:
        """Caller-supplied backlog signal (e.g. a rolling issued−delivered estimate).

        ``growing=True`` is a congestion signal (multiplicative decrease toward
        ``bw``); ``False`` is a no-op — the governor never *raises* the rate on
        external say-so, only via its own probing.
        """
        self._advance(now)
        if growing:
            self._decrease(now)

    def stats(self) -> dict:
        """A telemetry snapshot (for the daemon's perf log / status surfaces)."""
        return {"rate": self.rate, "bw": self.bw, "min_rtt": self.min_rtt,
                "mode": self.mode, "sent": self.sent, "deferred": self.deferred}

    # -- internals -----------------------------------------------------------
    def _clamp(self, rate: float) -> float:
        """Bound a candidate rate to the configured ``[rate_min, rate_max]``."""
        return max(self.rate_min, min(self.rate_max, rate))

    def _refresh_bw(self, now: float) -> None:
        """Evict aged bw samples and recompute the windowed max.

        An empty window keeps the last known ``bw`` rather than zeroing it: a
        static scene simply produces no samples, and a stale-but-real estimate is
        a far better decrease target than 0 (which would read as "unknown").
        """
        while self._bw_samples and now - self._bw_samples[0][0] > _BW_WINDOW:
            self._bw_samples.popleft()
        if self._bw_samples:
            self.bw = max(sample for _, sample in self._bw_samples)

    def _refresh_min_rtt(self, now: float) -> None:
        """Evict aged rtt samples and recompute the windowed min (kept if empty)."""
        while self._rtt_samples and now - self._rtt_samples[0][0] > _RTT_WINDOW:
            self._rtt_samples.popleft()
        if self._rtt_samples:
            self.min_rtt = min(sample for _, sample in self._rtt_samples)

    def _advance(self, now: float) -> None:
        """Drive the timer-based mode machine. Called at the top of every event."""
        if self._next_startup_step is None:
            self._next_startup_step = now + _STARTUP_STEP
        if self._next_probe_rtt is None:
            self._next_probe_rtt = now + self.probe_rtt_interval
        self._refresh_bw(now)
        self._refresh_min_rtt(now)

        if self.mode == self.PROBE_RTT:
            if now >= self._probe_rtt_end:
                # Quiesce over: restore the pre-probe rate and resume cruising.
                self.rate = self._clamp(self._saved_rate)
                self.mode = self.CRUISE
                self._next_probe_rtt = now + self.probe_rtt_interval
                self._next_probe_bw = now + self.probe_interval
            return

        if self.mode != self.STARTUP and now >= self._next_probe_rtt:
            # Enter ProbeRTT: drop to the quiesce rate and *forget* the old rtt
            # window, so the samples taken while the queue is drained re-baseline
            # min_rtt (it may legitimately move UP after an interval renegotiation).
            self._saved_rate = self.rate
            self.rate = self._clamp(min(self.rate, _PROBE_RTT_RATE))
            self._rtt_samples.clear()
            self.mode = self.PROBE_RTT
            self._probe_rtt_end = now + self.probe_rtt_duration
            return

        if self.mode == self.STARTUP:
            if now >= self._next_startup_step:
                # BBR full-pipe check: did bw grow >=25% since the last step?
                if self.bw > 0 and self.bw < self._bw_at_last_step * _STARTUP_PLATEAU_GROWTH:
                    self._plateau_steps += 1
                else:
                    self._plateau_steps = 0
                self._bw_at_last_step = self.bw
                if self._plateau_steps >= _STARTUP_PLATEAU_STEPS:
                    self._exit_startup(now)     # pipe full: settle at ~bw
                else:
                    self.rate = self._clamp(self.rate * _STARTUP_GAIN)
                    self._last_increase = now       # arms the deferral grace period
                    self._next_startup_step = now + _STARTUP_STEP
                    if self.rate >= self.rate_max:
                        self._exit_startup(now)  # hit the ceiling; nothing to ramp
            return

        # CRUISE / PROBE_BW: periodic additive-increase probing for headroom.
        if now >= self._next_probe_bw:
            self.rate = self._clamp(self.rate * self.increase_factor)
            self.mode = self.PROBE_BW
            self._last_increase = now
            self._next_probe_bw = now + self.probe_interval
        elif self.mode == self.PROBE_BW and now - self._last_increase >= _PROBE_SETTLE:
            self.mode = self.CRUISE

    def _exit_startup(self, now: float) -> None:
        """Leave STARTUP for CRUISE, settling the rate at ≈ the measured bw."""
        self.mode = self.CRUISE
        if self.bw > 0:
            self.rate = self._clamp(self.bw)
        self._next_probe_bw = now + self.probe_interval

    def _decrease(self, now: float) -> None:
        """Multiplicative decrease toward ``bw`` (set-to-BtlBw), rate-limited.

        With a bw estimate the new rate is ``min(rate, bw)`` — never below the
        measured delivery rate, which is what makes the decrease scar-free. With
        no estimate yet, fall back to classic halving. During PROBE_RTT the
        decrease applies to the *saved* rate (the quiesce rate is already low and
        must not become the restored cruise rate).
        """
        if now - self._last_decrease < _DECREASE_COOLDOWN:
            return                       # one decrease per burst of signals
        self._last_decrease = now
        current = self._saved_rate if self.mode == self.PROBE_RTT else self.rate
        target = self.bw if self.bw > 0 else current * 0.5
        new_rate = self._clamp(min(current, target))
        if self.mode == self.PROBE_RTT:
            self._saved_rate = new_rate
            return
        self.rate = new_rate
        self.mode = self.CRUISE          # also exits STARTUP on first congestion
        self._next_probe_bw = now + self.probe_interval   # back off before re-probing


class GovernorBook(dict):
    """``{mac: WriteGovernor}`` that auto-creates a governor per tube on first use.

    All governors share the constructor kwargs (the config knobs); the send pass
    just indexes ``governors[mac]`` and a new tube gets a fresh controller. It is
    a plain dict otherwise, so telemetry can iterate ``items()``.
    """

    def __init__(self, **governor_kwargs):
        super().__init__()
        self._kwargs = governor_kwargs

    def __missing__(self, mac: str) -> WriteGovernor:
        governor = WriteGovernor(**self._kwargs)
        self[mac] = governor
        return governor


#: ``[modules.artnet]`` / ``[modules.sacn]`` config knob -> WriteGovernor kwarg.
#: Every knob is optional — the governor auto-tunes with zero config (that is the
#: point of BBR-style probing); these exist for rigs that need to pin the bounds.
_GOVERNOR_KNOBS = {
    "rate_min": "rate_min",                     # writes/s floor
    "rate_max": "rate_max",                     # writes/s ceiling
    "probe_interval": "probe_interval",         # s between ProbeBW increase steps
    "probe_rtt_interval": "probe_rtt_interval",  # s between ProbeRTT quiesces
    "increase_factor": "increase_factor",       # ProbeBW step multiplier
    "rtt_congestion_k": "rtt_k",                # rtt > min_rtt*K = congestion
}


def governors_from_cfg(cfg: dict) -> GovernorBook:
    """Build a :class:`GovernorBook` from a DMX module's config table.

    Reads only the knobs in :data:`_GOVERNOR_KNOBS`; anything absent keeps the
    governor's auto-tuning default, so an empty ``cfg`` is fully valid.
    """
    kwargs = {kwarg: float(cfg[knob])
              for knob, kwarg in _GOVERNOR_KNOBS.items() if knob in cfg}
    return GovernorBook(**kwargs)


def _mac6_or_none(mac: str) -> bytes | None:
    """Best-effort ``mac`` string -> 6 bytes; ``None`` if it isn't a canonical MAC.

    ``core.resolve`` hands back real colon-separated MACs, so by-MAC personalities
    (``rgbw``) get their bytes. Direct personalities ignore ``mac6`` entirely, so a
    non-canonical resolver value (e.g. a test placeholder) mustn't blow up the send
    pass — it just yields ``None`` and a direct frame builds fine. A by-MAC builder
    handed ``None`` still raises, as it must.
    """
    try:
        return frames.mac_bytes(mac)
    except ValueError:
        return None


async def send_tick(core, patches, latest, limiter, state, now, governors=None):
    """Run one coalescing send pass; return the ``(mac, frame)`` writes performed.

    Shared by every DMX-over-IP front-end (``modules/artnet``, ``modules/sacn``):
    each receive side only stashes the latest slots per universe in ``latest``, and
    this pass turns that into throttled BLE writes. For each patch that has pending
    universe data, it builds the frame, resolves the target to connected MACs, and
    writes each MAC the rate-limiter allows.

    ``governors`` (optional) is a per-MAC :class:`WriteGovernor` mapping — pass a
    :class:`GovernorBook` and each tube gets its own adaptive pacer. A changed
    frame the governor refuses is **dropped-newest**: the tube is simply skipped
    this tick (counted in ``governor.deferred``), never queued — the next tick
    rebuilds from the freshest ``latest``, so latency stays bounded at one tick.
    Every successful write feeds ``governor.on_delivery(now)``: the issued rate is
    the always-available delivery estimate that seeds/updates ``bw`` (the bounded
    write timeout in the fleet drops half-open links, so a write that returned
    True really was handed off; the RTT canary refines this signal separately).
    With ``governors=None`` the behaviour is exactly the pre-governor one.

    The first DMX that reaches a live fixture cancels any running effect once
    (``state['owning']``), so manual/animation control yields to the console. Goes
    through ``core.resolve`` / ``core.write`` (not ``core.dispatch``) so a ~30 Hz
    stream doesn't re-parse a command string and cancel effects per packet — ``core``
    stays the sole BLE owner. Returns the writes so a send loop can log and tests can
    assert.
    """
    # First pass (no BLE I/O): decide which (mac, frame) writes this tick performs.
    # Gate order matters: change-detection first (an unchanged frame is free and
    # must not consume a pacing token), then the governor's token bucket.
    todo = []
    for patch in patches:
        universe_data = latest.get(patch.universe)
        if universe_data is None:
            continue
        slots = patch.slots(universe_data)
        macs = core.resolve(patch.target)
        if macs and not state.get("owning"):
            await core.cancel_effect()
            state["owning"] = True
        for mac in macs:
            # Build the frame per-MAC: by-MAC personalities (rgbw) embed the target's
            # own MAC, so each tube gets a distinct frame. Direct personalities ignore
            # mac6, so their per-mac frame is byte-identical to the old shared one.
            frame = slice_to_frame(patch.personality, slots, _mac6_or_none(mac))
            if not limiter.should_send(mac, frame, now):
                continue
            if governors is not None and not governors[mac].allow(now):
                continue    # drop-newest: skip this tube this tick, do NOT queue
            todo.append((mac, frame))
    if not todo:
        return []

    # Fan the writes out CONCURRENTLY. On a single BLE adapter, awaiting each write
    # in turn serializes the whole fleet behind every write's D-Bus/controller
    # latency, capping the per-tube rate far below the limiter's ceiling — the
    # "transitions lagging / falling behind" symptom on a multi-tube rig. These are
    # write-without-response (no per-write ACK round-trip), so issuing them together
    # lets BlueZ pipeline them and the per-tick cost drops to ~the slowest single
    # write. gather preserves order, so results line up with todo.
    results = await asyncio.gather(
        *(core.write(mac, frame) for mac, frame in todo),
        return_exceptions=True,
    )
    written = []
    for (mac, frame), ok in zip(todo, results):
        # a failed/raised write isn't recorded, so it retries next tick (the
        # governor token it consumed is forfeit — a failed write still occupied
        # the adapter, so charging for it is the honest accounting)
        if ok is True:
            limiter.record(mac, frame, now)
            if governors is not None:
                governors[mac].on_delivery(now)     # issued-rate delivery sample
            written.append((mac, frame))
    return written
