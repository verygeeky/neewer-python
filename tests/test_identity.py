"""Tests for :mod:`neewer.protocol.identity` — advertised name -> light type / model.

The three "live roster" cases are the ground truth: the exact advertised names of the
tubes on the reference rig, each with a known physical model. The decoder must reproduce
the app's integer light type and the model name derived from it.
"""
from __future__ import annotations

from neewer.protocol import identity


# --- the live roster: (advertised name, known model, expected light type) ---
# name -> type validated against the official app's own results.
def test_live_roster_tl90c():
    # NW-20240012 = TL90C (three physical units, MACs rotate; the serial is the key).
    assert identity.get_light_type("NW-20240012&00000000") == 95
    assert identity.model_for_name("NW-20240012&FFFFFFFF") == "TL90C"
    assert identity.model_for_name("NW-20240012&0000000A") == "TL90C"


def test_live_roster_tl120c_2():
    # NW-20240047 = TL120C-2 (type 101).
    assert identity.get_light_type("NW-20240047&FFFFFFFF") == 101
    assert identity.model_for_name("NW-20240047&FFFFFFFF") == "TL120C-2"


def test_live_roster_tl60_rgb_3():
    # NW-20240061 = TL60 RGB-3 (type 115).
    assert identity.get_light_type("NW-20240061&00000000") == 115
    assert identity.model_for_name("NW-20240061&00000000") == "TL60 RGB-3"


def test_mac_does_not_change_the_type():
    # The MAC is accepted but never changes the answer: the type is a pure function of
    # the name. (MACs rotate on power-cycle, so relying on them would be wrong.)
    name = "NW-20240012&00000000"
    assert (identity.get_light_type(name, "CA:B1:6E:F4:7D:D3")
            == identity.get_light_type(name, "AA:BB:CC:DD:EE:03")
            == identity.get_light_type(name, None)
            == 95)


def test_serial_table_covers_other_known_families():
    # A few more distinctive serials from the app's table.
    assert identity.model_for_name("NW-20210036&00000000") == "TL60 RGB"      # type 32
    assert identity.model_for_name("NW-20230064&00000000") == "TL60 RGB-2"    # type 59
    assert identity.model_for_name("NW-20230031&00000000") == "TL120C"        # type 50
    assert identity.model_for_name("NW-20200037&00000000") == "SL90"          # type 14


def test_bare_model_name_substring_branch():
    # A name that already carries the model string (NWR-/NEEWER-/raw) resolves by
    # substring; the longest match wins so a "-2" generation beats the base model.
    assert identity.get_light_type("NWR-TL120C-2") == 101
    assert identity.get_light_type("NEEWER-TL120C") == 50
    assert identity.get_light_type("TL90C") == 95
    assert identity.get_light_type("TL60 RGB-3") == 115


def test_unknown_and_edge_cases():
    assert identity.get_light_type(None) == identity.UNKNOWN
    assert identity.get_light_type("") == identity.UNKNOWN
    # Known NW- shape but an unlisted serial: no fallback substring to match -> UNKNOWN.
    assert identity.get_light_type("NW-99999999&00000000") == identity.UNKNOWN
    # A wholly unrelated name.
    assert identity.get_light_type("SomeOtherBrand") == identity.UNKNOWN
    assert identity.model_for_name("nope") is None


def test_type_to_model_roundtrips_for_our_fixtures():
    for light_type, model in ((95, "TL90C"), (101, "TL120C-2"), (115, "TL60 RGB-3"),
                              (50, "TL120C"), (32, "TL60 RGB"), (59, "TL60 RGB-2")):
        assert identity.model_for_type(light_type) == model
    assert identity.model_for_type(0) is None
