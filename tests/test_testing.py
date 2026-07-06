"""Tests for :mod:`neewer.testing` — the public hardware-free ``MockTransport``.

Covers the reply-frame builders, frame parsing into virtual tube state, the
advertise/connect/drop lifecycle, failure injection, and an end-to-end run of a
**real** :class:`neewer.Fleet` (discovery, auto-query telemetry, dispatch, and
the reconnect supervisor) against the mock. No radio, no ``bleak``.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

import pytest

import neewer.fleet as fleet_mod
from neewer.fleet import Fleet
from neewer.protocol import frames, replies
from neewer.testing import (
    MockTransport,
    MockTube,
    MockWriteError,
    battery_reply,
    state_reply,
    version_reply,
)

MAC1 = "AA:BB:CC:DD:EE:01"
MAC2 = "AA:BB:CC:DD:EE:02"


def run(coro):
    """Run a coroutine to completion on a fresh event loop."""
    return asyncio.run(coro)


def checksum_ok(frame: bytes) -> bool:
    """The protocol's frame checksum: low byte of the sum of all preceding bytes."""
    return frame[-1] == sum(frame[:-1]) & 0xFF


# --- reply builders ---------------------------------------------------------

def test_battery_reply_decodes_with_valid_checksum():
    frame = battery_reply(MAC1, 80)
    assert checksum_ok(frame)
    out = replies.parse(frame)
    assert out["battery"] == 80
    assert out["mac"] == "aa:bb:cc:dd:ee:01"


def test_battery_reply_external_power_sentinel_passes_through():
    # 0xF0 is the mains sentinel some fixtures report instead of a percentage.
    out = replies.parse(battery_reply(MAC1, 0xF0))
    assert "battery" not in out
    assert out["power_source"] == "external"


def test_state_reply_decodes_mode_and_power():
    on = replies.parse(state_reply(MAC1, mode=2, on=True))
    off = replies.parse(state_reply(MAC1, mode=2, on=False))
    assert on["power"] == "on" and off["power"] == "off"
    assert on["mode"] == 2
    assert checksum_ok(state_reply(MAC1))


def test_version_reply_decodes_dotted_triplet():
    frame = version_reply(MAC1, "2.0.5")
    assert checksum_ok(frame)
    assert replies.parse(frame)["version"] == "2.0.5"


def test_version_reply_rejects_malformed_versions():
    with pytest.raises(ValueError):
        version_reply(MAC1, "2.0")             # not a triplet
    with pytest.raises(ValueError):
        version_reply(MAC1, "2.x.5")           # non-integer component


# --- scanning / advertising --------------------------------------------------

def test_scan_advertises_only_unconnected_tubes():
    async def body():
        free = MockTube(mac=MAC1)
        busy = MockTube(mac=MAC2, name="NW-20240012&00000000")     # a TL90C batch name
        transport = MockTransport(tubes=[free, busy])
        await transport.connect(busy, lambda: None)                # busy: link held
        seen = []
        await transport.start_scan(seen.append)
        assert [advert.address for advert in seen] == [MAC1]
        advert = seen[0]
        assert advert.name == "NW-20240047&00000000"
        assert advert.rssi == -50
        assert advert.handle is free                # the handle connect() accepts

    run(body())


def test_add_tube_mid_scan_advertises_immediately():
    async def body():
        transport = MockTransport()
        seen = []
        await transport.start_scan(seen.append)
        transport.add_tube(MockTube(mac=MAC1))
        assert [advert.address for advert in seen] == [MAC1]

    run(body())


def test_stop_scan_silences_adverts():
    async def body():
        tube = MockTube(mac=MAC1)
        transport = MockTransport(tubes=[tube])
        seen = []
        await transport.start_scan(seen.append)
        await transport.stop_scan()
        client = await transport.connect(tube, lambda: None)
        await transport.disconnect(client)          # freed, but no scan running
        assert len(seen) == 1                       # only the initial advert

    run(body())


# --- connect / disconnect lifecycle -------------------------------------------

def test_connect_by_mac_string_and_double_connect_refused():
    async def body():
        tube = MockTube(mac=MAC1)
        transport = MockTransport(tubes=[tube])
        client = await transport.connect(MAC1, lambda: None)   # a MAC works as a handle
        assert client is tube
        assert transport.is_connected(client)
        with pytest.raises(ConnectionError):
            await transport.connect(tube, lambda: None)        # one central at a time
        with pytest.raises(ConnectionError):
            await transport.connect("11:22:33:44:55:66", lambda: None)  # unknown tube

    run(body())


