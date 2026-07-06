"""Minimal hello-world: scan, connect everything in range, turn it all blue.

    python examples/hello.py

Needs a BLE adapter with a Neewer tube in range, in Bluetooth mode.
"""
import asyncio

from neewer import Fleet


async def main() -> None:
    async with Fleet() as fleet:                     # scan + connect everything in range
        print(await fleet.set_hsi("all", hue=240, sat=100, bri=100))   # all lights blue
        await asyncio.sleep(2)
        print(await fleet.power("all", on=False))    # then off


if __name__ == "__main__":
    asyncio.run(main())
