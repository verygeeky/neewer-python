"""Tests for :mod:`neewer.fleet` — addressing, frame routing and end-to-end
dispatch against a fake BLE layer.

No real radio is touched: we inject :class:`~neewer.fleet.Tube` objects whose
client is a recorder that captures every ``write_gatt_char`` call. ``bleak`` is
the conftest stub, so importing the module is safe.

The string-grammar coercion (``_ints`` / ``_parse_gel_brand`` / ``parse``) lives
in :mod:`neewer.grammar` now and is covered by ``test_grammar.py``; the typed
frame-building lives in :mod:`neewer.protocol.commands` (``test_commands.py``).
"""
from __future__ import annotations

import asyncio
import dataclasses
import json

import pytest

from neewer.devices import DeviceBook
from neewer.errors import UnknownEffect, UnknownTarget, Unsupported
from neewer.fleet import NeewerCore, Tube, TubeState
from neewer.protocol import frames, models
from neewer.transport import WRITE_UUID


def run(coro):
    """Run a coroutine to completion on a fresh event loop."""
    return asyncio.run(coro)


class FakeClient:
    """Records write_gatt_char calls; stands in for a connected BleakClient."""

    def __init__(self):
        self.writes: list[tuple[str, bytes, bool]] = []
        self.disconnected = False

    async def write_gatt_char(self, uuid, data, response=True):
        self.writes.append((uuid, bytes(data), response))

    async def disconnect(self):
        self.disconnected = True


def make_core(*specs) -> NeewerCore:
    """Build a core pre-populated with connected fake tubes.

    Each spec is ``(mac, position)``; the tube gets a FakeClient and connected.
    """
    core = NeewerCore()
    for mac, position in specs:
        tube = Tube(mac, name=f"NW-{mac}", position=position)
        tube.client = FakeClient()
        tube.connected = True
        core.tubes[mac] = tube
    return core


# --- resolve / ordered ----------------------------------------------------

def test_resolve_all_returns_position_order():
    core = make_core(("AA", 2), ("BB", 1))
    assert core.resolve("all") == ["BB", "AA"]


def test_ordered_unpositioned_fall_back_to_mac_order():
    # No positions: deterministic MAC sort.
    core = make_core(("CC:02", None), ("CC:01", None))
    assert core.ordered() == ["CC:01", "CC:02"]


def test_ordered_positioned_sort_before_unpositioned():
    core = make_core(("ZZ", 1), ("AA", None))
    # Positioned tube sorts first even though its MAC is "larger".
    assert core.ordered() == ["ZZ", "AA"]


def test_ordered_skips_disconnected():
    core = make_core(("AA", 1), ("BB", 2))
    core.tubes["BB"].connected = False
    assert core.ordered() == ["AA"]


def test_resolve_t_position():
    core = make_core(("AA", 1), ("BB", 2))
    assert core.resolve("t2") == ["BB"]


def test_resolve_t_position_unknown_is_empty():
    core = make_core(("AA", 1))
    assert core.resolve("t9") == []


def test_resolve_mac_case_insensitive():
    core = make_core(("AA:BB:CC:DD:EE:FF", 1))
    assert core.resolve("aa:bb:cc:dd:ee:ff") == ["AA:BB:CC:DD:EE:FF"]


def test_resolve_unknown_target_is_empty():
    core = make_core(("AA", 1))
    assert core.resolve("nope") == []


def test_resolve_mac_not_connected_is_empty():
    core = make_core(("AA", 1))
    core.tubes["AA"].connected = False
    assert core.resolve("AA") == []


# --- resolve with a device book (aliases / groups) ------------------------

def test_resolve_alias_to_mac():
    core = make_core(("AA", 1))
    core.book = DeviceBook(aliases={"key": "AA"})
    assert core.resolve("key") == ["AA"]
    assert core.resolve("KEY") == ["AA"]  # case-insensitive