def test_disconnect_fires_callback_and_readvertises():
    async def body():
        tube = MockTube(mac=MAC1)
        transport = MockTransport(tubes=[tube])
        seen = []
        await transport.start_scan(seen.append)
        dropped = []
        client = await transport.connect(tube, lambda: dropped.append(True))
        before = len(seen)
        await transport.disconnect(client)
        assert dropped == [True]                    # deliberate disconnects fire it too
        assert not transport.is_connected(client)
        assert len(seen) == before + 1              # the freed tube advertises again
        await transport.disconnect(client)          # idempotent

    run(body())


# --- writes: virtual state, log, unknown opcodes -------------------------------

def test_writes_update_virtual_state_and_log():
    async def body():
        tube = MockTube(mac=MAC1)
        transport = MockTransport(tubes=[tube])
        client = await transport.connect(tube, lambda: None)
        await transport.write(client, frames.power(True))
        await transport.write(client, frames.hsi(240, 100, 80))
        await transport.write(client, frames.cct(50, 56))
        await transport.write(client, frames.power(False))
        assert tube.power == "off"
        assert tube.hsi == (240, 100, 80)
        assert tube.cct == (50, 56, frames.GM_NEUTRAL)
        assert len(tube.writes) == 4                # the full write log, in order
        assert tube.last_frame == frames.power(False)
        assert tube.unknown == []

    run(body())


def test_unknown_opcode_is_recorded_not_raised():
    async def body():
        tube = MockTube(mac=MAC1)
        transport = MockTransport(tubes=[tube])
        client = await transport.connect(tube, lambda: None)
        identify = frames.identify(frames.mac_bytes(MAC1))      # 0x99: not modelled
        await transport.write(client, identify)
        await transport.write(client, b"\x01\x02\x03")          # not a 0x78 frame at all
        assert tube.unknown == [identify, b"\x01\x02\x03"]
        assert len(tube.writes) == 2                # still logged

    run(body())


# --- query replies on the notify path -------------------------------------------

def test_queries_elicit_wellformed_replies_after_the_write_returns():
    async def body():
        tube = MockTube(mac=MAC1, version="2.0.5", battery=80, mode=2)
        transport = MockTransport(tubes=[tube])
        client = await transport.connect(tube, lambda: None)
        got: list[bytes] = []
        await transport.subscribe(client, got.append)
        mac6 = frames.mac_bytes(MAC1)
        await transport.write(client, frames.battery_query(mac6))
        assert got == []                            # reply lands after the write, not during
        await transport.write(client, frames.state_query(mac6))
        await transport.write(client, frames.version_query_mac(mac6))
        await asyncio.sleep(0)                      # let the scheduled notifies deliver
        assert all(checksum_ok(frame) for frame in got)
        parsed = [replies.parse(frame) for frame in got]
        assert parsed[0]["battery"] == 80
        assert parsed[1]["mode"] == 2 and parsed[1]["power"] == "on"
        assert parsed[2]["version"] == "2.0.5"

    run(body())


def test_query_for_another_mac_stays_silent():
    async def body():
        tube = MockTube(mac=MAC1)
        transport = MockTransport(tubes=[tube])
        client = await transport.connect(tube, lambda: None)
        got: list[bytes] = []
        await transport.subscribe(client, got.append)
        await transport.write(client, frames.battery_query(frames.mac_bytes(MAC2)))
        await asyncio.sleep(0)
        assert got == []                            # a real light ignores someone else's query

    run(body())


def test_state_reply_tracks_the_written_power_state():
    async def body():
        tube = MockTube(mac=MAC1)
        transport = MockTransport(tubes=[tube])
        client = await transport.connect(tube, lambda: None)
        got: list[bytes] = []
        await transport.subscribe(client, got.append)
        await transport.write(client, frames.power(False))      # switch the virtual light off
        await transport.write(client, frames.state_query(frames.mac_bytes(MAC1)))
        await asyncio.sleep(0)
        assert replies.parse(got[-1])["power"] == "off"

    run(body())


# --- failure injection ------------------------------------------------------------

def test_fail_writes_flag_raises_and_can_be_cleared():
    async def body():
        tube = MockTube(mac=MAC1)
        transport = MockTransport(tubes=[tube], fail_writes=True)
        client = await transport.connect(tube, lambda: None)
        with pytest.raises(MockWriteError):
            await transport.write(client, frames.power(True))
        assert tube.writes == []                    # the failed write never landed
        transport.fail_writes = False               # runtime-flippable
        await transport.write(client, frames.power(True))
        assert tube.power == "on"

    run(body())


