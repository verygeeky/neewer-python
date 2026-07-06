"""The pure Neewer protocol layer -- frame builders, the typed command model,
reply decoding, fixture-capability tables, and DMX maths.

Everything in this subpackage is **standard-library only**: it never imports
``bleak`` (or any transport). A project that drives the lights over a different
transport -- an ESP32 bridge, a UART gateway, a DMX node -- can depend on
``neewer.protocol`` for all the hard-won frame knowledge without pulling in a BLE
stack it will never use.

(The *string* grammar -- the ``<target> <action> [args]`` line parser -- lives in
the opt-in :mod:`neewer.grammar`, not here: it's a wire/REPL concern layered over
the typed model, not part of the pure protocol.)
"""
from __future__ import annotations

from . import commands, dmx, frames, models, replies

__all__ = ["frames", "commands", "replies", "models", "dmx"]
