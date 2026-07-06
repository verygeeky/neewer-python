"""Tests for :class:`neewer.protocol.dmx.WriteGovernor` — the BBR-style pacer (#46).

The governor is pure and clock-injected: every test drives it with a fake clock
(monotonically increasing ``now`` floats) and injected delivery/RTT samples. No
asyncio, no BLE, no real time.

Covered: token-bucket math, STARTUP ramp + plateau exit, additive increase while
healthy (ProbeBW), multiplicative decrease on each distinct congestion signal
(rtt inflation / backlog flag / sustained deferrals), the decrease cooldown,
ProbeRTT quiesce + min_rtt re-baseline, deferred counting, clamping, the
GovernorBook, and — critically — the **anti-scar property**: a governor forced
to a low rate climbs back up on healthy samples alone.
"""
from __future__ import annotations

from neewer.protocol import dmx
from neewer.protocol.dmx import GovernorBook, WriteGovernor


def make(**kw) -> WriteGovernor:
    """A governor with test-friendly defaults (overridable per test)."""
    defaults = dict(rate_init=10.0, rate_min=1.0, rate_max=80.0,
                    probe_interval=4.0, increase_factor=1.15,
                    probe_rtt_interval=10.0, probe_rtt_duration=0.25, rtt_k=2.0)
    defaults.update(kw)
    return WriteGovernor(**defaults)


def drive_healthy(gov: WriteGovernor, start: float, duration: float,
                  demand_hz: float = 120.0, rtt: float | None = None) -> float:
    """Simulate a healthy tube: demand at ``demand_hz``; every allowed write is
    delivered immediately (the delivered rate tracks the issue rate). Returns the
    clock value after ``duration`` seconds.

    ``demand_hz`` is deliberately fine-grained (120 Hz): grants quantize to
    ``demand_hz / k`` (one write every k-th ask), and a coarse demand clock would
    make the *simulation's* quantization steps wider than the governor's 1.25x
    deferral guard — a stall that is an artifact of grant-synchronized delivery,
    not of the controller (real deliveries are not tick-aligned)."""
    step = 1.0 / demand_hz
    now = start
    end = start + duration
    while now < end:
        if gov.allow(now):
            gov.on_delivery(now, rtt)
        now += step
    return now


# --- token-bucket math ------------------------------------------------------

def test_first_allow_is_immediate():
    gov = make()
    assert gov.allow(0.0) is True           # starts with one full token
    assert gov.sent == 1 and gov.deferred == 0


def test_second_allow_same_instant_is_deferred():
    gov = make()
    assert gov.allow(0.0) is True
    assert gov.allow(0.0) is False          # no time elapsed -> no refill
    assert gov.sent == 1 and gov.deferred == 1


def test_refill_is_proportional_to_rate_and_elapsed():
    gov = make(rate_init=10.0)              # 10 tokens/s -> 1 token per 0.1 s
    assert gov.allow(0.0) is True
    assert gov.allow(0.05) is False         # only half a token accrued
    assert gov.allow(0.11) is True          # >0.1 s since the granted write


def test_tokens_cap_prevents_stored_bursts():
    gov = make(rate_init=10.0)
    assert gov.allow(0.0) is True
    # A long idle gap must NOT bank a burst: cap is 1 token.
    assert gov.allow(5.0) is True
    assert gov.allow(5.0) is False          # no second banked token
    assert gov.allow(5.001) is False        # and no near-instant refill either


def test_clock_going_backwards_does_not_refill():
    gov = make(rate_init=10.0)
    assert gov.allow(1.0) is True
    assert gov.allow(0.5) is False          # negative elapsed is clamped to 0


def test_deferred_counter_accumulates():
    gov = make(rate_init=1.0)               # 1 write/s
    assert gov.allow(0.0) is True
    for i in range(5):
        assert gov.allow(0.01 * (i + 1)) is False
    assert gov.deferred == 5 and gov.sent == 1