def test_write_latency_delays_the_write():
    async def body():
        tube = MockTube(mac=MAC1)
        transport = MockTransport(tubes=[tube], write_latency=0.05)
        client = await transport.connect(tube, lambda: None)
        start = time.monotonic()
        await transport.write(client, frames.power(True))
        assert time.monotonic() - start >= 0.04     # latency applied (with timer slack)

    run(body())


def test_drop_fires_disconnect_readvertises_and_kills_writes():
    async def body():
        tube = MockTube(mac=MAC1)
        transport = MockTransport(tubes=[tube])
        seen = []
        await transport.start_scan(seen.append)
        dropped = []
        client = await transport.connect(tube, lambda: dropped.append(True))
        before = len(seen)
        tube.drop()
        assert dropped == [True]                    # the on_disconnect callback fired
        assert not transport.is_connected(client)
        assert len(seen) == before + 1              # resumed advertising
        with pytest.raises(MockWriteError):
            await transport.write(client, frames.power(True))   # dead link refuses writes
        tube.drop()                                 # idempotent
        assert dropped == [True]

    run(body())


# --- end-to-end: a real Fleet on the mock ------------------------------------------


async def _wait_for(predicate, timeout: float = 2.0) -> None:
    """Poll ``predicate`` until true (asyncio.timeout, not wait_for — 3.11-safe)."""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


async def _no_stale_links(prefixes, macs):
    """Stand-in for the BlueZ self-heal: never touch the host's real Bluetooth."""
    return []


def test_fleet_end_to_end_scan_connect_dispatch_drop_reconnect(monkeypatch):
    # Shrink the fleet's timing so discovery/reconnect complete in milliseconds,
    # and stub the BlueZ startup self-heal — on a machine with real lights it
    # would otherwise disconnect them. This suite must stay hardware-free.
    monkeypatch.setattr(fleet_mod.bluez, "clear_stale_connections", _no_stale_links)
    monkeypatch.setattr(fleet_mod, "_INITIAL_SETTLE", 0.0)
    monkeypatch.setattr(fleet_mod, "_SUPERVISE_CONNECTED", 0.02)
    monkeypatch.setattr(fleet_mod, "_SUPERVISE_RETRY_BASE", 0.02)

    tube = MockTube(mac=MAC1, name="NW-20240047&00000000", version="2.0.5", battery=80)
    fleet = Fleet(transport=MockTransport(tubes=[tube]))

    async def body():
        await fleet.start()                         # scan: the advert registers the tube
        assert MAC1 in fleet.tubes
        assert fleet.tubes[MAC1].model == "TL120C-2"    # decoded from the advertised name
        await _wait_for(lambda: fleet.tubes[MAC1].connected)

        # Auto-query on connect populated telemetry through the mock's replies.
        state = fleet.tubes[MAC1].state
        await _wait_for(lambda: state.battery == 80 and state.version == "2.0.5")
        assert state.power == "on"

        # Dispatch drives the virtual state.
        result = await fleet.dispatch("all hsi 240 100 80")
        assert "1 tube(s)" in result
        assert tube.hsi == (240, 100, 80)
        await fleet.power("all", False)             # the typed API works too
        assert tube.power == "off"

        # Drop the link: the supervisor notices and reconnects on its own.
        tube.drop()
        assert fleet.tubes[MAC1].connected is False
        await _wait_for(lambda: fleet.tubes[MAC1].connected)

        await fleet.stop()
        assert tube.link_up is False                # clean shutdown released the link

    run(body())


def test_fleet_write_timeout_treats_slow_mock_as_half_open(monkeypatch):
    # A write slower than the fleet's deadline reads as a half-open link: the
    # write fails fast and the tube is dropped for the supervisor to reclaim.
    tube = MockTube(mac=MAC1)
    transport = MockTransport(tubes=[tube], write_latency=1.0)
    fleet = Fleet(transport=transport, write_timeout=0.05)

    async def body():
        client = await transport.connect(tube, lambda m=MAC1: fleet._on_drop(m))
        fleet_tube = fleet_mod.Tube(MAC1, name="NW-20240047&00000000")
        fleet_tube.client = client
        fleet_tube.connected = True
        fleet.tubes[MAC1] = fleet_tube
        assert await fleet.write(MAC1, frames.power(True)) is False
        assert fleet_tube.connected is False        # declared half-open and dropped

    run(body())


# --- import hygiene ------------------------------------------------------------------

def test_testing_module_imports_without_bleak():
    """`neewer.testing` must import cleanly with no BLE stack installed."""
    repo_root = Path(__file__).resolve().parent.parent
    code = (
        "import sys\n"
        "sys.modules['bleak'] = None\n"     # poison: any 'import bleak' now fails
        "from neewer.testing import MockTransport, MockTube\n"
        "print('ok')\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=repo_root,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"
