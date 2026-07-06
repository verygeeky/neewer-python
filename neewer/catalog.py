"""Machine-readable protocol catalogue — pure data, no I/O, no radio.

This module answers "what exists?" for every upper layer (the daemon's
``GET /api/v1/catalog``, MCP tools, UIs), so no client ever hard-codes protocol
knowledge again:

* :func:`actions` — the wire-facing argument schema for every grammar action,
  **derived** from :data:`neewer.protocol.commands.ACTIONS` (the single source of
  argument-order truth — nothing is re-typed here).
* :data:`SCENES` / :data:`SCENE_ID_SETS` — the built-in scene/FX engine
  (``0x88`` direct / ``0x91`` by-MAC, same catalogue): effect id -> name +
  ordered parameter specs, plus the per-model id subsets the app ships.
* :data:`PIXEL_EFFECTS` / :data:`PIXEL_PALETTE` — the ``0xB0`` pixel engine:
  effect id -> name + subIndex-0 scalar-block layout, and the shared 3-byte
  palette-slot encoding. Metadata only — the frame builders are unchanged.
* :data:`FLOW_MODES` — the host-side flow engines' option metadata (from
  :data:`neewer.effects.PARAMS`).
* :data:`GEL_BRANDS` — the gel brand-code map (derived from the frame-layer
  constants).
* :func:`catalog` — all of the above as one JSON-serialisable blob.

Every table entry carries a ``confidence`` flag: :data:`CONFIRMED` means a human
watched real hardware do it; :data:`EXPERIMENTAL` means the layout/ranges match
what the official app (NEEWER Studio 5.7.0) sends but still await live
verification. Downstream layers must keep that distinction visible (hardware
passes flip entries to confirmed one by one).

Like the rest of :mod:`neewer.protocol`, importing this module never touches
``bleak`` — it is pure data over the stdlib.
"""
from __future__ import annotations

from . import effects
from .protocol import commands, frames

#: Bump when the catalogue's *shape* changes (clients may cache per version).
CATALOG_VERSION = 1

#: A human watched real hardware do it (the evidence-table "Human" bar).
CONFIRMED = "confirmed"
#: Matches the official app's behaviour; layout/ranges not yet verified on hardware.
EXPERIMENTAL = "experimental"


# --- actions: derived from the typed command registry ----------------------

def actions() -> dict[str, dict]:
    """The wire-facing argument schema per grammar action, from ``commands.ACTIONS``.

    Derived — never re-typed — so a registry change flows through automatically
    (re-hardcoding the field lists here would reintroduce exactly the
    four-hand-kept-copies drift the registry exists to kill).
    """
    return {
        name: {"fields": list(spec.fields), "variadic": spec.variadic}
        for name, spec in commands.ACTIONS.items()
    }


# --- scene / FX engine (0x88 direct, 0x91 by-MAC — one shared catalogue) ---

def _p(name: str, lo: int, hi: int, unit: str = "", **extra) -> dict:
    """One ordered scene/pixel parameter spec: name, range, unit (+ extras)."""
    spec = {"name": name, "min": lo, "max": hi, "unit": unit}
    spec.update(extra)
    return spec


def _bri(name: str = "bri") -> dict:
    """Brightness/intensity 0..100 (the app's INT)."""
    return _p(name, 0, 100, "%")


def _cct(name: str = "cct") -> dict:
    """Colour temperature in hundreds of Kelvin, 32..85 (3200..8500 K)."""
    return _p(name, 32, 85, "x100K")


def _gm(name: str = "gm") -> dict:
    """Green/magenta tint 0..100, 50 neutral."""
    return _p(name, 0, 100, "", note="50 neutral")


def _hue(name: str = "hue") -> dict:
    """A hue 0..360 sent as **two** payload bytes (LE16) — one logical param."""
    return _p(name, 0, 360, "deg", bytes=2, encoding="le16")


def _sat(name: str = "sat") -> dict:
    """Saturation 0..100."""
    return _p(name, 0, 100, "%")


def _byte(name: str) -> dict:
    """An unsigned byte whose usable range the app enforces upstream (in its UI
    seekbars, not the builder) — the exact slider range is unpinned until fuzzed
    live (A.20.3)."""
    return _p(name, 0, 255, "", note="raw byte; UI range unpinned")


def _scene(name: str, label: str, params: list[dict],
           confidence: str = EXPERIMENTAL) -> dict:
    """One scene-catalogue row."""
    return {"name": name, "label": label, "params": params,
            "confidence": confidence}


