"""Tests for the refactored async machinery in :mod:`neewer.fleet`.

Covers the shared-scan discovery callback, the connection helper's BUG-1 guard
(no wedged-but-dead links), notify handling, ordered shutdown, the effect-swap
invariant, and the read commands (``state`` / ``query``).

No real radio: we inject a :class:`FakeTransport` (the :class:`neewer.transport.Transport`
seam) whose ``connect`` yields controllable fake clients. Nothing imports ``bleak``.
"""
from __future__ import annotations

import asyncio

import pytest

from neewer.errors import UnknownTarget
from neewer.fleet import NeewerCore, Tube
from neewer.protocol import frames
from neewer.transport import Advert


def run(coro):
    return asyncio.run(coro)


# --- fake transport -------------------------------------------------------

class FakeHandle:
    """An opaque connect handle (stands in for a bleak BLEDevice)."""

    def __init__(self, address, name="NW-test", drop_during_connect=False):
        self.address = address
        self.name = name
        #: When true, the transport simulates the link dying *during* connect().
        self.drop_during_connect = drop_during_connect


class FakeClient:
    """A connected client the transport hands back; records writes/notify/disconnect."""

    def __init__(self, handle, on_disconnect=None):
        self.handle = handle
        self.on_disconnect = on_disconnect
        self.connected = False
        self.notify_started = False
        self.on_notify = None
        self.disconnected = False
        self.writes: list[bytes] = []


class FakeTransport:
    """A hardware-free :class:`neewer.transport.Transport` for supervisor tests."""

    def __init__(self):
        self.scanning = False
        self.on_advert = None
        self.clients: list[FakeClient] = []

    async def start_scan(self, on_advert):
        self.scanning = True
        self.on_advert = on_advert

    async def stop_scan(self):
        self.scanning = False

    async def connect(self, handle, on_disconnect):
        client = FakeClient(handle, on_disconnect)
        self.clients.append(client)
        if getattr(handle, "drop_during_connect", False):
            # The BUG-1 wedge: the disconnect callback fires *during* connect,
            # leaving a dead link. The caller must not end up "connected".
            client.connected = False
            on_disconnect()
        else:
            client.connected = True
        return client

    def is_connected(self, client):
        return client.connected

    async def subscribe(self, client, on_notify):
        client.notify_started = True
        client.on_notify = on_notify

    async def write(self, client, data):
        client.writes.append(bytes(data))

    async def disconnect(self, client):
        client.disconnected = True
        client.connected = False


def make_core(**kw) -> NeewerCore:
    """A core wired to a fresh FakeTransport (no radio)."""
    return NeewerCore(transport=FakeTransport(), **kw)


async def _cancel_supervisors(core):
    for task in core._supervisors.values():
        task.cancel()
    for task in core._supervisors.values():
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def _noop_try_connect(tube, handle):
    return None


def _advert(address, name="NW-test", rssi=-50, drop_during_connect=False):
    return Advert(address, name, rssi, FakeHandle(address, name, drop_during_connect))


# --- discovery callback ---------------------------------------------------

def test_on_advert_registers_matching_tube(monkeypatch):
    core = make_core(prefixes=("NW-",), positions={"AA:BB:CC:DD:EE:01": 3})

    async def body():
        # Stub the connect path so the spawned supervisor does nothing real.
        monkeypatch.setattr(core, "_try_connect", _noop_try_connect)
        core._on_advert(_advert("AA:BB:CC:DD:EE:01", "NW-2024"))
        assert "AA:BB:CC:DD:EE:01" in core.tubes
        assert core.tubes["AA:BB:CC:DD:EE:01"].position == 3
        assert "AA:BB:CC:DD:EE:01" in core._devices
        await _cancel_supervisors(core)

    run(body())


def test_on_advert_ignores_non_matching_prefix():
    core = make_core(prefixes=("NW-",))
    core._on_advert(_advert("11:22:33:44:55:66", name="OTHER"))
    assert core.tubes == {}
    assert core._supervisors == {}


