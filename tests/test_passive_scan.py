"""Tests for passive scanning (issue #2): the ``passive_scan`` knob and fallback.

No radio: ``name_or_patterns`` is pure, and the fallback path runs against the
inert ``bleak`` stub installed by conftest. Passive setup fails under that stub
(no ``bleak.args.bluez``), so the transport degrades to active scanning, the same
outcome as a real stack without ``bluetoothd --experimental``.
"""
from __future__ import annotations

import asyncio
import logging

from neewer.fleet import DEFAULT_PREFIXES, Fleet
from neewer.transport import _COMPLETE_LOCAL_NAME, BleakTransport, name_or_patterns


def run(coro):
    return asyncio.run(coro)


def test_name_or_patterns_maps_each_prefix():
    assert name_or_patterns(("NW-", "NEEWER")) == [
        (0, _COMPLETE_LOCAL_NAME, b"NW-"),
        (0, _COMPLETE_LOCAL_NAME, b"NEEWER"),
    ]


def test_name_or_patterns_covers_all_default_prefixes():
    contents = [content for _, _, content in name_or_patterns(DEFAULT_PREFIXES)]
    assert contents == [prefix.encode("ascii") for prefix in DEFAULT_PREFIXES]


def test_fleet_passes_passive_flag_to_default_transport():
    fleet = Fleet(passive_scan=True)
    assert isinstance(fleet.transport, BleakTransport)
    assert fleet.transport._passive is True
    assert fleet.transport._prefixes == DEFAULT_PREFIXES


def test_fleet_defaults_to_active():
    assert Fleet().transport._passive is False


def test_passive_without_prefixes_falls_back(caplog):
    transport = BleakTransport(passive_scan=True, prefixes=())
    with caplog.at_level(logging.WARNING, logger="neewer.transport"):
        run(transport.start_scan(lambda _adv: None))
    assert transport._scanner is not None  # an active scanner was still created
    assert "without name prefixes" in caplog.text


def test_passive_setup_failure_falls_back_to_active(caplog):
    transport = BleakTransport(passive_scan=True, prefixes=("NW-",))
    with caplog.at_level(logging.WARNING, logger="neewer.transport"):
        run(transport.start_scan(lambda _adv: None))
    assert transport._scanner is not None
    assert "falling back to active" in caplog.text


def test_active_scan_is_unaffected():
    transport = BleakTransport()
    assert transport._passive is False
    run(transport.start_scan(lambda _adv: None))
    assert transport._scanner is not None
