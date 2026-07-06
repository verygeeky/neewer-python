"""``neewer`` -- the reference command-line tool.

A thin terminal wrapper over :class:`neewer.Fleet`: it scans for every light in
range, holds them for one command, and exits. This is the fastest way to prove
the library works against real hardware::

    neewer scan                        # list the lights it can see
    neewer all hsi 240 100 100         # everything blue
    neewer all power off
    neewer t1 cct 100 5600

Anything after the program name is passed verbatim as one command line to
:meth:`Fleet.dispatch`, so the full grammar (``all`` / ``t<N>`` / MAC targets,
``hsi`` / ``cct`` / ``power`` / ``pixel`` / ``flow`` ...) is available.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys


async def _run(line: str) -> int:
    # Imported lazily so ``neewer --help`` works even without a BLE stack present.
    from .fleet import Fleet

    async with Fleet() as fleet:
        if line.strip() == "scan":
            print(json.dumps(fleet.snapshot(), indent=2))
            return 0
        result = await fleet.dispatch(line)
        print(result)
        # A "no tubes ..." / "unknown ..." reply is a failure the shell should see.
        return 0 if result.startswith("ok") else 1


def _bluetooth_unavailable(exc: BaseException) -> bool:
    """Is ``exc`` bleak reporting an unusable Bluetooth stack/adapter?

    Imported lazily so this module (and ``neewer --help``) works even where
    ``bleak`` is not installed — in which case nothing bleak-shaped can have
    been raised, so the answer is no.
    """
    try:
        from bleak.exc import BleakError
    except Exception:
        return False
    return isinstance(exc, BleakError)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="neewer",
        description="Control Neewer TL-series lights over BLE. "
        "Give a command line, e.g. \"all hsi 240 100 100\", or \"scan\".",
    )
    parser.add_argument(
        "command",
        nargs="+",
        help='a command line, e.g. "all hsi 240 100 100" or "scan"',
    )
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run(" ".join(args.command)))
    except Exception as exc:
        # A machine with no usable Bluetooth (a VM without an adapter, BlueZ not
        # running, the radio switched off) is a first-run situation, not a bug —
        # answer with a diagnosis instead of a 40-line traceback.
        if os.environ.get("NEEWER_DEBUG") or not _bluetooth_unavailable(exc):
            raise
        print(f"neewer: Bluetooth is unavailable: {exc}", file=sys.stderr)
        print(
            "  Check that:\n"
            "  - this machine has a Bluetooth adapter at all (a VM usually needs\n"
            "    the host's adapter passed through — there is none by default)\n"
            "  - Linux: BlueZ is installed and running (systemctl status bluetooth)\n"
            "  - Windows / macOS: Bluetooth is switched on in system settings\n"
            "  Set NEEWER_DEBUG=1 for the full traceback.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