#: Scene effect id -> name + ordered parameter specs. Payload layouts match what
#: the official app sends per effect: ids 16-18 exist, and id 14's leading byte
#: is a CCT-vs-HSI mode selector (not a constant 0). Only id 1 is
#: hardware-confirmed end-to-end (Lightning live: ``0x88`` on the TL90C, ``0x91``
#: on the TL120C-2, params byte-for-byte); the rest are experimental.
SCENES: dict[int, dict] = {
    1: _scene("lightning", "Lightning", [_bri(), _cct(), _byte("rate")], CONFIRMED),
    2: _scene("paparazzi", "Paparazzi", [_bri(), _cct(), _gm(), _byte("rate")]),
    3: _scene("defective_bulb", "Defective bulb",
              [_bri(), _cct(), _gm(), _byte("rate")]),
    4: _scene("explosion", "Explosion",
              [_bri(), _cct(), _gm(), _byte("rate"), _byte("ember")]),
    5: _scene("welding", "Welding",
              [_bri("bri_min"), _bri("bri_max"), _cct(), _gm(), _byte("rate")]),
    6: _scene("cct_flash", "CCT flash", [_bri(), _cct(), _gm(), _byte("rate")]),
    7: _scene("hue_flash", "Hue flash", [_bri(), _hue(), _sat(), _byte("rate")]),
    8: _scene("cct_pulse", "CCT pulse", [_bri(), _cct(), _gm(), _byte("rate")]),
    9: _scene("hue_pulse", "Hue pulse", [_bri(), _hue(), _sat(), _byte("rate")]),
    10: _scene("cop_car", "Cop car",
               # colour-set selector: red / blue / red-blue / white-blue /
               # red-white-blue (the app's COLOR_R..COLOR_RWB presets).
               [_bri(), _p("color_num", 0, 4, "", note="colour-set selector"),
                _byte("rate")]),
    11: _scene("candlelight", "Candlelight",
               [_bri("bri_min"), _bri("bri_max"), _cct(), _gm(), _byte("rate"),
                _byte("ember")]),
    12: _scene("hue_loop", "Hue loop",
               [_bri(), _hue("hue_min"), _hue("hue_max"), _byte("rate")]),
    13: _scene("cct_loop", "CCT loop",
               [_bri(), _cct("cct_min"), _cct("cct_max"), _byte("rate")]),
    14: _scene("int_loop", "INT loop",
               # Leading byte selects CCT vs HSI mode (an earlier reading
               # mistook it for a constant 0).
               [_p("cct_hsi_num", 0, 1, "", note="0=CCT / 1=HSI mode selector"),
                _bri("bri_min"), _bri("bri_max"), _hue(), _cct(), _byte("rate")]),
    15: _scene("tv_screen", "TV screen",
               [_bri("bri_min"), _bri("bri_max"), _cct(), _gm(), _byte("rate")]),
    16: _scene("fireworks", "Fireworks",
               [_bri(), _byte("mode_num"), _byte("rate"), _byte("ember")]),
    17: _scene("party", "Party", [_bri(), _byte("mode_num"), _byte("rate")]),
    18: _scene("music", "Music",
               # The host-streamed placeholder: the app drives colour live and
               # this effect only carries the brightness.
               [_bri()]),
}

#: Per-model scene id subsets: a fixture advertising an N-scene catalogue
#: supports exactly these ids, so a UI must not offer id 16 to a 9-scene
#: fixture. Keyed by the scene count the fixture advertises.
SCENE_ID_SETS: dict[int, tuple[int, ...]] = {
    9: (1, 2, 3, 4, 5, 6, 8, 14, 15),
    10: (1, 2, 3, 4, 5, 6, 8, 14, 15),
    12: (1, 2, 3, 4, 5, 6, 8, 11, 13, 14, 15, 16),
    13: (1, 2, 3, 4, 5, 6, 8, 11, 13, 14, 15, 16, 18),
    17: tuple(range(1, 18)),
    18: tuple(range(1, 19)),
}


# --- pixel engine (0xB0 by-MAC, TL120C) ------------------------------------

#: The pixel ``runningStatus`` transport enum (``RunningStatus.java:10-13``).
RUNNING_STATUS: dict[int, str] = {0: "stop", 1: "play", 2: "pause", 3: "continue"}


def _pixel_common() -> list[dict]:
    """The scalar specs shared by most pixel effects (per A.20.1/A.20.2)."""
    return [
        _bri("brightness"),
        # active palette-slot count; the app default is 4, palette max is 8.
        _p("color_number", 1, 8, "slots"),
        # a wide raw byte, NOT 0-10 — the app default is 20 (capture: 46) and
        # the builder doesn't clamp; the app's UI seekbar bounds it upstream.
        _byte("speed"),
        _p("direction", 0, 1, ""),
        _p("running_status", 0, 3, "", enum=RUNNING_STATUS),
    ]


def _pixel(name: str, label: str, scalars: list[dict],
           confidence: str = EXPERIMENTAL) -> dict:
    """One pixel-effect row: the subIndex-0 scalar block after ``[id, 0x00]``."""
    return {"name": name, "label": label, "scalars": scalars,
            "confidence": confidence}


#: The 7-scalar block shared by the three "moving" effects (wire ids 3/4/5).
_PIXEL_MOVING: list[dict] = [
    _bri("color_brightness"), _bri("background_brightness"), _byte("way"),
    _byte("speed"), _p("direction", 0, 1, ""), _byte("movement"),
    _p("running_status", 0, 3, "", enum=RUNNING_STATUS),
]

