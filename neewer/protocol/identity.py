"""Fixture identity: advertised name (+ optional MAC) -> integer light type.

Reproduces the official app's name -> type mapping in pure Python. Every Neewer
fixture is identified by an integer *light type*; per-fixture behaviour (opcode
routing, capabilities, DMX footprint) keys off that integer, and the model *name*
(e.g. "TL90C") is derived from it.

How identification works:

* A Neewer tube advertises as ``NW-<8-digit-serial>&<8-hex-network>`` -- for example
  ``NW-20240012&00000000``. The 8-digit serial is a **batch/model code** shared
  across many physical units (which is why every TL90C in a fleet shows the same
  ``NW-20240012``); it is *not* unique per light. The BLE MAC, by contrast, rotates
  on power-cycle -- so the robust key is the **name**, never the MAC. A MAC is
  accepted here too for signature parity, but the type is fully determined by
  the name.

* The serial maps to a model name via :data:`_SERIAL_TYPE` (~96 known serials).
  For the ``NW-<serial>`` names every real fixture advertises, the serial table
  settles the type directly -- so this decoder keys off the serial and matches the
  official app's result exactly (validated live: NW-20240012 -> TL90C (95),
  NW-20240047 -> TL120C-2 (101), NW-20240061 -> TL60 RGB-3 (115)).

* Fixtures that advertise a bare model name (``NWR<model>``, ``NEEWER-<model>``, or
  a raw name) are matched by name-substring against :data:`_NAME_TYPE` -- the small,
  high-value subset of known model names. Unknown names return
  :data:`UNKNOWN` (0), which callers treat as "not identified".

The advertised-name scheme and serial -> model table are documented in the
companion protocol reference: https://github.com/verygeeky/neewer-hardware
"""
from __future__ import annotations

#: Returned when the name cannot be resolved to a known type.
UNKNOWN = 0

# --- serial digits -> integer light type -----------------------------------
# The serial table: the 8-digit batch code carried in "NW-<serial>&<net>"
# maps to a light type. This is the path every NW-2024xxxx tube takes. The trailing
# comment is the model name for that type (from the type -> model table below).
_SERIAL_TYPE = {
    "20200037": 14,  # SL90
    "20200040": 215,  # AF200C
    "20200049": 18,  # RGB1200
    "20210006": 24,  # Apollo 150D
    "20210007": 21,  # RGB C80
    "20210012": 22,  # CB60 RGB
    "20210018": 26,  # BH-30S RGB
    "20210034": 30,  # MS60B
    "20210035": 25,  # MS60C
    "20210036": 32,  # TL60 RGB
    "20210037": 28,  # CB200B
    "20220014": 31,  # CB60B
    "20220016": 60,  # PL60C
    "20220024": 108,  # FL100C
    "20220035": 38,  # MS150B
    "20220041": 58,  # AS600B
    "20220043": 37,  # FS150B
    "20220051": 49,  # CB100C
    "20220055": 47,  # CB300B
    "20220057": 34,  # SL90 Pro
    "20230021": 42,  # BH-30S RGB-2
    "20230022": 66,  # HS60B
    "20230025": 43,  # RGB1200-2
    "20230029": 75,  # FS150C
    "20230031": 50,  # TL120C
    "20230036": 217,  # AP300C
    "20230041": 229,  # FS230C
    "20230042": 91,  # HS60C
    "20230043": 82,  # CB300C
    "20230044": 74,  # CB200C
    "20230050": 53,  # FS230 5600K
    "20230051": 55,  # FS230B
    "20230052": 54,  # FS150 5600K
    "20230054": 73,  # MS150C
    "20230059": 105,  # HS200C
    "20230064": 59,  # TL60 RGB-2
    "20230070": 232,  # AS1200B
    "20230080": 70,  # MS60C-2
    "20230091": 85,  # RGB2
    "20230092": 71,  # RGB1200-3
    "20230093": 90,  # BH20C
    "20230103": 78,  # MS60
    "20230104": 79,  # MS150
    "20230108": 106,  # HB80C
    "20230110": 80,  # CB200
    "20230111": 81,  # CB300
    "20230112": 84,  # CB120B
    "20240002": 214,  # AS600C
    "20240003": 209,  # FS600C
    "20240007": 83,  # AP150C-2
    "20240009": 107,  # FS300C
    "20240012": 95,  # TL90C
    "20240014": 88,  # RGB1200-4
    "20240015": 102,  # AP100C
    "20240033": 98,  # MS150C-2
    "20240037": 99,  # CB200-2
    "20240042": 219,  # AP600C
    "20240043": 116,  # PL60B
    "20240044": 117,  # AP100B
    "20240045": 118,  # AP150B
    "20240047": 101,  # TL120C-2
    "20240049": 206,  # HB80B
    "20240050": 200,  # HS200B
    "20240053": 103,  # CB200B PRO
    "20240061": 115,  # TL60 RGB-3
    "20240063": 112,  # BH20C-2
    "20240064": 110,  # AS600B-2
    "20240072": 119,  # PL60C-2
    "20240073": 202,  # SL90 Pro-2
    "20240074": 201,  # CB120B-2
    "20240075": 204,  # AP150C-3
    "20240076": 203,  # CB200B Pro-2
    "20240079": 207,  # CB200B Pro-3
    "20240080": 239,  # CT90C
    "20250003": 224,  # FS300B
    "20250010": 230,  # FS60B
    "20250015": 231,  # FS100B
    "20250017": 211,  # CB200C
    "20250020": 221,  # FS600B
    "20250024": 210,  # FS300C
    "20250031": 216,  # MS150C-3
    "20250036": 220,  # HS200C-2
    "20250038": 222,  # HS200B-2
    "20250048": 225,  # CB300C-2
    "20250049": 226,  # FS600C-2
    "20250053": 223,  # HB80C-2
    "20250066": 228,  # HB60B
    "20250067": 227,  # FS600B-2
    "20250086": 234,  # CB300C-3
    "20250104": 236,  # CB300C-4
    "20250107": 255,  # HB200C
    "20250111": 242,  # FS100C
    "20250124": 256,  # AS1200C
    "20250131": 257,  # HB60C
    "20250135": 241,  # FS60C
    "20250144": 238,  # FS300C-3
}

