"""Typed command model — the single source of argument-order truth.

Each user-facing action is a small **frozen dataclass** with named, typed fields,
so the canonical argument order for an action ("HSI is hue, sat, bri") lives in
exactly one place. Everything that used to re-hardcode that order — the string
grammar (:mod:`neewer.grammar`), the HTTP JSON field-map, the MCP tool schemas,
and :class:`~neewer.fleet.Fleet`'s typed methods — constructs one of these objects
instead. Change an argument order here and every surface follows.

The objects also validate their own arguments at construction (ranges, gel brand)
and know how to build their wire frame(s) from :mod:`.frames`. They are **pure**:
no BLE, no transport, no I/O — importing this module never touches ``bleak``.

Two frame shapes appear, because the protocol has two addressing forms:

* **Direct-frame actions** (:class:`Power`, :class:`HSI`, :class:`CCT`,
  :class:`Brightness`, :class:`Raw`) expose ``frame()`` — one frame written to
  every addressed tube.
* **By-MAC actions** (:class:`RGBCW`, :class:`XY`, :class:`Gel`,
  :class:`Identify`, and the two-frame :class:`Pixel`) expose ``frame(mac6)`` /
  the ``*_frame(mac6)`` builders, because the target tube's own MAC is embedded
  in the payload. Each also names the :class:`~neewer.protocol.models.Capabilities`
  flag a fixture must have via :attr:`CAPABILITY`, so the fleet can skip-and-report
  fixtures that would silently ignore the frame.

:class:`Scene` straddles both: the same effect catalogue is carried by the direct
``0x88`` and the MAC-addressed ``0x91``, so it exposes both builders and the fleet
picks per fixture.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

from . import frames

# --- direct-frame actions (one frame to every addressed tube) -------------


@dataclass(frozen=True)
class Power:
    """Turn the light on or off (``0x81``)."""

    on: bool

    def frame(self) -> bytes:
        return frames.power(self.on)


@dataclass(frozen=True)
class HSI:
    """Hue / saturation / intensity colour (``0x86``).

    ``sat``/``bri`` default to full so ``HSI(240)`` is "blue at 100 %".
    """

    hue: int
    sat: int = 100
    bri: int = 100

    def frame(self) -> bytes:
        return frames.hsi(self.hue, self.sat, self.bri)


@dataclass(frozen=True)
class CCT:
    """White by colour temperature + brightness + green/magenta tint (``0x87``)."""

    bri: int
    temp: int
    gm: int = frames.GM_NEUTRAL

    def frame(self) -> bytes:
        return frames.cct(self.bri, self.temp, self.gm)


@dataclass(frozen=True)
class Brightness:
    """Brightness-only, kept deliberately simple: white-ish HSI at ``bri`` (``0x86``)."""

    bri: int

    def frame(self) -> bytes:
        return frames.hsi(0, 0, self.bri)


@dataclass(frozen=True)
class Raw:
    """A literal frame from a hex string — the fuzzing / replay escape hatch."""

    hexstr: str

    def frame(self) -> bytes:
        return frames.raw(self.hexstr)


# --- by-MAC actions (the tube's own MAC is embedded in the payload) -------


@dataclass(frozen=True)
class RGBCW:
    """RGB plus dedicated Cold/Warm white channels (``0xA9``, by-MAC).

    ``bri`` is 0..100; the five channels are each 0..255 and default to 0. The
    Cold/Warm channels mix a white a plain HSI hue can't.
    """

    #: Capability flag a fixture must carry for this frame to render.
    CAPABILITY = "rgbcw"

    bri: int
    r: int = 0
    g: int = 0
    b: int = 0
    c: int = 0
    w: int = 0

    def frame(self, mac6: bytes) -> bytes:
        return frames.rgbcw_by_mac(mac6, self.bri, self.r, self.g, self.b, self.c, self.w)


@dataclass(frozen=True)
class XY:
    """A CIE-1931 xy colour point (``0xB7``, by-MAC).

    ``x``/``y`` are floats in 0..1 (validated here — out of range is a client
    error, not a silently clamped one).
    """

    CAPABILITY = "xy"

    bri: int
    x: float
    y: float

    def __post_init__(self) -> None:
        if not (0.0 <= self.x <= 1.0) or not (0.0 <= self.y <= 1.0):
            raise ValueError(f"xy coordinates must be in 0..1, got x={self.x} y={self.y}")

    def frame(self, mac6: bytes) -> bytes:
        return frames.xy_by_mac(mac6, self.bri, self.x, self.y)


@dataclass(frozen=True)
class Gel:
    """A lighting-gel colour: HSI plus gel brand + catalogue-number metadata
    (``0xAD``, by-MAC).

    ``brand`` is the numeric brand byte (:data:`.frames.GEL_BRAND_ROSCO` /
    :data:`.frames.GEL_BRAND_LEE`); the string ``rosco``/``lee`` spelling is a
    grammar concern parsed upstream. A brand outside the known set is rejected.
    """

    CAPABILITY = "gel"

    hue: int
    sat: int
    bri: int
    brand: int = frames.GEL_BRAND_ROSCO
    gel_no: int = 0

    def __post_init__(self) -> None:
        if self.brand not in (frames.GEL_BRAND_ROSCO, frames.GEL_BRAND_LEE):
            raise ValueError(
                f"gel brand must be {frames.GEL_BRAND_ROSCO} (rosco) or "
                f"{frames.GEL_BRAND_LEE} (lee), got {self.brand!r}")

    def frame(self, mac6: bytes) -> bytes:
        return frames.gel_by_mac(mac6, self.hue, self.sat, self.bri, self.brand, self.gel_no)


@dataclass(frozen=True)
class Identify:
    """Flash the light to physically locate it (``0x99``, by-MAC). No arguments."""

    def frame(self, mac6: bytes) -> bytes:
        return frames.identify(mac6)


@dataclass(frozen=True)
class Scene:
    """A built-in scene / FX effect (``0x88`` direct, or ``0x91`` by-MAC).

    Both transports carry the same effect catalogue and the ``<effect> <params…>``
    tail is identical, so this object exposes both builders; the fleet picks the one
    a given fixture actually honours (the TL120C drops ``0x88`` but runs ``0x91``).
    """

    effect: int
    params: tuple[int, ...] = ()

    def legacy_frame(self) -> bytes:
        return frames.scene(self.effect, *self.params)

    def mac_frame(self, mac6: bytes) -> bytes:
        return frames.scene_by_mac(mac6, self.effect, *self.params)


@dataclass(frozen=True)
class Pixel:
    """A per-segment pixel palette (``0xB0``, by-MAC, TL120C).

    ``colors`` is one token per segment band — a hue ``0-359``, ``off`` (dark), or
    ``k<kelvin>``. The effect is sent as two frames (params, then the palette); the
    palette is chunked to the ATT payload cap by the fleet. A bad colour token is
    rejected when :meth:`palette_frame` builds the palette.
    """

    CAPABILITY = "pixel"

    colors: tuple[str, ...]
    effect: int = 1

    def __post_init__(self) -> None:
        if not self.colors:
            raise ValueError("no pixel colours")

    def params_frame(self, mac6: bytes) -> bytes:
        return frames.pixel_params(mac6, self.effect)

    def palette_frame(self, mac6: bytes) -> bytes:
        return frames.pixel_palette(mac6, list(self.colors), self.effect)


# --- action registry: the single source of argument-order truth -----------


class ActionSpec(NamedTuple):
    """The wire-facing argument spec for one grammar action.

    * ``command`` — the dataclass above that owns the canonical argument order.
    * ``fields`` — the *wire-facing* names of the scalar arguments, in that order.
      These are what a transport spells (HSI is ``h``/``s``/``i``, not the
      dataclass' ``hue``/``sat``/``bri``) and, crucially, the order a positional
      transport sends them in.
    * ``variadic`` — the name of a trailing list argument (scene ``params``, pixel
      ``colors``) that a transport appends after the scalars, or ``None``.
    """

    command: type
    fields: tuple[str, ...]
    variadic: str | None = None


#: Grammar action verb -> its :class:`ActionSpec`. This is the ONE place the
#: wire-facing argument order lives; the HTTP JSON field-map derives its table from
#: here and the MCP tool signatures are pinned to it by a test, so a dataclass
#: field reorder can't silently desync a transport (the old bug: four hand-kept
#: copies of the order in commands / grammar / http / mcp). ``pixel``'s ``effect``
#: is intentionally not wire-exposed (it defaults to 1), so it is absent here.
ACTIONS: dict[str, ActionSpec] = {
    "power": ActionSpec(Power, ("on",)),
    "hsi": ActionSpec(HSI, ("h", "s", "i")),
    "cct": ActionSpec(CCT, ("bri", "temp", "gm")),
    "bri": ActionSpec(Brightness, ("bri",)),
    "scene": ActionSpec(Scene, ("effect",), variadic="params"),
    "pixel": ActionSpec(Pixel, (), variadic="colors"),
    "rgbcw": ActionSpec(RGBCW, ("bri", "r", "g", "b", "c", "w")),
    "xy": ActionSpec(XY, ("bri", "x", "y")),
    "gel": ActionSpec(Gel, ("hue", "sat", "bri", "brand", "gel_no")),
    "identify": ActionSpec(Identify, ()),
    "raw": ActionSpec(Raw, ("hexstr",)),
}