def test_on_advert_does_not_duplicate_supervisor(monkeypatch):
    core = make_core(prefixes=("NW-",))

    async def body():
        monkeypatch.setattr(core, "_try_connect", _noop_try_connect)
        core._on_advert(_advert("AA:BB:CC:DD:EE:02"))
        first_task = core._supervisors["AA:BB:CC:DD:EE:02"]
        core._on_advert(_advert("AA:BB:CC:DD:EE:02", rssi=-40))    # second advert, same MAC
        assert core._supervisors["AA:BB:CC:DD:EE:02"] is first_task
        assert len(core.tubes) == 1
        await _cancel_supervisors(core)

    run(body())


# --- _try_connect (BUG-1 guard) ------------------------------------------

def test_try_connect_success_marks_connected_and_subscribes():
    core = make_core()
    tube = Tube("AA:BB:CC:DD:EE:03")
    core.tubes[tube.mac] = tube

    run(core._try_connect(tube, FakeHandle("AA:BB:CC:DD:EE:03")))

    assert tube.connected is True
    assert isinstance(tube.client, FakeClient)
    assert tube.client.notify_started is True


def test_try_connect_drop_during_connect_does_not_wedge():
    """BUG-1: if the link dies during connect(), we must NOT end up 'connected'."""
    core = make_core()
    tube = Tube("AA:BB:CC:DD:EE:04")
    core.tubes[tube.mac] = tube

    run(core._try_connect(tube, FakeHandle("AA:BB:CC:DD:EE:04", drop_during_connect=True)))

    assert tube.connected is False
    assert tube.client is None          # not left pointing at a dead client


# --- notify / drop --------------------------------------------------------

def test_on_notify_updates_tube_state():
    core = make_core()
    tube = Tube("AA:BB:CC:DD:EE:05")
    core.tubes[tube.mac] = tube
    # A battery reply for this MAC.
    mac6 = frames.mac_bytes(tube.mac)
    core._on_notify(tube.mac, bytes([0x78, 0x05, 0x07, *mac6, 0x50]))
    assert tube.state.battery == 80


def test_on_drop_marks_disconnected():
    core = make_core()
    tube = Tube("AA:BB:CC:DD:EE:06")
    tube.connected = True
    tube.client = object()
    core.tubes[tube.mac] = tube
    core._on_drop(tube.mac)
    assert tube.connected is False
    assert tube.client is None


# --- ordered shutdown (BUG-4) --------------------------------------------

def test_stop_cancels_supervisors_stops_scan_and_disconnects():
    core = make_core()

    async def body():
        core._scanning = True
        core.transport.scanning = True
        # A connected tube with a fake client from the transport.
        tube = Tube("AA:BB:CC:DD:EE:07")
        client = await core.transport.connect(FakeHandle(tube.mac), lambda: None)
        tube.client, tube.connected = client, True
        core.tubes[tube.mac] = tube
        # A long-running supervisor that stop() must cancel.
        core._supervisors[tube.mac] = asyncio.create_task(asyncio.sleep(100))

        await core.stop()

        assert core._shutdown is True
        assert core.transport.scanning is False
        assert core._supervisors == {}
        assert client.disconnected is True

    run(body())


# --- effect swap invariant (BUG-2) ---------------------------------------

def test_start_effect_replaces_and_cancels_previous():
    core = make_core()
    tube = Tube("AA:BB:CC:DD:EE:08", position=1)
    tube.client = FakeClient(FakeHandle(tube.mac))
    tube.connected = True
    core.tubes[tube.mac] = tube

    async def body():
        await core.start_effect("hue", {})
        first = core._effect_task
        assert first is not None
        await core.start_effect("comet", {})
        second = core._effect_task
        assert second is not first
        # Previous effect task is no longer the active one and gets cancelled.
        await asyncio.sleep(0)
        assert first.cancelled() or first.done()
        await core.cancel_effect()
        assert core._effect_task is None

    run(body())


# --- read commands --------------------------------------------------------

def _connected_core():
    core = make_core()
    tube = Tube("AA:BB:CC:DD:EE:01", name="NW-20240047", position=1)
    tube.client = FakeClient(FakeHandle(tube.mac))
    tube.connected = True
    core.tubes[tube.mac] = tube
    return core, tube


