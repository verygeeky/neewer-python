"""Shared test fixtures and import-time stubs for the ``neewer`` suite.

Since the transport seam landed, :mod:`neewer.fleet` no longer imports ``bleak``
at module top — only :class:`neewer.transport.BleakTransport` does, lazily, when it
actually scans/connects. Tests inject a fake transport and never reach that path,
so ``bleak`` need not be installed. We still register a lightweight **inert** stub
``bleak`` in ``sys.modules`` as a safety net: if a test ever does trip the real
transport, the stub raises instead of touching a radio. Any test that exercises
Bluetooth behaviour does so against an explicit fake, never these stubs.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _install_bleak_stub() -> None:
    """Register a minimal fake ``bleak`` so imports resolve without the package.

    The stubs are deliberately inert: calling them is a test bug, so they raise
    if anyone tries to actually scan/connect. Tests that need BLE behaviour
    inject their own fakes.
    """
    if "bleak" in sys.modules:
        return
    bleak = types.ModuleType("bleak")

    class BleakClient:  # noqa: D401 - stub
        """Inert stand-in; real client behaviour is faked per-test."""

        def __init__(self, *args, **kwargs):
            self._args = args
            self._kwargs = kwargs

        async def connect(self, *a, **k):
            raise RuntimeError("stub BleakClient.connect must never run in tests")

        async def disconnect(self, *a, **k):
            return None

        async def write_gatt_char(self, *a, **k):
            raise RuntimeError("stub BleakClient.write_gatt_char must never run")

    class BleakScanner:  # noqa: D401 - stub
        """Inert stand-in for the scanner."""

        def __init__(self, *args, **kwargs):
            pass

        @staticmethod
        async def discover(*a, **k):
            raise RuntimeError("stub BleakScanner.discover must never run in tests")

        async def start(self):
            return None

        async def stop(self):
            return None

    bleak.BleakClient = BleakClient
    bleak.BleakScanner = BleakScanner

    # Mirror bleak's exception module so `from bleak.exc import BleakError`
    # resolves under the stub (the CLI's Bluetooth-unavailable detection needs
    # the class to exist even in a bleak-free test run).
    exc_module = types.ModuleType("bleak.exc")

    class BleakError(Exception):
        """Stub of bleak's base error; raised by tests, never by the stubs."""

    exc_module.BleakError = BleakError
    bleak.exc = exc_module
    sys.modules["bleak"] = bleak
    sys.modules["bleak.exc"] = exc_module


_install_bleak_stub()

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