# --- rate clamping ------------------------------------------------------------

def test_rate_init_is_clamped_into_bounds():
    assert make(rate_init=500.0, rate_max=40.0).rate == 40.0
    assert make(rate_init=0.1, rate_min=2.0).rate == 2.0


def test_startup_ramp_never_exceeds_rate_max():
    gov = make(rate_init=10.0, rate_max=30.0)
    gov.allow(0.0)
    drive_healthy(gov, 0.0, 10.0)
    assert gov.rate <= 30.0


def test_decrease_never_goes_below_rate_min():
    gov = make(rate_init=10.0, rate_min=4.0)
    # No bw estimate -> each congestion halves; the floor must hold.
    for i in range(10):
        gov.on_backlog(float(i * 2), growing=True)
    assert gov.rate == 4.0


# --- STARTUP ------------------------------------------------------------------

def test_startup_doubles_rate_per_step():
    gov = make(rate_init=8.0)
    assert gov.mode == WriteGovernor.STARTUP
    gov.allow(0.0)                          # arms the timers
    gov.allow(0.6)                          # past the 0.5 s startup step
    assert gov.rate == 16.0                 # 8 -> 16 (one doubling)
    gov.allow(1.2)
    assert gov.rate == 32.0                 # -> 32


def test_startup_exits_at_rate_max():
    gov = make(rate_init=8.0, rate_max=20.0)
    gov.allow(0.0)
    gov.allow(0.6)                          # 8*2 = 16
    gov.allow(1.2)                          # 16*2 clamped to 20 -> ceiling hit
    assert gov.rate == 20.0
    assert gov.mode == WriteGovernor.CRUISE


def test_startup_exits_on_bw_plateau_and_settles_at_bw():
    # Deliveries arrive at a fixed 5/s no matter how high the ramp pushes the
    # rate — bw stops growing, so after 3 flat steps STARTUP must exit at ~bw.
    gov = make(rate_init=8.0)
    now = 0.0
    while gov.mode == WriteGovernor.STARTUP and now < 20.0:
        gov.allow(now)
        gov.on_delivery(now)                # one delivery per 0.2 s = 5/s
        now += 0.2
    assert gov.mode == WriteGovernor.CRUISE
    assert gov.rate < 8.0 * 2 ** 4          # did NOT ramp unbounded
    assert abs(gov.rate - gov.bw) / gov.bw < 0.35   # settled near the measured bw


def test_startup_exits_on_first_congestion():
    gov = make(rate_init=8.0)
    gov.allow(0.0)
    gov.on_backlog(0.1, growing=True)
    assert gov.mode == WriteGovernor.CRUISE


# --- bw / min_rtt estimation ---------------------------------------------------

def test_bw_tracks_delivery_rate():
    gov = make()
    # 20 deliveries spaced 0.1 s apart = 10/s.
    for i in range(20):
        gov.on_delivery(i * 0.1)
    assert abs(gov.bw - 10.0) < 1.0


def test_bw_is_max_filtered_over_window():
    gov = make()
    for i in range(20):
        gov.on_delivery(i * 0.05)           # a fast burst: 20/s
    fast = gov.bw
    for i in range(5):
        gov.on_delivery(1.0 + i * 0.5)      # then a slow trickle: 2/s
    # Within the 10 s window the max filter still remembers the fast burst.
    assert gov.bw == fast


def test_bw_window_ages_out_old_maximum():
    gov = make()
    for i in range(20):
        gov.on_delivery(i * 0.05)           # fast burst around t=0..1 (20/s)
    # Much later, sustained slow deliveries: the old max has aged out (>10 s).
    for i in range(30):
        gov.on_delivery(30.0 + i * 0.5)     # 2/s
    assert gov.bw < 5.0


def test_min_rtt_is_windowed_min():
    gov = make()
    gov.on_delivery(0.0, rtt=0.08)
    gov.on_delivery(0.5, rtt=0.03)
    gov.on_delivery(1.0, rtt=0.06)
    assert gov.min_rtt == 0.03


