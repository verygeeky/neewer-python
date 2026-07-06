"""The typed API surface — structured args, typed errors, no string grammar.

Shows the primary way to drive the lights from Python: call the typed ``Fleet``
methods directly and catch typed :mod:`neewer.errors`. The string grammar
(``fleet.dispatch("all hsi 240 100 80")``) still works, but it's a convenience
layered on top of these.

    python examples/typed_api.py
"""
import asyncio

from neewer import Fleet
from neewer.errors import UnknownTarget, Unsupported


async def main() -> None:
    async with Fleet() as fleet:
        await fleet.set_cct("all", bri=80, temp=56)          # ~5600 K at 80 %
        await fleet.set_rgbcw("all", bri=60, r=0, g=0, b=0, c=255, w=0)  # cold white
        await fleet.scene("all", 3)                          # a built-in scene

        try:
            await fleet.pixel("all", ["0", "off", "240"])    # TL120C-only
        except Unsupported as exc:
            print("pixel not supported here:", exc)
        except UnknownTarget as exc:
            print("nothing connected:", exc)

        # Live state changes can be pushed instead of polled:
        unsubscribe = fleet.subscribe(lambda: print("state changed:", fleet.snapshot()))
        await fleet.query("all")                             # battery/version/…
        await asyncio.sleep(1)
        unsubscribe()


if __name__ == "__main__":
    asyncio.run(main())
