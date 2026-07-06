"""Typed command errors — the library's error model.

Every command failure raises one of these instead of returning a sentinel string,
so callers branch on a **type** (and a transport maps that type to a status) rather
than sniffing a message prefix. Each carries a human-readable message that matches
the historical wording, so a transport that simply echoes ``str(exc)`` — the socket
line protocol, the OSC/MQTT logs — reads exactly as it did before.

The classes are pure: no HTTP, no BLE. A transport owns the status mapping (see the
daemon's ``http`` module, which maps :class:`UnknownTarget`/:class:`UnknownPreset`
→ 404, :class:`Unsupported` → 422, and the rest → 400). The library only says
*what* went wrong, not *how* a given protocol should report it.
"""
from __future__ import annotations


class NeewerError(Exception):
    """Base class for every command error the library raises."""


class UnknownTarget(NeewerError):
    """A target resolved to no connected tubes (all / t<N> / alias / MAC / group)."""

    def __init__(self, target: str):
        self.target = target
        super().__init__(f"no tubes for target {target!r}")


class UnknownAction(NeewerError):
    """The command names an action the grammar doesn't know."""

    def __init__(self, action: str):
        self.action = action
        super().__init__(f"unknown action {action!r}")


class UnknownEffect(NeewerError):
    """``flow`` names an effect that isn't in the registry."""

    def __init__(self, mode: str):
        self.mode = mode
        super().__init__(f"unknown effect {mode!r}")


class UnknownPreset(NeewerError):
    """``preset`` names a preset that isn't configured."""

    def __init__(self, name: str):
        self.name = name
        super().__init__(f"no preset {name!r}")


class Unsupported(NeewerError):
    """The command was well-formed but *no* addressed fixture can perform it.

    Raised only when every resolved tube was skipped for lack of the capability. A
    *partial* application — some tubes sent, some skipped — is a success reported
    with detail ("ok pixel -> 1 tube(s) (1 lack pixel support)"), not this error.
    """

    def __init__(self, message: str):
        super().__init__(message)
