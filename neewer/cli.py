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
    return asyncio.run(_run(" ".join(args.command)))


if __name__ == "__main__":
    raise SystemExit(main())