def test_dispatch_state_returns_json_snapshot():
    core, tube = _connected_core()
    tube.state.battery = 80
    import json
    out = json.loads(run(core.dispatch("state")))
    assert out["AA:BB:CC:DD:EE:01"]["battery"] == 80
    assert out["AA:BB:CC:DD:EE:01"]["connected"] is True


def test_dispatch_query_sends_battery_state_version_frames():
    core, tube = _connected_core()
    result = run(core.dispatch("query"))
    assert "1 tube(s)" in result
    ops = [frame[1] for frame in tube.client.writes]
    # battery 0x95, state 0x8E, version-by-mac 0x9E, in that order.
    assert ops == [frames.OP_BATTERY, frames.OP_STATE_MAC, frames.OP_VERSION_MAC]


def test_dispatch_state_filtered_by_target():
    core, _ = _connected_core()
    # A second tube that should be filtered out when we ask for t1.
    other = Tube("AA:AA:AA:AA:AA:AA", name="NW-other", position=2)
    other.client = FakeClient(FakeHandle(other.mac))
    other.connected = True
    core.tubes[other.mac] = other

    import json
    out = json.loads(run(core.dispatch("state t1")))
    assert list(out.keys()) == ["AA:BB:CC:DD:EE:01"]


def test_dispatch_query_no_tubes():
    core = make_core()
    with pytest.raises(UnknownTarget):
        run(core.dispatch("query t9"))


# --- change events --------------------------------------------------------

def test_subscribe_fires_on_connect_notify_command_and_drop():
    core = make_core()
    events = []
    unsub = core.subscribe(lambda: events.append(1))

    tube = Tube("AA:BB:CC:DD:EE:0A")
    core.tubes[tube.mac] = tube
    run(core._try_connect(tube, FakeHandle(tube.mac)))       # connect
    assert len(events) == 1
    core._on_notify(tube.mac, bytes([0x78, 0x05, 0x07, *frames.mac_bytes(tube.mac), 0x50]))
    assert len(events) == 2                                  # telemetry notify
    run(core.power(tube.mac, True))                          # a command
    assert len(events) == 3
    core._on_drop(tube.mac)                                  # disconnect
    assert len(events) == 4

    unsub()
    core._on_drop(tube.mac)
    assert len(events) == 4                                  # no more after unsubscribe


def test_subscribe_error_is_swallowed():
    core = make_core()
    core.subscribe(lambda: (_ for _ in ()).throw(RuntimeError("bad subscriber")))
    ok = []
    core.subscribe(lambda: ok.append(1))                    # a healthy one still fires

    tube = Tube("AA:BB:CC:DD:EE:0B")
    core.tubes[tube.mac] = tube
    run(core._try_connect(tube, FakeHandle(tube.mac)))       # must not raise
    assert ok == [1]


# --- reconnect backoff (#6) ----------------------------------------------

def test_next_backoff_doubles_and_caps():
    from neewer.fleet import _SUPERVISE_RETRY_MAX, _next_backoff

    assert _next_backoff(4.0) == 8.0
    assert _next_backoff(20.0) == 40.0
    assert _next_backoff(40.0) == _SUPERVISE_RETRY_MAX      # 80 -> capped at 60
    assert _next_backoff(_SUPERVISE_RETRY_MAX) == _SUPERVISE_RETRY_MAX


def test_retry_delay_is_jittered_within_window_and_capped(monkeypatch):
    from neewer import fleet as fmod

    # No jitter -> the base of the window.
    monkeypatch.setattr(fmod.random, "uniform", lambda a, b: 0.0)
    assert fmod._retry_delay(4.0) == 4.0
    # Max jitter -> just under 2x the backoff.
    monkeypatch.setattr(fmod.random, "uniform", lambda a, b: b)
    assert fmod._retry_delay(4.0) == 8.0
    # Even at max jitter the delay never exceeds the cap.
    assert fmod._retry_delay(fmod._SUPERVISE_RETRY_MAX) == fmod._SUPERVISE_RETRY_MAX