def test_resolve_group_in_declared_order_filtering_disconnected():
    core = make_core(("AA", 2), ("BB", 1))
    core.book = DeviceBook(
        aliases={"key": "AA", "fill": "BB"},
        groups={"pair": ["fill", "key"]},  # declared fill-then-key, not position
    )
    # group order is honoured (fill=BB before key=AA), NOT position order
    assert core.resolve("pair") == ["BB", "AA"]
    core.tubes["BB"].connected = False
    assert core.resolve("pair") == ["AA"]  # disconnected member dropped


def test_resolve_builtins_win_over_group_of_same_name():
    core = make_core(("AA", 1), ("BB", 2))
    # a group perversely named "all" must not shadow the built-in
    core.book = DeviceBook(aliases={"key": "AA"}, groups={"all": ["key"]})
    assert core.resolve("all") == ["AA", "BB"]  # built-in all, both tubes


def test_resolve_unknown_alias_still_empty():
    core = make_core(("AA", 1))
    core.book = DeviceBook(aliases={"key": "AA"})
    assert core.resolve("ghost") == []


def test_book_positions_feed_ordering():
    # positions from the shared book should drive t<N> / ordered() just like
    # the daemon's [core.positions] does.
    core = NeewerCore(book=DeviceBook(aliases={"key": "AA"}, positions={"key": 3}))
    tube = Tube("AA", name="NW-AA", position=core.positions.get("AA"))
    tube.client = FakeClient()
    tube.connected = True
    core.tubes["AA"] = tube
    assert core.resolve("t3") == ["AA"]


# --- typed state ----------------------------------------------------------

def test_tube_state_types_known_fields_and_keeps_unknowns():
    st = TubeState()
    st.update_from_reply({"battery": 80, "version": "2.0.5", "wibble": 7})
    assert st.battery == 80
    assert st.version == "2.0.5"
    assert st.extra == {"wibble": 7}            # unrecognised key preserved
    st.last = "all power on"
    # as_dict renders exactly the wire snapshot shape the status modules consume.
    assert st.as_dict() == {"last": "all power on", "battery": 80,
                            "version": "2.0.5", "wibble": 7}


def test_tube_state_empty_as_dict_is_empty():
    assert TubeState().as_dict() == {}


# --- dispatch end-to-end --------------------------------------------------

def test_dispatch_writes_correct_bytes_to_all_tubes():
    core = make_core(("AA", 1), ("BB", 2))
    result = run(core.dispatch("all hsi 240 100 80"))
    assert "2 tube(s)" in result
    expected = frames.hsi(240, 100, 80)
    for mac in ("AA", "BB"):
        writes = core.tubes[mac].client.writes
        assert writes == [(WRITE_UUID, expected, False)]
    # last-command state is recorded per tube.
    assert core.tubes["AA"].state.last == "all hsi 240 100 80"


def test_dispatch_targets_single_tube_by_position():
    core = make_core(("AA", 1), ("BB", 2))
    run(core.dispatch("t1 power on"))
    assert core.tubes["AA"].client.writes == [
        (WRITE_UUID, frames.power(True), False)]
    assert core.tubes["BB"].client.writes == []     # untouched


def test_dispatch_no_matching_tubes_raises_and_writes_nothing():
    core = make_core(("AA", 1))
    with pytest.raises(UnknownTarget):
        run(core.dispatch("t5 power on"))
    assert core.tubes["AA"].client.writes == []


def test_dispatch_raw_passthrough():
    core = make_core(("AA", 1))
    run(core.dispatch("AA raw 78 81 01 01 fb"))
    assert core.tubes["AA"].client.writes == [
        (WRITE_UUID, b"\x78\x81\x01\x01\xfb", False)]


def test_dispatch_flow_starts_effect():
    core = make_core(("AA", 1), ("BB", 2))

    async def body():
        result = await core.dispatch("flow comet")
        assert "effect comet" in result
        assert core._effect_task is not None
        await asyncio.sleep(0)  # let the engine run at least one tick
        await core.cancel_effect()

    run(body())
    # The comet engine wrote HSI frames to the tubes.
    assert core.tubes["AA"].client.writes, "effect should have written frames"
    op = core.tubes["AA"].client.writes[0][1][1]
    assert op == frames.OP_HSI