#: Pixel effect **wire** id -> name + subIndex-0 scalar-block layout. Wire ids
#: are 1-7 and 10-12 (the app remaps its internal ids 8/9/10 to 10/11/12 on the
#: wire). Effect 1's block is hardware-confirmed (``01 00 32 02 2e 01 01``
#: replayed live); the per-effect variants are experimental until each is
#: verified individually on hardware.
PIXEL_EFFECTS: dict[int, dict] = {
    1: _pixel("color_replacement", "Color replacement", _pixel_common(), CONFIRMED),
    2: _pixel("color_alternate", "Color alternate", [
        _bri("brightness"), _byte("speed"), _p("direction", 0, 1, ""),
        _byte("transition"), _p("running_status", 0, 3, "", enum=RUNNING_STATUS),
    ]),
    3: _pixel("single_color_moving", "Single color moving", _PIXEL_MOVING),
    4: _pixel("two_color_moving", "Two color moving", _PIXEL_MOVING),
    5: _pixel("three_color_moving", "Three color moving", _PIXEL_MOVING),
    6: _pixel("colorful", "Colorful", [
        _bri("brightness"), _byte("speed"), _p("direction", 0, 1, ""),
        _p("running_status", 0, 3, "", enum=RUNNING_STATUS),
    ]),
    7: _pixel("fire", "Fire", [
        _bri("brightness_min"), _bri("brightness_max"),
        _bri("background_brightness"), _byte("speed"),
        _p("orientation", 0, 1, ""),
        _p("running_status", 0, 3, "", enum=RUNNING_STATUS),
    ]),
    10: _pixel("color_gradient", "Color gradient", [
        _bri("brightness"), _p("color_number", 1, 8, "slots"), _byte("speed"),
        _p("direction", 0, 1, ""), _byte("section_type"),
        _p("running_status", 0, 3, "", enum=RUNNING_STATUS),
    ]),
    11: _pixel("trail", "Trail", [
        _bri("brightness"), _p("color_number", 1, 8, "slots"), _byte("speed"),
        _p("direction", 0, 1, ""), _byte("brightness_sat_type"),
        _p("running_status", 0, 3, "", enum=RUNNING_STATUS),
    ]),
    12: _pixel("color_shift", "Color shift", _pixel_common()),
}

#: The pixel palette shape: up to 8 slots, each a 3-byte cell in a shared
#: encoding — every slot carries a per-slot gm (CCT) or sat (HSI) byte.
#: Hardware-confirmed (palettes replayed and crafted live, off-blocks included).
PIXEL_PALETTE: dict = {
    "max_slots": 8,
    "slot_modes": {
        # [0x00, cct, gm] — white with per-slot green/magenta tint.
        "cct": {"flag": 0x00, "fields": [_cct(), _gm()]},
        # [(hue>>8)|0x10, hue&0xFF, sat] — the 0x10 nibble flags HSI; the hue's
        # high bits ride in the flag byte's low nibble, the low byte follows
        # (NOT the LE16 split the direct 0x86 frame uses).
        "hsi": {"flag": 0x10,
                "fields": [_p("hue", 0, 360, "deg", bytes=2,
                              encoding="flag_nibble_be"), _sat()]},
        # [0x20, 0, 0] — the slot is dark.
        "off": {"flag": 0x20, "fields": []},
    },
    "confidence": CONFIRMED,
}


# --- host-side flow engines -------------------------------------------------

#: Flow mode -> option metadata, straight from the effect registry's PARAMS
#: companion (:data:`neewer.effects.PARAMS`). Host-side code, so always
#: "confirmed" — there is no wire layout to doubt.
FLOW_MODES: dict[str, dict] = {
    mode: {"params": params} for mode, params in effects.PARAMS.items()
}


# --- gel brands --------------------------------------------------------------

#: Gel brand byte -> brand name, derived from the frame-layer constants so the
#: 1=ROSCO / 2=LEE fact lives in exactly one place (:mod:`neewer.protocol.frames`).
GEL_BRANDS: dict[int, str] = {
    frames.GEL_BRAND_ROSCO: "ROSCO",
    frames.GEL_BRAND_LEE: "LEE",
}


# --- the one blob ------------------------------------------------------------

def catalog() -> dict:
    """The full catalogue as one JSON-serialisable dict (static per version).

    Integer keys (scene/pixel effect ids, gel brand bytes) serialise to JSON
    object *string* keys — clients index with ``String(id)``.
    """
    return {
        "version": CATALOG_VERSION,
        "actions": actions(),
        "scenes": SCENES,
        "scene_id_sets": SCENE_ID_SETS,
        "pixel_effects": PIXEL_EFFECTS,
        "pixel_palette": PIXEL_PALETTE,
        "flow_modes": FLOW_MODES,
        "gel_brands": GEL_BRANDS,
        "running_status": RUNNING_STATUS,
    }
