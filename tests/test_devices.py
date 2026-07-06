"""Tests for the shared device-identity config (aliases / positions / groups).

These exercise :mod:`neewer.devices` directly with hand-built dicts and a couple
of tmp-file loads; no BLE, no daemon. The resolution/expansion rules here are the
contract that both ``core.resolve()`` and the root scripts depend on.
"""
from __future__ import annotations

import textwrap

import pytest

from neewer import devices
from neewer.devices import DeviceBook, is_mac, normalize_netid

KEY = "AA:BB:CC:DD:EE:01"
FILL = "AA:BB:CC:DD:EE:02"
BACK = "AA:BB:CC:DD:EE:03"


def test_is_mac_accepts_colon_and_dash_forms():
    assert is_mac(KEY)
    assert is_mac(KEY.lower())
    assert is_mac(KEY.replace(":", "-"))
    assert not is_mac("key")
    assert not is_mac("all")
    assert not is_mac("")


def test_alias_resolves_case_insensitively_and_uppercases_mac():
    book = DeviceBook(aliases={"Key": KEY.lower()})
    assert book.resolve_one("key") == KEY
    assert book.resolve_one("KEY") == KEY
    # a bare MAC passes through, upper-cased
    assert book.resolve_one(KEY.lower()) == KEY
    # unknown name is not a MAC -> None
    assert book.resolve_one("nope") is None


def test_expand_group_of_aliases_preserves_order_and_dedupes():
    book = DeviceBook(
        aliases={"key": KEY, "fill": FILL},
        groups={"keys": ["key", "fill", "key"]},  # duplicate on purpose
    )
    assert book.expand("keys") == [KEY, FILL]


def test_expand_nested_group_flattens():
    book = DeviceBook(
        aliases={"key": KEY, "fill": FILL},
        groups={"keys": ["key", "fill"], "all_rgb": ["keys", BACK]},
    )
    assert book.expand("all_rgb") == [KEY, FILL, BACK]


def test_expand_cycle_is_broken():
    book = DeviceBook(groups={"a": ["b"], "b": ["a", KEY]})
    # a -> b -> (a already seen, skip) -> KEY
    assert book.expand("a") == [KEY]


def test_expand_bare_mac_and_unknown():
    book = DeviceBook()
    assert book.expand(KEY.lower()) == [KEY]
    assert book.expand("ghost") == []


def test_positions_key_may_be_alias_or_mac():
    book = DeviceBook(
        aliases={"key": KEY},
        positions={"key": 4, FILL: 1},
    )
    assert book.positions == {KEY: 4, FILL: 1}


def test_empty_book_is_falsy_populated_is_truthy():
    assert not DeviceBook()
    assert DeviceBook(aliases={"key": KEY})


def test_config_path_precedence(monkeypatch, tmp_path):
    monkeypatch.delenv("NEEWER_DEVICES", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    # explicit wins
    assert devices.config_path("/x/y.toml").as_posix() == "/x/y.toml"
    # env next
    monkeypatch.setenv("NEEWER_DEVICES", "/env/d.toml")
    assert devices.config_path().as_posix() == "/env/d.toml"
    monkeypatch.delenv("NEEWER_DEVICES")
    # XDG next
    monkeypatch.setenv("XDG_CONFIG_HOME", tmp_path.as_posix())
    assert devices.config_path() == tmp_path / "neewer" / "devices.toml"


def test_load_missing_file_is_empty(tmp_path):
    book = devices.load((tmp_path / "absent.toml").as_posix())
    assert not book


def test_load_parses_full_file(tmp_path):
    p = tmp_path / "devices.toml"
    p.write_text(
        textwrap.dedent(
            f"""
            [aliases]
            key = "{KEY}"
            fill = "{FILL}"

            [positions]
            key = 4
            fill = 1

            [groups]
            keys = ["key", "fill"]
            """
        )
    )
    book = devices.load(p.as_posix())
    assert book.resolve_one("key") == KEY
    assert book.expand("keys") == [KEY, FILL]
    assert book.positions == {KEY: 4, FILL: 1}


def test_load_malformed_raises(tmp_path):
    p = tmp_path / "bad.toml"
    p.write_text("this is = = not toml")
    with pytest.raises(Exception):
        devices.load(p.as_posix())


def test_models_declared_by_alias_or_mac():
    book = DeviceBook(aliases={"key": KEY}, models={"key": "TL120C", FILL: "TL90C"})
    assert book.model_for(KEY) == "TL120C"        # alias key resolved to its MAC
    assert book.model_for(FILL.lower()) == "TL90C"
    assert book.model_for("00:00:00:00:00:00") is None


# ---- networkId unit-id -------------------------------------------------------

def test_normalize_netid_accepts_int_and_string_forms():
    # int input, zero-padded to 8 lowercase hex digits
    assert normalize_netid(0x00900002) == "00900002"
    assert normalize_netid(0x01200001) == "01200001"
    # bare hex, '&'-prefixed advert suffix, and 0x-prefixed literal all agree
    assert normalize_netid("00900002") == "00900002"
    assert normalize_netid("&00900002") == "00900002"
    assert normalize_netid("0x00900002") == "00900002"
    # case-insensitive + short forms get zero-padded
    assert normalize_netid("&ABCD") == "0000abcd"
    assert normalize_netid("0X0000ABCD") == "0000abcd"


def test_normalize_netid_rejects_junk():
    assert normalize_netid(None) is None
    assert normalize_netid("") is None
    assert normalize_netid("&") is None
    assert normalize_netid("nothex") is None


def test_units_parsing_normalizes_keys():
    # keys given in mixed forms/case all collapse to the same canonical id
    book = DeviceBook(units={
        "0x00900001": "Key Left",
        "&00900002": "Key Right",
        "01200001": "Back Wall",
        "junk": "dropped",       # un-parseable key is silently dropped
    })
    assert book.units == {
        "00900001": "Key Left",
        "00900002": "Key Right",
        "01200001": "Back Wall",
    }


def test_unit_name_lookup_by_int_and_string():
    book = DeviceBook(units={"00900002": "Key Right"})
    assert book.unit_name(0x00900002) == "Key Right"       # int
    assert book.unit_name("00900002") == "Key Right"       # bare hex
    assert book.unit_name("&00900002") == "Key Right"      # advert suffix
    assert book.unit_name("0x00900002") == "Key Right"     # 0x literal
    assert book.unit_name("0X00900002") == "Key Right"     # case-insensitive
    assert book.unit_name("00900003") is None              # miss -> None
    assert book.unit_name(None) is None                    # junk -> None


def test_units_make_book_truthy_and_load_parses_them(tmp_path):
    assert DeviceBook(units={"00900001": "Key Left"})      # units alone -> truthy
    p = tmp_path / "devices.toml"
    p.write_text(
        textwrap.dedent(
            """
            [units]
            "00900001" = "Key Left"
            "01200001" = "Back Wall"
            """
        )
    )
    book = devices.load(p.as_posix())
    assert book.unit_name("00900001") == "Key Left"
    assert book.unit_name(0x01200001) == "Back Wall"