# --- additive increase (ProbeBW) ------------------------------------------------

def test_cruise_probes_rate_up_periodically():
    gov = make(rate_init=8.0, rate_max=20.0)
    gov.allow(0.0)
    gov.allow(0.6)                          # startup: 16
    gov.allow(1.2)                          # startup: clamped 20 -> CRUISE
    assert gov.mode == WriteGovernor.CRUISE
    gov.rate = 10.0                         # give headroom below the clamp
    gov.allow(1.2 + 4.1)                    # past probe_interval -> +15%
    assert abs(gov.rate - 10.0 * 1.15) < 1e-9
    assert gov.mode == WriteGovernor.PROBE_BW


def test_probe_bw_mode_settles_back_to_cruise():
    gov = make(rate_init=8.0, rate_max=20.0)
    gov.allow(0.0)
    gov.allow(0.6)
    gov.allow(1.2)                          # CRUISE at the 20 cap
    gov.rate = 10.0
    gov.allow(5.4)                          # probe step -> PROBE_BW
    assert gov.mode == WriteGovernor.PROBE_BW
    gov.allow(6.6)                          # >1 s after the step, no congestion
    assert gov.mode == WriteGovernor.CRUISE


# --- multiplicative decrease: each congestion signal ----------------------------

def _cruising_gov(rate: float = 40.0, bw_rate: float = 10.0) -> tuple[WriteGovernor, float]:
    """A governor in CRUISE with rate ``rate`` and a seeded bw of ``bw_rate``."""
    gov = make(rate_init=8.0)
    now = 0.0
    for i in range(40):                     # seed bw at bw_rate deliveries/s
        now = i * (1.0 / bw_rate)
        gov.on_delivery(now)
    gov.on_backlog(now, growing=True)       # force STARTUP -> CRUISE (one decrease)
    gov.rate = rate                         # then place the rate above bw
    now += 2.0                              # clear the decrease cooldown
    return gov, now


def test_rtt_inflation_decreases_rate_to_bw():
    gov, now = _cruising_gov(rate=40.0, bw_rate=10.0)
    gov.on_delivery(now, rtt=0.05)          # baseline min_rtt
    now += 0.1
    before = gov.rate
    gov.on_delivery(now, rtt=0.05 * 2.5)    # > min_rtt * K(2) -> congestion
    assert gov.rate < before
    assert abs(gov.rate - gov.bw) / gov.bw < 0.2    # set-to-BtlBw
    assert gov.mode == WriteGovernor.CRUISE


def test_rtt_below_threshold_is_not_congestion():
    gov, now = _cruising_gov(rate=40.0, bw_rate=10.0)
    gov.on_delivery(now, rtt=0.05)
    before = gov.rate
    gov.on_delivery(now + 0.1, rtt=0.05 * 1.5)      # inflated but < K=2 -> fine
    assert gov.rate == before


def test_backlog_flag_decreases_rate_to_bw():
    gov, now = _cruising_gov(rate=40.0, bw_rate=10.0)
    gov.on_backlog(now, growing=True)
    assert abs(gov.rate - gov.bw) / gov.bw < 0.2


def test_backlog_not_growing_is_a_noop():
    gov, now = _cruising_gov(rate=40.0, bw_rate=10.0)
    before = gov.rate
    gov.on_backlog(now, growing=False)
    assert gov.rate == before


def test_backlog_without_bw_estimate_halves_rate():
    gov = make(rate_init=16.0)
    gov.allow(0.0)
    gov.on_backlog(0.1, growing=True)       # no deliveries yet -> classic halving
    assert gov.rate == 8.0


def test_sustained_deferrals_decrease_rate_to_bw():
    gov, now = _cruising_gov(rate=40.0, bw_rate=10.0)
    # Hammer allow() far past the bucket: >=10 deferrals inside 2 s while
    # rate (40) >> bw (10) must fire the congestion decrease.
    for i in range(60):
        gov.allow(now + i * 0.01)
    assert abs(gov.rate - gov.bw) / gov.bw < 0.2
    assert gov.deferred > 0


