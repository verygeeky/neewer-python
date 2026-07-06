"""neewer -- a Python control library for Neewer TL-series RGB tube lights.

Talk to the lights over Bluetooth LE with no app and no pairing. The whole
hello-world is a handful of lines -- scan for every light in range, connect, and
call a typed method::

    import asyncio
    from neewer import Fleet

    async def main():
        async with Fleet() as fleet:            # scan + connect everything in range
            await fleet.set_hsi("all", 240, 100, 100)   # all lights blue

    asyncio.run(main())

The typed methods (``set_hsi``/``power``/``set_cct``/…) are the primary API; the
string grammar (``fleet.dispatch("all hsi 240 100 100")``) is a convenience over
them, in the opt-in :mod:`neewer.grammar`.

Layout:

* :mod:`neewer.protocol` -- pure, stdlib-only frame/reply/model knowledge and the
  typed command model (importable without a BLE stack; bring your own transport).
* :mod:`neewer.transport` -- the radio seam: a ``Transport`` Protocol + the
  bleak-backed default, injected into ``Fleet``.
* :class:`neewer.Fleet` -- the batteries-included BLE client: discovery,
  persistent connections, an auto-reconnect supervisor, addressing, typed methods,
  and a change-event ``subscribe()`` API.
* :mod:`neewer.errors` -- typed command errors (``UnknownTarget``/``Unsupported``/…).
* :mod:`neewer.grammar` -- the opt-in ``<target> <action> [args]`` string grammar.
* :mod:`neewer.effects` -- animation engines that run against a held ``Fleet``.
* :mod:`neewer.catalog` -- the machine-readable protocol catalogue (actions,
  scenes, pixel effects, flow options, gel brands) for discovery endpoints/UIs.
* :mod:`neewer.devices` -- the device book (aliases / positions / groups).

``Fleet``/``Tube`` are imported lazily (PEP 562), and even ``neewer.fleet`` no
longer imports ``bleak`` at module top -- only the injected transport does, and
lazily -- so both the pure protocol layer and the fleet import cleanly without a
radio stack present.
"""
from __future__ import annotations

from . import catalog, devices, effects, errors, grammar, protocol  # stdlib-only, no bleak

__version__ = "0.1.0"

__all__ = ["Fleet", "Tube", "protocol", "catalog", "effects", "devices", "errors",
           "grammar", "__version__"]

#: Lazily-loaded names -> the submodule that defines them. Touching ``bleak`` is
#: deferred to first access so the pure ``neewer.protocol`` import stays clean.
_LAZY = {"Fleet": "fleet", "Tube": "fleet"}


def __getattr__(name: str):
    """PEP 562 lazy attribute access for the bleak-backed client classes."""
    module = _LAZY.get(name)
    if module is not None:
        import importlib

        return getattr(importlib.import_module(f".{module}", __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