def test_dispatch_stop_cancels_effect():
    core = make_core(("AA", 1))

    async def body():
        await core.dispatch("flow comet")
        assert core._effect_task is not None
        result = await core.dispatch("stop")
        assert "stopped" in result
        assert core._effect_task is None

    run(body())


# --- consumer-registered verbs -------------------------------------------
# Presets used to live on the Fleet; they now belong to the daemon, which layers
# them on via this generic hook. The library only owns the mechanism.

def test_register_verb_intercepts_before_parse():
    core = make_core(("AA", 1))
    seen = {}

    async def macro(fleet, args):
        seen["fleet"] = fleet
        seen["args"] = args
        # a registered verb can drive the fleet like any consumer would
        await fleet.dispatch("AA power on")
        return f"ok macro {args}"

    core.register_verb("macro", macro)
    result = run(core.dispatch("macro warm bright"))
    assert result == "ok macro ['warm', 'bright']"
    assert seen["fleet"] is core                       # handler gets the fleet
    assert seen["args"] == ["warm", "bright"]          # and the raw trailing words
    assert core.tubes["AA"].client.writes == [
        (WRITE_UUID, frames.power(True), False)]        # it could dispatch normally


def test_unregistered_verb_still_parses_as_target_action():
    # Without a matching registered verb, the first word is a target as before.
    core = make_core(("AA", 1))
    run(core.dispatch("t1 power on"))
    assert core.tubes["AA"].client.writes == [
        (WRITE_UUID, frames.power(True), False)]


def test_direct_command_cancels_running_effect():
    core = make_core(("AA", 1))

    async def body():
        await core.dispatch("flow hue")
        assert core._effect_task is not None
        await asyncio.sleep(0)
        # A manual command must stop the animation (manual control wins).
        await core.dispatch("AA power off")
        assert core._effect_task is None

    run(body())
    # The final recorded write is the manual power-off frame.
    assert core.tubes["AA"].client.writes[-1][1] == frames.power(False)


def test_start_effect_unknown_mode():
    core = make_core(("AA", 1))
    with pytest.raises(UnknownEffect):
        run(core.start_effect("nope", {}))


def test_start_effect_no_tubes():
    core = NeewerCore()  # nothing connected
    with pytest.raises(UnknownTarget):
        run(core.start_effect("comet", {}))


# --- snapshot -------------------------------------------------------------

def test_snapshot_shape():
    core = make_core(("AA", 1))
    core.tubes["AA"].state.last = "all power on"
    snap = core.snapshot()
    # Unknown model -> the permissive GENERIC capability set rides along.
    generic_caps = dataclasses.asdict(models.GENERIC)
    assert snap == {
        "AA": {"name": "NW-AA", "pos": 1, "connected": True, "model": None,
               "rssi": None, "caps": generic_caps, "last": "all power on"}
    }


def test_snapshot_caps_follow_the_model():
    """A tube with a known model exposes that model's capabilities in ``caps``."""
    core = make_core(("AA", 1))
    core.tubes["AA"].model = "TL120C"
    caps = core.snapshot()["AA"]["caps"]
    assert caps == dataclasses.asdict(models.capabilities("TL120C"))
    assert caps["pixel"] is True                  # TL120C: pixel-capable
    assert caps["scene_legacy"] is False          # ... but drops the 0x88 scene
    assert json.dumps(caps)                       # JSON-serialisable for SSE/MQTT


def test_write_returns_false_when_not_connected():
    core = make_core(("AA", 1))
    core.tubes["AA"].connected = False
    assert run(core.write("AA", b"\x00")) is False


# --- pixel palette (#33) ---------------------------------------------------

MAC1 = "AA:BB:CC:DD:EE:01"


def test_dispatch_pixel_writes_params_then_palette():
    core = make_core((MAC1, 1))
    core.tubes[MAC1].model = "TL120C"
    result = run(core.dispatch("t1 pixel 0 240"))
    assert "ok pixel -> 1 tube(s)" in result
    mac6 = frames.mac_bytes(MAC1)
    sent = [w[1] for w in core.tubes[MAC1].client.writes]
    assert frames.pixel_params(mac6) in sent
    assert frames.pixel_palette(mac6, ["0", "240"]) in sent
    assert core.tubes[MAC1].state.last == "t1 pixel 0 240"