def test_deferrals_at_or_below_bw_do_not_decrease():
    # Deferrals with rate ~= bw are plain demand-shaping, not congestion:
    # the rate must stay put (else any 30 Hz demand would pin every tube).
    gov, now = _cruising_gov(rate=10.0, bw_rate=10.0)
    before = gov.rate
    for i in range(60):
        gov.allow(now + i * 0.01)
    assert gov.rate == before


def test_decrease_cooldown_limits_to_one_per_burst():
    gov = make(rate_init=32.0)
    gov.allow(0.0)
    gov.on_backlog(0.1, growing=True)       # halves: 32 -> 16
    gov.on_backlog(0.2, growing=True)       # inside the 1 s cooldown -> ignored
    gov.on_backlog(0.3, growing=True)       # ignored
    assert gov.rate == 16.0
    gov.on_backlog(1.5, growing=True)       # cooldown over -> halves again
    assert gov.rate == 8.0


# --- ProbeRTT --------------------------------------------------------------------

def test_probe_rtt_quiesces_then_restores_rate():
    gov, now = _cruising_gov(rate=40.0, bw_rate=10.0)
    cruise_rate = gov.rate
    gov.allow(now + 10.1)                   # past probe_rtt_interval -> quiesce
    assert gov.mode == WriteGovernor.PROBE_RTT
    assert gov.rate <= 2.0                  # the quiesce rate
    gov.allow(now + 10.1 + 0.3)             # past probe_rtt_duration -> restore
    assert gov.mode == WriteGovernor.CRUISE
    assert gov.rate == cruise_rate


def test_probe_rtt_rebaselines_min_rtt_upwards():
    gov, now = _cruising_gov(rate=40.0, bw_rate=10.0)
    gov.on_delivery(now, rtt=0.02)          # old (stale-low) baseline
    assert gov.min_rtt == 0.02
    gov.allow(now + 10.1)                   # enter PROBE_RTT (window cleared)
    gov.on_delivery(now + 10.2, rtt=0.05)   # quiesced-link sample
    gov.allow(now + 10.5)                   # exit PROBE_RTT
    # Re-baseline: min_rtt moved UP to the fresh empty-queue measurement.
    assert gov.min_rtt == 0.05


def test_probe_rtt_samples_never_count_as_congestion():
    gov, now = _cruising_gov(rate=40.0, bw_rate=10.0)
    gov.on_delivery(now, rtt=0.02)
    gov.allow(now + 10.1)                   # enter PROBE_RTT
    before_saved = gov._saved_rate
    gov.on_delivery(now + 10.2, rtt=0.2)    # huge rtt DURING the probe window
    assert gov._saved_rate == before_saved  # no decrease was applied
    gov.allow(now + 10.5)                   # restore
    assert gov.rate == before_saved


def test_congestion_during_probe_rtt_decreases_the_saved_rate():
    gov, now = _cruising_gov(rate=40.0, bw_rate=10.0)
    gov.allow(now + 10.1)                   # enter PROBE_RTT (rate quiesced)
    gov.on_backlog(now + 10.2, growing=True)
    gov.allow(now + 10.5)                   # restore
    # The decrease landed on the restored cruise rate, not the quiesce rate.
    assert abs(gov.rate - gov.bw) / gov.bw < 0.2


def test_probe_rtt_does_not_fire_during_startup():
    gov = make(rate_init=8.0, probe_rtt_interval=0.1)
    gov.allow(0.0)
    gov.allow(0.2)                          # past the interval, but still STARTUP
    assert gov.mode == WriteGovernor.STARTUP


# --- the anti-scar property -------------------------------------------------------

