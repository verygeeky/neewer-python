"""Tests for :mod:`neewer.cli` — the reference command-line tool.

The interesting behaviour is the failure UX: a machine with no usable Bluetooth
(a VM without an adapter, BlueZ not running) must produce a one-screen
diagnosis, not a raw traceback — unless NEEWER_DEBUG asks for one. Fleet is
faked; no BLE stack is touched.
"""
from __future__ import annotations

import pytest
from bleak.exc import BleakError

import neewer.fleet as fleet_mod
from neewer import cli

#: The error BlueZ-less Linux produces (verbatim shape from a real VM run).
_NO_BLUEZ = BleakError(
    "[org.freedesktop.DBus.Error.ServiceUnknown] "
    "The name org.bluez was not provided by any .service files")


class _UnavailableFleet:
    """A Fleet whose startup fails the way bleak does without a BLE stack."""

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        raise _NO_BLUEZ

    async def __aexit__(self, *exc):
        return False


class _BrokenFleet:
    """A Fleet whose startup fails with a non-Bluetooth error."""

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        raise ValueError("unrelated bug")

    async def __aexit__(self, *exc):
        return False


def test_help_exits_zero():
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--help"])
    assert excinfo.value.code == 0


def test_no_bluetooth_prints_diagnosis_not_traceback(monkeypatch, capsys):
    monkeypatch.setattr(fleet_mod, "Fleet", _UnavailableFleet)
    rc = cli.main(["scan"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "Bluetooth is unavailable" in err
    assert "org.bluez" in err                 # the underlying detail survives
    assert "NEEWER_DEBUG" in err              # and the escape hatch is advertised


def test_no_bluetooth_with_debug_reraises(monkeypatch):
    monkeypatch.setattr(fleet_mod, "Fleet", _UnavailableFleet)
    monkeypatch.setenv("NEEWER_DEBUG", "1")
    with pytest.raises(BleakError):
        cli.main(["scan"])


def test_non_bluetooth_errors_still_raise(monkeypatch):
    # Only the Bluetooth-unavailable family gets the friendly treatment;
    # anything else is a real bug and must surface loudly.
    monkeypatch.setattr(fleet_mod, "Fleet", _BrokenFleet)
    with pytest.raises(ValueError):
        cli.main(["scan"])