# --- canary round-trip (#46) -----------------------------------------------

def _battery_reply(mac: str) -> bytes:
    """A decodable by-MAC battery notify for ``mac`` (any reply stamps the tube)."""
    return bytes([0x78, 0x05, 0x07, *frames.mac_bytes(mac), 0x50])


def test_on_notify_stamps_last_reply_at():
    core = make_core()
    tube = Tube("AA:BB:CC:DD:EE:0C")
    core.tubes[tube.mac] = tube
    assert tube.state.last_reply_at is None
    core._on_notify(tube.mac, _battery_reply(tube.mac))
    first = tube.state.last_reply_at
    assert first is not None
    core._on_notify(tube.mac, _battery_reply(tube.mac))
    assert tube.state.last_reply_at >= first        # monotonic, advances per notify


def test_last_reply_at_stays_out_of_the_snapshot():
    # A monotonic float is meaningless to status consumers; the wire shape of
    # as_dict()/snapshot() must not grow it.
    core = make_core()
    tube = Tube("AA:BB:CC:DD:EE:0D")
    core.tubes[tube.mac] = tube
    core._on_notify(tube.mac, _battery_reply(tube.mac))
    assert "last_reply_at" not in core.snapshot()[tube.mac]


def test_canary_measures_a_round_trip():
    core, tube = _connected_core()

    async def body():
        task = asyncio.create_task(core.canary(tube.mac, timeout=1.0))
        await asyncio.sleep(0)                      # let the canary issue its query
        # The query frame went out (state query 0x8E, MAC-addressed).
        assert tube.client.writes[-1][1] == frames.OP_STATE_MAC
        # The light "replies" on the notify path.
        core._on_notify(tube.mac, _battery_reply(tube.mac))
        rtt = await task
        assert rtt is not None and 0 <= rtt < 1.0

    run(body())


def test_canary_times_out_when_no_reply():
    core, tube = _connected_core()

    async def body():
        return await core.canary(tube.mac, timeout=0.05)

    assert run(body()) is None


def test_canary_ignores_notifies_from_other_tubes():
    core, tube = _connected_core()
    other = Tube("AA:AA:AA:AA:AA:AA", position=2)
    other.client = FakeClient(FakeHandle(other.mac))
    other.connected = True
    core.tubes[other.mac] = other

    async def body():
        task = asyncio.create_task(core.canary(tube.mac, timeout=0.05))
        await asyncio.sleep(0)
        core._on_notify(other.mac, _battery_reply(other.mac))   # someone else's reply
        return await task

    assert run(body()) is None                      # not fooled -> timeout


def test_canary_disconnected_tube_returns_none():
    core = make_core()
    tube = Tube("AA:BB:CC:DD:EE:0E")                # known but not connected
    core.tubes[tube.mac] = tube

    assert run(core.canary(tube.mac)) is None
    assert run(core.canary("11:22:33:44:55:66")) is None    # unknown MAC


def test_canary_rtt_feeds_the_governor():
    # End-to-end: a canary RTT sample is exactly what WriteGovernor.on_delivery
    # consumes (the pluggable congestion-signal seam, plan §Design.2).
    from neewer.protocol import dmx

    core, tube = _connected_core()
    gov = dmx.WriteGovernor()

    async def body():
        task = asyncio.create_task(core.canary(tube.mac, timeout=1.0))
        await asyncio.sleep(0)
        core._on_notify(tube.mac, _battery_reply(tube.mac))
        rtt = await task
        gov.on_delivery(0.0, rtt)
        return rtt

    rtt = run(body())
    assert gov.min_rtt == rtt


def test_auto_query_fires_on_connect():
    """A freshly-connected tube gets battery/state/version reads automatically, so it
    self-identifies (model) and populates telemetry with no external poll — closing
    the 'generic until something queries it' window."""
    core = make_core()
    tube = Tube("AA:BB:CC:DD:EE:09", name="NW-test")
    core.tubes[tube.mac] = tube

    async def body():
        await core._try_connect(tube, FakeHandle(tube.mac))
        assert tube.connected
        await asyncio.sleep(0.25)                       # let the fire-and-forget query run
        mac6 = frames.mac_bytes(tube.mac)
        writes = tube.client.writes
        assert frames.version_query_mac(mac6) in writes  # -> model inference
        assert frames.battery_query(mac6) in writes
        assert frames.state_query(mac6) in writes

    run(body())