def test_anti_scar_rate_recovers_after_a_low_period():
    """THE core guarantee (#46): a tube that was slow must never stay throttled.

    Scar the governor down to a very low rate (a degraded-link episode), then
    feed nothing but healthy samples: the periodic additive increase plus the
    max-filtered bw estimate must ratchet the rate back up — the low period
    leaves no permanent verdict.
    """
    gov = make(rate_init=8.0)
    # Degraded episode: deliveries trickle at 2/s and the backlog signal fires
    # repeatedly (spaced past the cooldown) — the rate collapses toward 2/s.
    now = 0.0
    for i in range(60):
        now = i * 0.5
        gov.on_delivery(now)                # 2/s trickle
        if i % 4 == 0:
            gov.on_backlog(now, growing=True)
    scarred = gov.rate
    assert scarred < 4.0                    # genuinely scarred low

    # Wait out the bw window so the degraded-era estimate ages away, then the
    # tube heals: every allowed write is delivered immediately.
    now = drive_healthy(gov, now + 15.0, 200.0)

    assert gov.rate > scarred * 5           # climbed way off the scar...
    assert gov.rate > 25.0                  # ...back into healthy-tube territory


def test_anti_scar_bw_estimate_itself_recovers():
    gov = make(rate_init=8.0)
    # Slow era: 2/s for 30 s.
    now = 0.0
    for i in range(60):
        now = i * 0.5
        gov.on_delivery(now)
    assert gov.bw < 4.0
    # Healthy era: as the rate probes back up, the max filter follows the faster
    # deliveries — the bw estimate itself carries no memory of the slow era.
    drive_healthy(gov, now + 1.0, 120.0)
    assert gov.bw > 15.0


# --- counters / stats / book -------------------------------------------------------

def test_stats_snapshot_shape():
    gov = make()
    gov.allow(0.0)
    gov.allow(0.0)
    gov.on_delivery(0.1, rtt=0.04)
    snap = gov.stats()
    assert snap["sent"] == 1 and snap["deferred"] == 1
    assert snap["mode"] == gov.mode
    assert snap["min_rtt"] == 0.04
    assert set(snap) == {"rate", "bw", "min_rtt", "mode", "sent", "deferred"}


def test_governor_book_autocreates_per_mac_with_shared_config():
    book = GovernorBook(rate_init=5.0, rate_max=12.0)
    a = book["AA:BB:CC:DD:EE:01"]
    b = book["AA:BB:CC:DD:EE:02"]
    assert a is not b
    assert a is book["AA:BB:CC:DD:EE:01"]   # cached, not recreated
    assert a.rate == 5.0 and a.rate_max == 12.0
    assert set(book) == {"AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"}


def test_module_exports():
    assert hasattr(dmx, "WriteGovernor") and hasattr(dmx, "GovernorBook")


# --- governors_from_cfg (the [modules.artnet]/[modules.sacn] knob surface) ------

def test_governors_from_cfg_zero_config_uses_defaults():
    book = dmx.governors_from_cfg({})
    gov = book["AA:BB:CC:DD:EE:01"]
    ref = WriteGovernor()
    assert (gov.rate_min, gov.rate_max, gov.probe_interval, gov.rtt_k) == \
        (ref.rate_min, ref.rate_max, ref.probe_interval, ref.rtt_k)


def test_governors_from_cfg_maps_every_knob():
    book = dmx.governors_from_cfg({
        "rate_min": 2, "rate_max": 40, "probe_interval": 6,
        "probe_rtt_interval": 20, "increase_factor": 1.3,
        "rtt_congestion_k": 3,          # config spelling -> rtt_k kwarg
        "send_hz": 30.0, "port": 6454,  # unrelated module keys are ignored
    })
    gov = book["AA:BB:CC:DD:EE:01"]
    assert gov.rate_min == 2.0 and gov.rate_max == 40.0
    assert gov.probe_interval == 6.0 and gov.probe_rtt_interval == 20.0
    assert gov.increase_factor == 1.3 and gov.rtt_k == 3.0