# --- integer light type -> model name --------------------------------------
# The inverse (display) table. Types 1/2/3/6/7/13 have string-dependent names in the
# app (SRP18C/RP18-P, NL140D, an RGB family, SL, SNL variants); they are given their
# common default here and never arise from the serial path.
_TYPE_MODEL = {
    4: "GL1",
    5: "RGB176",
    8: "RGB1",
    9: "RGB18",
    10: "ZRP",
    11: "RGB190",
    12: "RGB960",
    14: "SL90",
    15: "RGB140",
    16: "RGB168",
    17: "ZK-RY",
    18: "RGB1200",
    19: "CL124-RGB",
    20: "RGB176 A1",
    21: "RGB C80",
    22: "CB60 RGB",
    23: "ER1",
    24: "Apollo 150D",
    25: "MS60C",
    26: "BH-30S RGB",
    27: "X2",
    28: "CB200B",
    29: "RGB-P280",
    30: "MS60B",
    31: "CB60B",
    32: "TL60 RGB",
    33: "GL1 PRO",
    34: "SL90 Pro",
    35: "DL200",
    36: "GM16",
    37: "FS150B",
    38: "MS150B",
    39: "GL1C",
    40: "RGB62",
    41: "DL300",
    42: "BH-30S RGB-2",
    43: "RGB1200-2",
    44: "T100C",
    45: "A19C 220V",
    46: "A19C(E26)",
    47: "CB300B",
    48: "R360",
    49: "CB100C",
    50: "TL120C",
    51: "RP18B PRO",
    52: "RL45B",
    53: "FS230 5600K",
    54: "FS150 5600K",
    55: "FS230B",
    56: "CL124 RGB(II)",
    57: "RGB18(II)",
    58: "AS600B",
    59: "TL60 RGB-2",
    60: "PL60C",
    61: "BH40C",
    62: "GR18C",
    63: "RP19C",
    64: "TL97C",
    65: "VL67C",
    66: "HS60B",
    67: "TL40",
    68: "Q200",
    69: "TL21C",
    70: "MS60C-2",
    71: "RGB1200-3",
    72: "RP18B PRO-2",
    73: "MS150C",
    74: "CB200C",
    75: "FS150C",
    76: "SRP18C",
    77: "RL45B-2",
    78: "MS60",
    79: "MS150",
    80: "CB200",
    81: "CB300",
    82: "CB300C",
    83: "AP150C-2",
    84: "CB120B",
    85: "RGB2",
    86: "TL40-2",
    87: "T100C-2",
    88: "RGB1200-4",
    89: "A19C(E26)-2",
    90: "BH20C",
    91: "HS60C",
    92: "RP19C-2",
    93: "DL400",
    94: "RL45C",
    95: "TL90C",
    96: "TL98C",
    97: "GL25C",
    98: "MS150C-2",
    99: "CB200-2",
    100: "VL67B",
    101: "TL120C-2",
    102: "AP100C",
    103: "CB200B PRO",
    104: "RB12B",
    105: "HS200C",
    106: "HB80C",
    107: "FS300C",
    108: "FL100C",
    109: "RGB18(II)-2",
    110: "AS600B-2",
    111: "GL25C-2",
    112: "BH20C-2",
    113: "PS099S",
    114: "Q6",
    115: "TL60 RGB-3",
    116: "PL60B",
    117: "AP100B",
    118: "AP150B",
    119: "PL60C-2",
    200: "HS200B",
    201: "CB120B-2",
    202: "SL90 Pro-2",
    203: "CB200B Pro-2",
    204: "AP150C-3",
    205: "RH100B",
    206: "HB80B",
    207: "CB200B Pro-3",
    208: "HS60C PRO",
    209: "FS600C",
    210: "FS300C",
    211: "CB200C",
    212: "PS050S",
    213: "PS150S",
    214: "AS600C",
    215: "AF200C",
    216: "MS150C-3",
    217: "AP300C",
    218: "TL97C-2",
    219: "AP600C",
    220: "HS200C-2",
    221: "FS600B",
    222: "HS200B-2",
    223: "HB80C-2",
    224: "FS300B",
    225: "CB300C-2",
    226: "FS600C-2",
    227: "FS600B-2",
    228: "HB60B",
    229: "FS230C",
    230: "FS60B",
    231: "FS100B",
    232: "AS1200B",
    233: "LP40S",
    234: "CB300C-3",
    235: "Q120",
    236: "CB300C-4",
    237: "PS099U",
    238: "FS300C-3",
    239: "CT90C",
    240: "Q4Pro",
    241: "FS60C",
    242: "FS100C",
    243: "FL12C",
    244: "BP300S",
    245: "NL480S",
    246: "NL660S",
    247: "NL192SAI",
    248: "NL116SAI",
    249: "BP66S",
    250: "R06S",
    251: "GC22B",
    252: "GC31B",
    253: "GC22C",
    254: "GC31C",
    255: "HB200C",
    256: "AS1200C",
    257: "HB60C",
}
_TYPE_MODEL.update({1: "SRP18C", 2: "NL140D", 3: "RGB480", 6: "SL", 7: "SNL", 13: "SNL"})