def test_dispatch_pixel_chunks_a_long_palette_to_the_mtu():
    core = make_core((MAC1, 1))
    core.tubes[MAC1].model = "TL120C"
    tokens = [str(h) for h in range(0, 300, 30)]          # 10 segments -> >20-byte frame
    run(core.dispatch("t1 pixel " + " ".join(tokens)))
    sent = [w[1] for w in core.tubes[MAC1].client.writes]
    assert all(len(w) <= 20 for w in sent)                # every ATT write within the cap
    assert len(sent) >= 3                                  # params + a chunked palette


def test_dispatch_pixel_bad_colour_raises_valueerror():
    core = make_core((MAC1, 1))
    core.tubes[MAC1].model = "TL120C"
    with pytest.raises(ValueError):
        run(core.dispatch("t1 pixel nope"))


def test_dispatch_pixel_no_tubes():
    core = make_core((MAC1, 1))
    with pytest.raises(UnknownTarget):
        run(core.dispatch("t9 pixel 0"))


# --- fixture-model awareness (#18) ----------------------------------------

def test_model_from_book_on_discovery_and_snapshot():
    core = NeewerCore(book=DeviceBook(models={"AA": "TL120C"}))
    tube = Tube("AA", model=core.book.model_for("AA"))
    tube.client = FakeClient()
    tube.connected = True
    core.tubes["AA"] = tube
    assert core.snapshot()["AA"]["model"] == "TL120C"


def test_model_inferred_from_version_notification():
    core = make_core((MAC1, 1))
    assert core.tubes[MAC1].model is None
    # a direct version reply (0x00): bytes[5:8] = 2.0.5  -> infer TL120C-2
    core._on_notify(MAC1, bytearray([0x78, 0x00, 0, 0, 0, 2, 0, 5, 0]))
    assert core.tubes[MAC1].state.version == "2.0.5"
    assert core.tubes[MAC1].model == "TL120C-2"


def test_pixel_skipped_on_non_pixel_model():
    core = make_core((MAC1, 1))
    core.tubes[MAC1].model = "TL90C"              # no pixel capability
    with pytest.raises(Unsupported):
        run(core.dispatch("t1 pixel 0 240"))
    assert core.tubes[MAC1].client.writes == []   # nothing sent


def test_scene_routed_via_0x91_on_tl120c():
    core = make_core((MAC1, 1))
    core.tubes[MAC1].model = "TL120C"             # no 0x88 handler, but has 0x91
    result = run(core.dispatch("t1 scene 3"))
    assert "ok scene" in result and "0x91" in result
    expected = frames.scene_by_mac(frames.mac_bytes(MAC1), 3)
    assert core.tubes[MAC1].client.writes == [(WRITE_UUID, expected, False)]


def test_scene_sent_on_permissive_unknown_model():
    core = make_core((MAC1, 1))                    # model None -> GENERIC (permissive)
    run(core.dispatch("t1 scene 3"))
    assert core.tubes[MAC1].client.writes == [(WRITE_UUID, frames.scene(3), False)]


def test_identify_writes_per_mac_frame():
    core = make_core((MAC1, 1))
    result = run(core.dispatch("t1 identify"))
    assert result == "ok identify -> 1 tube(s)"
    expected = frames.identify(frames.mac_bytes(MAC1))
    assert core.tubes[MAC1].client.writes == [(WRITE_UUID, expected, False)]


# --- by-MAC colour modes: rgbcw / xy / gel (TL120C) -----------------------

