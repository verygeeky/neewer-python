"""Fixture models and their capabilities — the daemon's per-model awareness.

The lights don't reliably self-identify their model over BLE. A tube's model is
resolved **in priority order**:

1. an explicit ``[models]`` entry in the shared device book (always wins), else
2. the advertised **name** decoded to a model (:func:`name_model` — the app's own
   fixture decoder, ported in :mod:`neewer.protocol.identity`; this is authoritative
   and needs no connection), else
3. inference from the firmware version we query on connect (:data:`VERSION_MODELS`),
   else
4. ``None`` -> the permissive :data:`GENERIC` capability set.

The name decoder (2) is the robust identifier: the advertised ``NW-<serial>`` batch
code maps straight to a model, so a tube self-identifies from its advertisement alone
— no query round-trip, and no "generic until first version reply" window. Firmware-
version inference (3) stays as a fallback for oddly-named fixtures.

The point of knowing the model is to stop frames that *silently no-op* on a given
fixture — most notably: the **TL120C ignores the legacy ``0x88`` scene**, and
**pixel (``0xB0``) is a TL120C feature**. So we gate those two. Everything we're
*not* sure about stays permissive (assume supported), so awareness never makes the
daemon *less* capable than before — only more honest where we have evidence.

Only mark a fixture as lacking something when there's real evidence, and record
why in a comment.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import identity


@dataclass(frozen=True)
class Capabilities:
    """What a fixture model can do. Defaults are permissive (assume yes)."""

    #: The ``0xB0`` per-segment pixel palette. TL120C family only (verified live);
    #: default off because it's a special feature, not something to send blind.
    pixel: bool = False
    #: The legacy ``0x88`` built-in scene works. The TL120C's LED-MCU firmware has
    #: no ``0x88`` handler, so it silently ignores it — gate that. Default on.
    scene_legacy: bool = True
    #: The MAC-addressed ``0x91`` scene (inner ``0x8b``) works. This is how the
    #: TL120C runs built-in scenes; the daemon prefers it over ``0x88`` when a
    #: fixture lacks the legacy path. Default off — only enable where confirmed.
    scene_mac: bool = False
    #: The MAC-addressed ``0xA9`` RGBCW colour (inner ``0xa8``): RGB plus dedicated
    #: Cold/Warm white channels. **By-MAC only on the whole CE line** — the direct ``0xA8``
    #: is dropped by every fixture tested (TL120C-2 + TL90C, verified live).
    #: Default **on**: direct is dead everywhere and a by-MAC frame no-ops harmlessly on a
    #: fixture that lacks the mode, so permitting it never makes the daemon less capable.
    rgbcw: bool = True
    #: The MAC-addressed ``0xB7`` CIE-1931 xy colour point. By-MAC only on the CE line —
    #: the direct ``0xB9`` is dropped (TL120C-2 + TL90C, verified live). Default on.
    xy: bool = True
    #: The MAC-addressed ``0xAD`` gel / colour-paper colour (HSI + brand metadata).
    #: By-MAC only on the CE line — the direct ``0xAF`` is dropped (TL120C-2 + TL90C,
    #: verified live). Default on.
    gel: bool = True
    #: The realtime streamer: ``0xC0`` group play / ``0xBF`` multi-fixture download.
    #: **TL60-only** — the TL120C has no handler and goes black. A fixture advertises it
    #: via the ``0xC4`` support query, which the TL60 answers with reply ``0x17`` = ``01``
    #: (verified live); the TL120C doesn't reply. Default off. (Playing a stream needs
    #: ≥2 fixtures, so single-unit rendering isn't verified.)
    streamer: bool = False


#: The permissive fallback for an unknown/unidentified fixture: assume the common features
#: work — including the by-MAC colour modes (rgbcw/xy/gel are default-on; see above) —
#: except pixel (a TL120C-specific extra we won't send blind). A fixture *known* to lack a
#: colour mode gets an explicit ``Capabilities(rgbcw=False, …)`` entry in MODELS.
GENERIC = Capabilities()

#: model name -> capabilities. Keep this evidence-based; unknown models fall back
#: to GENERIC. (Per-model capability notes live in the companion protocol docs at
#: https://github.com/verygeeky/neewer-hardware; only fixtures whose quirks we've
#: confirmed need an entry here.)
MODELS: dict[str, Capabilities] = {
    # TL120C: ignores the 0x88 scene, but it does run the by-MAC 0x91 scene; has pixel
    # 0xB0; and the by-MAC colour modes rgbcw/xy/gel (direct forms dropped). Verified live.
    "TL120C": Capabilities(pixel=True, scene_legacy=False, scene_mac=True,
                           rgbcw=True, xy=True, gel=True),
    "TL120C-2": Capabilities(pixel=True, scene_legacy=False, scene_mac=True,
                             rgbcw=True, xy=True, gel=True),
    # TL90C: by-MAC rgbcw/xy/gel human-confirmed live — 0xA9 red / 0xB7 white
    # / 0xAD amber all rendered; every direct form (0xA8/0xB9) ignored. So by-MAC-only is a
    # CE-line trait, not a TL120C quirk. pixel unconfirmed (leave off); 0x88 unconfirmed ->
    # permissive.
    "TL90C": Capabilities(pixel=False, scene_legacy=True,
                          rgbcw=True, xy=True, gel=True),
    # TL60 RGB: streamer-capable (0xC0/0xBF) — confirmed live via the 0xC4 support
    # query (reply 0x17 = 01); the TL120C is silent on 0xC4. It's an RGB
    # panel, not a tube: pixel off; the remaining scene/by-MAC-colour flags are
    # permissive defaults (unverified on this fixture — a by-MAC frame no-ops harmlessly
    # if unsupported).
    "TL60 RGB-2": Capabilities(streamer=True),
    "TL60 RGB-3": Capabilities(streamer=True),
}

#: firmware version -> model, for inference when no config model is declared.
#: Extend as versions are confirmed.
VERSION_MODELS: dict[str, str] = {
    "1.2.8": "TL120C",
    "2.0.5": "TL120C-2",
    "1.1.9": "TL90C",     # older TL90C firmware (same NW-20240012 family; verified live)
    "1.1.11": "TL90C",
    "2.4.8": "TL60 RGB-2",
    # 3.0.3 self-reports the ASCII tail "RGB-3" in its 0x08 version reply (verified live).
    "3.0.3": "TL60 RGB-3",
    "3.0.5": "TL60 RGB-3",
}


def capabilities(model: str | None) -> Capabilities:
    """Return the capability set for ``model`` (``GENERIC`` if unknown/None)."""
    return MODELS.get(model or "", GENERIC)


def infer_model(version: str | None) -> str | None:
    """Best-effort model from a firmware version string, or ``None`` if unknown."""
    return VERSION_MODELS.get(version or "")


def name_model(name: str | None, mac: str | None = None) -> str | None:
    """Model from the advertised BLE name via the app's decoder, or ``None`` if unknown.

    This is the authoritative, connection-free identifier (see module docstring): the
    ``NW-<serial>`` batch code in the advertisement resolves straight to a model. Used
    at discovery time, above firmware-version inference.
    """
    return identity.model_for_name(name, mac)
