"""Tests for the typed command model in :mod:`neewer.protocol.commands`.

Each action is a frozen dataclass that is the single source of argument-order
truth and knows how to build its own wire frame(s). These tests pin the frame
bytes to the :mod:`neewer.protocol.frames` builders and cover the validation each
dataclass performs at construction.
"""
from __future__ import annotations

import dataclasses

import pytest

from neewer.protocol import commands, frames

MAC = "AA:BB:CC:DD:EE:01"
MAC6 = frames.mac_bytes(MAC)


# --- direct-frame actions -------------------------------------------------

@pytest.mark.parametrize("cmd,expected", [
    (commands.Power(True), frames.power(True)),
    (commands.Power(False), frames.power(False)),
    (commands.HSI(240, 100, 80), frames.hsi(240, 100, 80)),
    (commands.HSI(240), frames.hsi(240, 100, 100)),          # sat/bri default to full
    (commands.CCT(80, 56), frames.cct(80, 56)),
    (commands.CCT(80, 56, 40), frames.cct(80, 56, 40)),
    (commands.Brightness(55), frames.hsi(0, 0, 55)),         # white-ish, brightness only
    (commands.Raw("78 81 01 01 fb"), frames.raw("78 81 01 01 fb")),
])
def test_direct_frames(cmd, expected):
    assert cmd.frame() == expected


def test_command_is_frozen():
    cmd = commands.HSI(240)
    with pytest.raises(dataclasses.FrozenInstanceError):
        cmd.hue = 120  # type: ignore[misc]


# --- by-MAC actions -------------------------------------------------------

def test_rgbcw_frame_and_defaults():
    assert commands.RGBCW(50, 0, 127, 250, 0, 0).frame(MAC6) == \
        frames.rgbcw_by_mac(MAC6, 50, 0, 127, 250, 0, 0)
    # the five channels default to 0
    assert commands.RGBCW(50).frame(MAC6) == frames.rgbcw_by_mac(MAC6, 50, 0, 0, 0, 0, 0)
    assert commands.RGBCW.CAPABILITY == "rgbcw"


def test_xy_frame_and_range_validation():
    assert commands.XY(50, 0.3127, 0.3290).frame(MAC6) == \
        frames.xy_by_mac(MAC6, 50, 0.3127, 0.3290)
    assert commands.XY.CAPABILITY == "xy"
    with pytest.raises(ValueError):
        commands.XY(50, 1.5, 0.3)            # x out of 0..1
    with pytest.raises(ValueError):
        commands.XY(50, 0.3, -0.1)           # y out of 0..1


def test_gel_frame_defaults_and_brand_validation():
    assert commands.Gel(45, 100, 50, frames.GEL_BRAND_ROSCO, 1).frame(MAC6) == \
        frames.gel_by_mac(MAC6, 45, 100, 50, frames.GEL_BRAND_ROSCO, 1)
    # brand/gel_no default to ROSCO/0
    assert commands.Gel(45, 100, 50).frame(MAC6) == \
        frames.gel_by_mac(MAC6, 45, 100, 50, frames.GEL_BRAND_ROSCO, 0)
    assert commands.Gel.CAPABILITY == "gel"
    with pytest.raises(ValueError):
        commands.Gel(45, 100, 50, brand=9)   # unknown brand byte


def test_identify_frame():
    assert commands.Identify().frame(MAC6) == frames.identify(MAC6)


def test_scene_has_both_transports():
    cmd = commands.Scene(3, (9,))
    assert cmd.legacy_frame() == frames.scene(3, 9)
    assert cmd.mac_frame(MAC6) == frames.scene_by_mac(MAC6, 3, 9)


def test_pixel_frames_and_empty_validation():
    cmd = commands.Pixel(("0", "240"))
    assert cmd.params_frame(MAC6) == frames.pixel_params(MAC6)
    assert cmd.palette_frame(MAC6) == frames.pixel_palette(MAC6, ["0", "240"])
    assert commands.Pixel.CAPABILITY == "pixel"
    with pytest.raises(ValueError):
        commands.Pixel(())                   # empty palette
    with pytest.raises(ValueError):
        commands.Pixel(("nope",)).palette_frame(MAC6)   # bad colour token


# --- action registry (the single source of argument-order truth) ----------

def test_dataclass_field_order_is_stable():
    """Pin every command dataclass' field ORDER.

    The wire surfaces (the string grammar, the HTTP JSON field-map, the MCP tool
    signatures) map their arguments *positionally* onto these dataclasses, so a
    silent field reorder here would desync all of them. If this test fails, a field
    order changed on purpose — update the wire surfaces (and ``commands.ACTIONS``)
    to match, then update this pin.
    """
    expected = {
        commands.Power: ["on"],
        commands.HSI: ["hue", "sat", "bri"],
        commands.CCT: ["bri", "temp", "gm"],
        commands.Brightness: ["bri"],
        commands.RGBCW: ["bri", "r", "g", "b", "c", "w"],
        commands.XY: ["bri", "x", "y"],
        commands.Gel: ["hue", "sat", "bri", "brand", "gel_no"],
        commands.Scene: ["effect", "params"],
        commands.Pixel: ["colors", "effect"],
        commands.Identify: [],
        commands.Raw: ["hexstr"],
    }
    for cls, names in expected.items():
        assert [f.name for f in dataclasses.fields(cls)] == names, cls.__name__


def test_actions_registry_is_consistent():
    """``commands.ACTIONS`` must stay coherent with the dataclasses it maps.

    A variadic name must be a real field on the command, and the count of scalar
    wire-fields plus the variadic must never exceed the command's field count.
    """
    for action, spec in commands.ACTIONS.items():
        cls_fields = [f.name for f in dataclasses.fields(spec.command)]
        if spec.variadic is not None:
            assert spec.variadic in cls_fields, action
        used = len(spec.fields) + (1 if spec.variadic else 0)
        assert used <= len(cls_fields), action


def test_actions_registry_covers_targeted_grammar_actions():
    """Every targeted grammar action has a registry entry (and vice-versa)."""
    from neewer import grammar
    # Actions the grammar dispatches that carry a leading target + typed args.
    targeted = {"power", "hsi", "cct", "bri", "scene", "pixel",
                "rgbcw", "xy", "gel", "identify", "raw"}
    assert set(commands.ACTIONS) == targeted
    # None of the targetless whole-daemon verbs leak into the registry.
    assert not (set(grammar.TARGETLESS_ACTIONS) & set(commands.ACTIONS))