def test_dispatch_rgbcw_writes_by_mac_frame():
    core = make_core((MAC1, 1))
    core.tubes[MAC1].model = "TL120C"
    result = run(core.dispatch("t1 rgbcw 50 0 127 250 0 0"))
    assert result == "ok rgbcw -> 1 tube(s)"
    expected = frames.rgbcw_by_mac(frames.mac_bytes(MAC1), 50, 0, 127, 250, 0, 0)
    assert core.tubes[MAC1].client.writes == [(WRITE_UUID, expected, False)]
    assert core.tubes[MAC1].state.last == "t1 rgbcw 50 0 127 250 0 0"


def test_dispatch_rgbcw_channels_default_to_zero():
    core = make_core((MAC1, 1))
    core.tubes[MAC1].model = "TL120C"
    run(core.dispatch("t1 rgbcw 50"))
    expected = frames.rgbcw_by_mac(frames.mac_bytes(MAC1), 50, 0, 0, 0, 0, 0)
    assert core.tubes[MAC1].client.writes == [(WRITE_UUID, expected, False)]


def test_dispatch_rgbcw_bad_arg_raises_valueerror():
    core = make_core((MAC1, 1))
    core.tubes[MAC1].model = "TL120C"
    with pytest.raises(ValueError):
        run(core.dispatch("t1 rgbcw notanint"))


def test_dispatch_rgbcw_skipped_on_incapable_model(monkeypatch):
    monkeypatch.setitem(models.MODELS, "NoColorFixture",
                        models.Capabilities(rgbcw=False, xy=False, gel=False))
    core = make_core((MAC1, 1))
    core.tubes[MAC1].model = "NoColorFixture"      # a fixture known to lack by-MAC colour
    with pytest.raises(Unsupported):
        run(core.dispatch("t1 rgbcw 50 10 20 30 0 0"))
    assert core.tubes[MAC1].client.writes == []


def test_dispatch_rgbcw_no_tubes():
    core = make_core((MAC1, 1))
    with pytest.raises(UnknownTarget):
        run(core.dispatch("t9 rgbcw 50"))


def test_dispatch_xy_writes_by_mac_frame():
    core = make_core((MAC1, 1))
    core.tubes[MAC1].model = "TL120C"
    result = run(core.dispatch("t1 xy 50 0.3127 0.3290"))
    assert result == "ok xy -> 1 tube(s)"
    expected = frames.xy_by_mac(frames.mac_bytes(MAC1), 50, 0.3127, 0.3290)
    assert core.tubes[MAC1].client.writes == [(WRITE_UUID, expected, False)]


def test_dispatch_xy_out_of_range_raises_valueerror():
    core = make_core((MAC1, 1))
    core.tubes[MAC1].model = "TL120C"
    with pytest.raises(ValueError):
        run(core.dispatch("t1 xy 50 1.5 0.3"))


def test_dispatch_xy_bad_float_raises_valueerror():
    core = make_core((MAC1, 1))
    core.tubes[MAC1].model = "TL120C"
    with pytest.raises(ValueError):
        run(core.dispatch("t1 xy 50 nope 0.3"))


def test_dispatch_xy_skipped_on_incapable_model(monkeypatch):
    monkeypatch.setitem(models.MODELS, "NoColorFixture",
                        models.Capabilities(rgbcw=False, xy=False, gel=False))
    core = make_core((MAC1, 1))
    core.tubes[MAC1].model = "NoColorFixture"      # a fixture known to lack by-MAC colour
    with pytest.raises(Unsupported):
        run(core.dispatch("t1 xy 50 0.3 0.3"))
    assert core.tubes[MAC1].client.writes == []


def test_dispatch_gel_writes_by_mac_frame():
    core = make_core((MAC1, 1))
    core.tubes[MAC1].model = "TL120C"
    result = run(core.dispatch("t1 gel 45 100 50 rosco 1"))
    assert result == "ok gel -> 1 tube(s)"
    expected = frames.gel_by_mac(frames.mac_bytes(MAC1), 45, 100, 50, 1, 1)
    assert core.tubes[MAC1].client.writes == [(WRITE_UUID, expected, False)]