# --- bare-model-name substrings -> integer light type ----------------------
# For names that already contain a model string (NWR-/NEEWER-/raw). Longest-substring
# wins so "TL120C-2" beats "TL120C". Derived from the model names in the type table;
# only the tube-family entries we actually care about are listed -- extend as needed.
_NAME_TYPE: dict[str, int] = {
    "TL120C-2": 101,
    "TL120C": 50,
    "TL90C": 95,
    "TL60 RGB-3": 115,
    "TL60 RGB-2": 59,
    "TL60 RGB": 32,
    "TL98C": 96,
    "TL97C-2": 218,
    "TL97C": 64,
    "TL40-2": 86,
    "TL40": 67,
    "TL21C": 69,
}


def _serial_from_name(name: str) -> str | None:
    """Extract the 8-digit serial from an ``NW-<serial>&<net>`` advertised name.

    Everything between the ``NW-`` prefix and the last ``&`` must be all digits,
    else there is no serial to look up — matching how the official app treats
    these names.
    """
    if not name.startswith("NW") or "&" not in name:
        return None
    serial = name[3 : name.rindex("&")]
    return serial if serial.isdigit() else None


def get_light_type(name: str | None, mac: str | None = None) -> int:
    """Map an advertised BLE ``name`` (+ optional ``mac``) to the integer light type.

    Returns :data:`UNKNOWN` (0) if the name resolves to no known fixture. ``mac`` is
    accepted for signature parity but is not needed to determine the type — the
    type derives from the name alone.
    """
    if not name:
        return UNKNOWN

    # 1) NW-<serial>&<net>: the serial batch code settles the type directly.
    serial = _serial_from_name(name)
    if serial is not None:
        t = _SERIAL_TYPE.get(serial)
        if t is not None:
            return t
        # Known-shape name but an unlisted serial: nothing else in the name to match.
        return UNKNOWN

    # 2) A name that carries a model string (NWR-/NEEWER-/raw). Longest match wins so
    #    a generation suffix ("-2") is preferred over the base model.
    for needle in sorted(_NAME_TYPE, key=len, reverse=True):
        if needle in name:
            return _NAME_TYPE[needle]
    return UNKNOWN


def model_for_type(light_type: int) -> str | None:
    """Return the display model name for an integer light type, or ``None`` if unknown."""
    return _TYPE_MODEL.get(light_type)


def model_for_name(name: str | None, mac: str | None = None) -> str | None:
    """Resolve an advertised name straight to a model name (``None`` if unidentified)."""
    return model_for_type(get_light_type(name, mac))
