"""Tests for the string command grammar in :mod:`neewer.grammar`.

Covers the line parser (three target shapes, targetless verbs, error cases, the
immutable :class:`Command` dataclass), the OSC address-to-command-line mapping,
and the string-argument coercion helpers (``_ints`` / ``_parse_gel_brand``) that
used to live in ``fleet``. End-to-end dispatch (line -> typed method -> frame) is
covered by ``test_fleet.py``, since ``Fleet.dispatch`` defers here.
"""
from __future__ import annotations

import dataclasses

import pytest

from neewer import grammar
from neewer.grammar import Command, osc_to_command, parse
from neewer.protocol import frames

# --- parse ----------------------------------------------------------------

def test_parse_target_action_args():
    assert parse("all hsi 240 100 80") == Command("all", "hsi", ["240", "100", "80"])


def test_parse_target_action_no_args():
    assert parse("t1 power on") == Command("t1", "power", ["on"])


def test_parse_mac_target():
    cmd = parse("AA:BB:CC:DD:EE:FF cct 80 56")
    assert cmd.target == "AA:BB:CC:DD:EE:FF"
    assert cmd.action == "cct"


def test_parse_strips_and_collapses_whitespace():
    assert parse("  all   hsi   10  ") == Command("all", "hsi", ["10"])


@pytest.mark.parametrize("verb", grammar.TARGETLESS_ACTIONS)
def test_parse_targetless_verbs_normalise_target_to_all(verb):
    cmd = parse(f"{verb} palette speed=.1")
    assert cmd.target == "all"
    assert cmd.action == verb


def test_parse_flow_keeps_args():
    cmd = parse("flow palette speed=.1 spread=.08")
    assert cmd.args == ["palette", "speed=.1", "spread=.08"]


def test_parse_stop_has_no_args():
    assert parse("stop").args == []


def test_parse_empty_raises():
    with pytest.raises(ValueError):
        parse("   ")


def test_parse_target_without_action_raises():
    with pytest.raises(ValueError):
        parse("all")


def test_command_is_frozen():
    cmd = parse("all power on")
    with pytest.raises(dataclasses.FrozenInstanceError):
        cmd.action = "hsi"  # type: ignore[misc]


# --- osc_to_command -------------------------------------------------------

def test_osc_strips_neewer_namespace_and_joins_list_args():
    assert osc_to_command("/neewer/all/hsi", [240, 100, 80]) == "all hsi 240 100 80"


def test_osc_without_namespace():
    assert osc_to_command("/t1/bri", [80]) == "t1 bri 80"


def test_osc_string_arg_passed_through():
    assert osc_to_command("/neewer/all/flow", "palette speed=.1") == \
        "all flow palette speed=.1"


def test_osc_handles_float_args():
    assert osc_to_command("/neewer/t2/hsi", [10.0]) == "t2 hsi 10.0"


def test_osc_no_args():
    assert osc_to_command("/neewer/stop", []) == "stop"


# --- _ints ----------------------------------------------------------------

def test_ints_fills_defaults_to_total():
    assert grammar._ints(["240"], ("hue", "sat", "bri"), required=1, total=3,
                         defaults=(0, 100, 100)) == [240, 100, 100]


def test_ints_unlimited_total_returns_all():
    assert grammar._ints(["1", "2", "3", "4"], ("effect", "p"), required=1, total=None,
                         defaults=()) == [1, 2, 3, 4]


def test_ints_truncates_to_total():
    assert grammar._ints(["1", "2", "3", "4"], ("a", "b", "c"), required=1, total=3,
                         defaults=()) == [1, 2, 3]


def test_ints_too_few_args_raises_with_names():
    with pytest.raises(ValueError) as exc:
        grammar._ints([], ("bri", "temp"), required=2, total=3, defaults=())
    assert "<bri>" in str(exc.value) and "<temp>" in str(exc.value)


def test_ints_non_integer_raises():
    with pytest.raises(ValueError) as exc:
        grammar._ints(["x"], ("hue",), required=1, total=1, defaults=())
    assert "integer" in str(exc.value)


# --- _parse_gel_brand -----------------------------------------------------

@pytest.mark.parametrize("token,expected", [
    ("rosco", frames.GEL_BRAND_ROSCO), ("1", frames.GEL_BRAND_ROSCO),
    ("lee", frames.GEL_BRAND_LEE), ("2", frames.GEL_BRAND_LEE),
    ("LEE", frames.GEL_BRAND_LEE),  # case-insensitive
])
def test_parse_gel_brand(token, expected):
    assert grammar._parse_gel_brand(token) == expected


def test_parse_gel_brand_bad_raises():
    with pytest.raises(ValueError):
        grammar._parse_gel_brand("kodak")