def test_dispatch_gel_brand_name_and_number_equivalent():
    # "lee" and "2" resolve to the same brand byte.
    core = make_core((MAC1, 1))
    core.tubes[MAC1].model = "TL120C"
    run(core.dispatch("t1 gel 45 100 50 lee 7"))
    core2 = make_core((MAC1, 1))
    core2.tubes[MAC1].model = "TL120C"
    run(core2.dispatch("t1 gel 45 100 50 2 7"))
    assert core.tubes[MAC1].client.writes == core2.tubes[MAC1].client.writes


def test_dispatch_gel_defaults_brand_rosco_and_gelno_zero():
    core = make_core((MAC1, 1))
    core.tubes[MAC1].model = "TL120C"
    run(core.dispatch("t1 gel 45 100 50"))
    expected = frames.gel_by_mac(frames.mac_bytes(MAC1), 45, 100, 50,
                                 frames.GEL_BRAND_ROSCO, 0)
    assert core.tubes[MAC1].client.writes == [(WRITE_UUID, expected, False)]


def test_dispatch_gel_bad_brand_raises_valueerror():
    core = make_core((MAC1, 1))
    core.tubes[MAC1].model = "TL120C"
    with pytest.raises(ValueError):
        run(core.dispatch("t1 gel 45 100 50 kodak 1"))


def test_dispatch_gel_too_few_args_raises_valueerror():
    core = make_core((MAC1, 1))
    core.tubes[MAC1].model = "TL120C"
    with pytest.raises(ValueError):
        run(core.dispatch("t1 gel 45 100"))


def test_dispatch_gel_skipped_on_incapable_model(monkeypatch):
    monkeypatch.setitem(models.MODELS, "NoColorFixture",
                        models.Capabilities(rgbcw=False, xy=False, gel=False))
    core = make_core((MAC1, 1))
    core.tubes[MAC1].model = "NoColorFixture"      # a fixture known to lack by-MAC colour
    with pytest.raises(Unsupported):
        run(core.dispatch("t1 gel 45 100 50"))
    assert core.tubes[MAC1].client.writes == []


def test_by_mac_colour_partial_skip_reports_lack(monkeypatch):
    # one capable tube + one incapable: capable one sent, incapable one reported.
    monkeypatch.setitem(models.MODELS, "NoColorFixture",
                        models.Capabilities(rgbcw=False, xy=False, gel=False))
    core = make_core((MAC1, 1), ("AA:BB:CC:DD:EE:F2", 2))
    core.tubes[MAC1].model = "TL120C"
    core.tubes["AA:BB:CC:DD:EE:F2"].model = "NoColorFixture"  # known to lack by-MAC colour
    result = run(core.dispatch("all rgbcw 50 10 20 30 0 0"))
    assert result == "ok rgbcw -> 1 tube(s) (1 lack rgbcw support)"
    assert core.tubes[MAC1].client.writes         # capable tube written
    assert core.tubes["AA:BB:CC:DD:EE:F2"].client.writes == []


# --- bounded writes: one dead tube must not stall the fleet -----------------

class _HangingClient:
    """A client whose write_gatt_char never returns — models a half-open BLE link
    (BlueZ still reports connected, but the ACL is dead and the write hangs)."""

    def __init__(self):
        self.disconnected = False

    async def write_gatt_char(self, uuid, data, response=True):
        await asyncio.sleep(3600)          # "forever" — the symptom we must bound

    async def disconnect(self):
        self.disconnected = True


def test_write_times_out_and_drops_a_half_open_link():
    core = make_core(("AA", 1))
    core._write_timeout = 0.05                        # short deadline for the test
    core.tubes["AA"].client = _HangingClient()        # its writes hang
    ok = run(core.write("AA", frames.power(True)))
    assert ok is False                                # failed FAST, didn't hang the caller
    assert core.tubes["AA"].connected is False        # dropped -> supervisor reconnects
    assert core.tubes["AA"].client is None


def test_healthy_write_is_unaffected_by_the_timeout():
    core = make_core(("AA", 1))
    assert run(core.write("AA", frames.power(True))) is True
    assert core.tubes["AA"].connected is True
    assert core.tubes["AA"].client.writes == [(WRITE_UUID, frames.power(True), False)]
