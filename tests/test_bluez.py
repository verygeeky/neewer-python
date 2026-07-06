"""Tests for :mod:`neewer.bluez` — the stale-link matcher.

Only the pure decision (`is_ours`) is unit-tested; the D-Bus disconnect path is
best-effort I/O verified live against a real BlueZ.
"""
from __future__ import annotations

from neewer import bluez

PREFIXES = ("NW-", "NWR", "NEEWER")
MACS = {"AA:BB:CC:DD:EE:01"}


def test_matches_by_configured_mac_case_insensitive():
    assert bluez.is_ours("aa:bb:cc:dd:ee:01", "", PREFIXES, MACS) is True


def test_matches_by_name_prefix():
    assert bluez.is_ours("AA:BB:CC:DD:EE:FF", "NW-20240047", PREFIXES, MACS) is True


def test_rejects_unrelated_device():
    # a different vendor's connected device must never be disconnected
    assert bluez.is_ours("11:22:33:44:55:66", "Klipsch The Fives", PREFIXES, MACS) is False


def test_rejects_non_claimed_prefix():
    # NHX9S ('NH') isn't claimed when prefixes is NW-only
    assert bluez.is_ours("65:AA:CC:64:CC:C3", "NHX9S", ("NW-",), set()) is False


def test_empty_name_and_unknown_mac_is_not_ours():
    assert bluez.is_ours("00:00:00:00:00:00", "", PREFIXES, MACS) is False