# --- connect timeout (#7) --------------------------------------------------

def test_try_connect_bounds_a_hung_connect(monkeypatch):
    """A transport connect that never returns must not stall the supervisor:
    _try_connect gives up after _CONNECT_TIMEOUT and leaves the tube down for
    the normal backoff-and-retry path."""
    import neewer.fleet as fleet_mod
    monkeypatch.setattr(fleet_mod, "_CONNECT_TIMEOUT", 0.05)
    core = make_core()

    async def hang_forever(handle, on_disconnect):
        await asyncio.sleep(3600)

    core.transport.connect = hang_forever
    tube = Tube("AA:BB:CC:DD:EE:07", name="NW-test")
    core.tubes[tube.mac] = tube

    async def body():
        await asyncio.wait_for(core._try_connect(tube, FakeHandle(tube.mac)), timeout=1.0)
        assert not tube.connected

    run(body())


# --- half-open-link liveness probe (#47) ------------------------------------

def _stale(core, tube, age=1000.0):
    """Backdate the tube's last reply so it looks silent for `age` seconds."""
    import time as _time
    tube.state.last_reply_at = _time.monotonic() - age


def test_liveness_skips_while_replies_are_recent():
    core, tube = _connected_core()
    tube.state.last_reply_at = None                     # then a fresh reply below
    core._on_notify(tube.mac, _battery_reply(tube.mac))
    probed = []

    async def fake_canary(mac, timeout=1.0):
        probed.append(mac)
        return 0.01

    core.canary = fake_canary
    run(core._check_liveness(tube))
    assert probed == []                                 # recent reply: no probe sent


def test_liveness_exempts_a_tube_that_never_replied():
    """Deaf-but-controllable fixtures (no notify support) must not be drop-cycled:
    with no reply ever observed, silence is indistinguishable from deafness."""
    core, tube = _connected_core()
    assert tube.state.last_reply_at is None
    probed = []

    async def fake_canary(mac, timeout=1.0):
        probed.append(mac)
        return None

    core.canary = fake_canary
    run(core._check_liveness(tube))
    assert probed == [] and tube.connected


def test_liveness_answered_probe_resets_misses():
    core, tube = _connected_core()
    _stale(core, tube)
    tube.probe_misses = 2                               # one miss away from a drop

    async def fake_canary(mac, timeout=1.0):
        return 0.02                                     # the tube answers

    core.canary = fake_canary
    run(core._check_liveness(tube))
    assert tube.probe_misses == 0 and tube.connected


def test_liveness_drops_half_open_link_after_misses():
    """The observed failure mode: writes 'succeed' into a dead ACL while the tube
    renders nothing. Only a missing *reply* reveals it — after _PROBE_MISSES
    silent probes the link is dropped so the supervisor reconnects."""
    from neewer.fleet import _PROBE_MISSES
    core, tube = _connected_core()

    async def fake_canary(mac, timeout=1.0):
        return None                                     # silence, every time

    core.canary = fake_canary

    async def body():
        for _ in range(_PROBE_MISSES - 1):
            _stale(core, tube)
            await core._check_liveness(tube)
            assert tube.connected                       # not yet: misses accumulating
        _stale(core, tube)
        await core._check_liveness(tube)
        assert not tube.connected                       # declared half-open and dropped
        assert tube.probe_misses == 0                   # counter reset for the next life

    run(body())


def test_liveness_disabled_by_zero_interval():
    core, tube = _connected_core()
    core.liveness_interval = 0
    _stale(core, tube)
    probed = []

    async def fake_canary(mac, timeout=1.0):
        probed.append(mac)
        return None

    core.canary = fake_canary
    run(core._check_liveness(tube))
    assert probed == [] and tube.connected
