"""Command-frame builders for Neewer TL90C / Infinity lights.

This module is the single source of truth for turning a high-level intent
("set HSI to hue 240") into the exact bytes the light expects on its BLE write
characteristic. Nothing else in the codebase should hand-assemble a frame.

Wire format
-----------
Every command is a single ATT write with this layout::

    [ 0x78 , tag , len , payload[0] , payload[1] , ... , checksum ]
      ^^^^   ^^^   ^^^   ^^^^^^^^^^^^^^^^^^^^^^^^^^^^   ^^^^^^^^
      prefix opc.  N     N payload bytes                sum & 0xFF

* ``0x78`` is a constant prefix on every frame.
* ``tag`` is the opcode (see the ``OP_*`` constants below).
* ``len`` is the number of payload bytes that follow (NOT counting the checksum).
* ``checksum`` is the low byte of the arithmetic sum of every preceding byte,
  prefix and opcode included.

Provenance
----------
Every frame produced here was confirmed against real hardware. The full opcode
reference lives in the companion protocol documentation:
https://github.com/verygeeky/neewer-hardware
"""
from __future__ import annotations

# --- Frame structure ------------------------------------------------------

#: Constant first byte of every command frame.
PREFIX = 0x78

# --- Opcodes (the ``tag`` byte) -------------------------------------------
# Named so call sites read as intent, not magic numbers. The hex values are the
# protocol's; do not change them.

OP_POWER = 0x81         #: power on/off
OP_VERSION = 0x80       #: firmware-version query (direct) → reply 0x00
OP_LIGHT_STATE = 0x85   #: light-state query (direct) → reply 0x02
OP_HSI = 0x86           #: hue / saturation / intensity colour
OP_CCT = 0x87           #: correlated colour temperature (white) + brightness + G/M
OP_SCENE = 0x88         #: built-in scene / FX effect (direct)
OP_SCENE_MAC = 0x91     #: built-in scene / FX effect (MAC-addressed, inner 0x8b) — TL120C
OP_PIXEL = 0xB0         #: per-segment pixel palette effect (TL120C), MAC-addressed
OP_RGBCW_MAC = 0xA9     #: RGB + dedicated Cold/Warm white (MAC-addressed, inner 0xa8) — TL120C
OP_XY_MAC = 0xB7        #: CIE-1931 xy colour point (MAC-addressed) — TL120C
OP_GEL_MAC = 0xAD       #: gel / colour-paper as HSI + brand metadata (MAC-addressed) — TL120C
OP_STATE_MAC = 0x8E     #: state/power read (MAC-addressed) → reply 0x04
OP_IDENTIFY = 0x99      #: find-light / identify — flashes the light (MAC-addressed)
OP_BATTERY = 0x95       #: battery-level query (MAC-addressed) → reply 0x05
OP_VERSION_MAC = 0x9E   #: firmware-version query (MAC-addressed) → reply 0x08
OP_TEMP = 0xB3          #: temperature + fan-mode query (MAC-addressed) → reply 0x12
OP_STREAMER_SUPPORT = 0xC4  #: streamer-support query (MAC-addressed) → reply 0x17 (TL60)

# --- by-channel (group / mesh) opcodes ------------------------------------
# Each is the <NET4><CH>-addressed twin of a by-MAC colour op: instead of a
# single fixture's 6-byte MAC, they carry a 4-byte network id + 1 channel byte,
# so one frame drives every tube provisioned onto that channel. See the
# provisioning/grouping ops (0x9F/0x8C/0xD4) for how a tube gets onto a channel.

OP_RGBCW_CH = 0xAA      #: RGB + Cold/Warm white, by-channel (inner 0xa8) — twin of 0xA9
OP_HSI_CH = 0x92        #: streamed HSI colour, by-channel (inner 0x86) — twin of 0x8F DIY-HSI
OP_GEL_CH = 0xAE        #: gel / colour-paper, by-channel — twin of 0xAD
OP_XY_CH = 0xB8         #: CIE-1931 xy colour point, by-channel — twin of 0xB7
OP_PIXEL_CH = 0xB1      #: pixel palette effect, by-channel — twin of 0xB0
OP_POWER_CH = 0x98      #: power on/off, by-channel (inner 0x81)

# --- provisioning / grouping opcodes --------------------------------------
OP_PROVISION = 0x9F     #: provision a tube: assign it a channel + network id (MAC-addressed)
OP_ASSIGN_CHANNEL = 0x8C  #: put a tube on a channel/group (MAC-addressed)
OP_GROUP_SELECT = 0xD4  #: select a group before streaming colour to it (by-channel)

# --- Payload literals -----------------------------------------------------

POWER_ON = 0x01
POWER_OFF = 0x02

# Correlated colour temperature is sent in hundreds of Kelvin: 32 == 3200 K,
# 56 == 5600 K, 85 == 8500 K. These bound the values the hardware accepts.
CCT_MIN = 32
CCT_MAX = 85

# Green/Magenta tint axis: 0..100, with 50 == neutral (no tint).
GM_NEUTRAL = 50

# Dimming-curve type byte appended to a CCT frame (0 == standard curve).
# The light does not act on it, but the official app always sends it, so we
# include it for byte-for-byte frame parity.
DIM_CURVE_DEFAULT = 0


def clamp(value: int, lo: int, hi: int) -> int:
    """Clamp ``value`` into ``[lo, hi]`` and coerce to ``int``.

    Used so a caller passing a slightly out-of-range brightness/saturation gets
    a valid frame instead of one the light silently rejects or mis-renders.
    """
    value = int(value)
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def checksum(payload: list[int]) -> bytes:
    """Append the protocol checksum to ``payload`` and return the full frame.

    The checksum is ``sum(payload) & 0xFF``. ``payload`` must already contain the
    prefix, opcode, length and data bytes — everything except the checksum.
    """
    body = list(payload)
    return bytes(body + [sum(body) & 0xFF])


def power(on: bool) -> bytes:
    """Frame to switch a light on or off."""
    return checksum([PREFIX, OP_POWER, 0x01, POWER_ON if on else POWER_OFF])


def hsi(hue: int, sat: int = 100, bri: int = 100) -> bytes:
    """Frame to set an HSI colour.

    Args:
        hue: 0..359 degrees (wrapped modulo 360). Sent little-endian as two bytes.
        sat: saturation 0..100 (clamped).
        bri: intensity / brightness 0..100 (clamped).
    """
    hue %= 360
    return checksum([
        PREFIX, OP_HSI, 0x04,
        hue & 0xFF, (hue >> 8) & 0xFF,
        clamp(sat, 0, 100), clamp(bri, 0, 100),
    ])


def cct(bri: int, temp: int, gm: int = GM_NEUTRAL,
        dim_curve: int = DIM_CURVE_DEFAULT) -> bytes:
    """Frame to set white (colour-temperature) output.

    Matches the official app's GM-capable CCT frame exactly: a **4-byte**
    payload (brightness, CCT, GM, dimming curve), so the length byte is ``0x04``.

    (An earlier version emitted ``len=0x02`` with three payload bytes. The TL120C
    handler reads the payload by absolute offset and tolerated the mismatch, but
    the length byte must equal the payload count — the two-chip UART framer and
    other fixtures reassemble a frame by that header, not by ATT write boundaries.)

    Args:
        bri: brightness 0..100 (clamped).
        temp: colour temperature in hundreds of Kelvin, clamped to the hardware
            range ``CCT_MIN..CCT_MAX`` (3200 K..8500 K).
        gm: green/magenta tint 0..100, 50 neutral (clamped).
        dim_curve: dimming-curve type byte (0 == standard). Sent for app parity;
            the CCT handler ignores it.
    """
    return checksum([
        PREFIX, OP_CCT, 0x04,
        clamp(bri, 0, 100), clamp(temp, CCT_MIN, CCT_MAX),
        clamp(gm, 0, 100), int(dim_curve) & 0xFF,
    ])


def scene(effect: int, *params: int) -> bytes:
    """Frame to trigger a built-in scene/FX effect.

    The length byte is ``len(params) + 1`` to account for the effect id itself.
    Parameter meaning is effect-specific; see the scene catalogue in
    :mod:`neewer.catalog`.

    Fixture note: the **TL120C ignores `0x88`** entirely, so this is a no-op on a
    TL120C. It is kept for other fixtures (e.g. TL90C). Real TL120C effects use the by-MAC
    effect opcodes `0x8F`/`0x90`/`0x91`, which this module does not yet build.
    """
    return checksum([PREFIX, OP_SCENE, len(params) + 1, int(effect), *map(int, params)])


#: Inner sub-opcode carried by the MAC-addressed scene frame (0x91). The captured
#: app frame is `78 91 0b <MAC6> 8b <effect> <params…> ck`, so 0x8b tags "this is a
#: scene effect" inside the by-MAC envelope (parallel to the direct 0x88).
SCENE_MAC_SUBOP = 0x8B


def scene_by_mac(mac6: bytes, effect: int, *params: int) -> bytes:
    """Frame to trigger a built-in scene/FX effect on a MAC-addressed fixture (`0x91`).

    This is how the **TL120C** runs its built-in scenes: its LED-MCU firmware has no
    direct `0x88` handler (that frame no-ops), but it *does* handle `0x91` — the same
    frame the app sends.

    Wire layout::

        78 91 <len> <MAC6> 8b <effect> <params…> ck        len = 6 + 2 + nparams

    e.g. effect 1 (Lightning) `78 91 0b <MAC6> 8b 01 <bri> <cct> <rate> ck` — the
    ``<effect> <params…>`` tail is byte-identical to the direct `0x88` catalog, so a
    caller passes the *same* effect id + params it would give :func:`scene`.

    Args:
        mac6: the 6-byte target MAC (see :func:`mac_bytes`).
        effect: scene effect id (1..N; same catalog as :func:`scene`).
        params: effect-specific parameters, passed through verbatim.
    """
    if len(mac6) != 6:
        raise ValueError(f"scene_by_mac needs a 6-byte MAC, got {len(mac6)} bytes")
    inner = [SCENE_MAC_SUBOP, int(effect), *map(int, params)]
    return checksum([PREFIX, OP_SCENE_MAC, len(mac6) + len(inner), *mac6, *inner])


# --- by-MAC colour modes (TL120C) ----------------------------------------
# These three set colour on the TL120C by its own MAC. On that fixture the
# *direct* forms (0xA8 RGBCW / 0xB9 xy / 0xAF gel) are silently dropped — only
# the MAC-addressed opcodes below take effect. Verified against real hardware.

#: Inner sub-opcode carried by the MAC-addressed RGBCW frame (0xA9). The captured
#: app frame is `78 a9 0e <MAC6> a8 <bri> <RGBCW> <decBri> ck`, so 0xa8 tags "this
#: is an RGBCW colour" inside the by-MAC envelope (parallel to SCENE_MAC_SUBOP).
RGBCW_MAC_SUBOP = 0xA8

#: CIE coordinates travel as fixed-point integers: the app multiplies the 0..1
#: float by this and rounds, so 0.3127 -> 3127, sent little-endian 16-bit.
XY_SCALE = 10000

#: Gel brand codes carried in the gel frame's `brand` byte.
GEL_BRAND_ROSCO = 1
GEL_BRAND_LEE = 2


def rgbcw_by_mac(mac6: bytes, bri: int, r: int = 0, g: int = 0, b: int = 0,
                 c: int = 0, w: int = 0, dec_bri: int = 0) -> bytes:
    """Frame to set RGB **plus dedicated Cold/Warm white** on a TL120C (`0xA9`).

    This is the TL120C's richest colour mode: unlike HSI it drives the physical
    Cold- and Warm-white emitters directly, so it can render a high-CRI white that
    an HSI hue simply cannot mix. It is **by-MAC only** — the direct `0xA8` form is
    dropped by this fixture. Verified on real hardware, byte-for-byte with the
    official app.

    Wire layout::

        78 a9 0e <MAC6> a8 <bri> <R> <G> <B> <C> <W> <decBri> ck

    Args:
        mac6: the 6-byte target MAC (see :func:`mac_bytes`).
        bri: master brightness 0..100 (clamped).
        r, g, b, c, w: red/green/blue/cold-white/warm-white channels, each 0..255
            (clamped). Default 0.
        dec_bri: the app's secondary "decimal brightness" byte; 0 in every capture.
    """
    if len(mac6) != 6:
        raise ValueError(f"rgbcw_by_mac needs a 6-byte MAC, got {len(mac6)} bytes")
    # 0xa8 is the inner colour tag; the five channels are 8-bit, brightness 0..100.
    inner = [
        RGBCW_MAC_SUBOP,
        clamp(bri, 0, 100),
        clamp(r, 0, 255), clamp(g, 0, 255), clamp(b, 0, 255),
        clamp(c, 0, 255), clamp(w, 0, 255),
        clamp(dec_bri, 0, 255),
    ]
    return checksum([PREFIX, OP_RGBCW_MAC, len(mac6) + len(inner), *mac6, *inner])


def xy_by_mac(mac6: bytes, bri: int, x: float, y: float) -> bytes:
    """Frame to set a CIE-1931 xy colour point on a TL120C (`0xB7`).

    Lets a caller specify colour as a chromaticity coordinate (e.g. the D65 white
    point 0.3127, 0.3290) rather than a hue — handy for colour-science-driven
    control. **By-MAC only** (the direct `0xB9` form is dropped by this fixture).
    Verified on real hardware, byte-for-byte with the official app.

    Wire layout::

        78 b7 0c <MAC6> <bri> <xLo xHi> <yLo yHi> 00 ck

    Args:
        mac6: the 6-byte target MAC (see :func:`mac_bytes`).
        bri: brightness 0..100 (clamped).
        x, y: CIE-1931 coordinates as floats in 0.0..1.0. Each is encoded as
            ``round(coord * XY_SCALE)`` little-endian 16-bit — the app's fixed-point
            form (so 0.3127 -> 3127 -> ``37 0c``).
    """
    if len(mac6) != 6:
        raise ValueError(f"xy_by_mac needs a 6-byte MAC, got {len(mac6)} bytes")
    xi = round(x * XY_SCALE)     # fixed-point: 0.0..1.0 float -> 0..10000 integer
    yi = round(y * XY_SCALE)
    payload = [
        clamp(bri, 0, 100),
        xi & 0xFF, (xi >> 8) & 0xFF,        # little-endian 16-bit x
        yi & 0xFF, (yi >> 8) & 0xFF,        # little-endian 16-bit y
        0x00,                                # trailing i3 byte, always 0 in captures
    ]
    return checksum([PREFIX, OP_XY_MAC, len(mac6) + len(payload), *mac6, *payload])


def gel_by_mac(mac6: bytes, hue: int, sat: int, bri: int,
               brand: int = GEL_BRAND_ROSCO, gel_no: int = 0,
               dec_bri: int = 0) -> bytes:
    """Frame to set a lighting-gel colour on a TL120C (`0xAD`).

    A "gel" (colour-paper) is just an HSI colour tagged with its catalogue brand +
    number metadata. The brand/number -> HSI *catalogue* is a Neewer server JSON we
    do not have, so this builder takes the resolved hue/sat/bri explicitly and just
    carries the brand/number through as metadata. **By-MAC only** (the direct `0xAF`
    form is dropped by this fixture). Rendered live on the TL120C — there is no app
    capture for gel, so the frame is our own, cross-checked against the sibling
    by-MAC frames.

    Wire layout::

        78 ad 0d <MAC6> <hueLo hueHi> <sat> <bri> <decBri> <brand> <gelNo> ck

    Args:
        mac6: the 6-byte target MAC (see :func:`mac_bytes`).
        hue: 0..359 degrees (wrapped modulo 360), little-endian 16-bit.
        sat: saturation 0..100 (clamped).
        bri: brightness 0..100 (clamped).
        brand: gel brand — 1 = ROSCO, 2 = LEE (:data:`GEL_BRAND_ROSCO` /
            :data:`GEL_BRAND_LEE`).
        gel_no: the gel's catalogue number, carried as metadata.
        dec_bri: the app's secondary "decimal brightness" byte; default 0.
    """
    if len(mac6) != 6:
        raise ValueError(f"gel_by_mac needs a 6-byte MAC, got {len(mac6)} bytes")
    hue %= 360
    payload = [
        hue & 0xFF, (hue >> 8) & 0xFF,      # little-endian 16-bit hue
        clamp(sat, 0, 100), clamp(bri, 0, 100),
        clamp(dec_bri, 0, 255),
        int(brand) & 0xFF, int(gel_no) & 0xFF,
    ]
    return checksum([PREFIX, OP_GEL_MAC, len(mac6) + len(payload), *mac6, *payload])


# --- by-channel (group / mesh) colour twins -------------------------------
# Each mirrors a by-MAC colour op but addresses a whole channel: the 6-byte MAC
# is replaced by a 4-byte network id + 1 channel byte, so one write drives every
# tube provisioned onto that channel. Field layouts (bar the header swap) are
# byte-identical to their by-MAC siblings. Provenance per builder below.


def rgbcw_by_channel(net4, ch: int, bri: int, r: int = 0, g: int = 0, b: int = 0,
                     c: int = 0, w: int = 0, dec_bri: int = 0) -> bytes:
    """Frame to set RGB **plus Cold/Warm white** on a whole channel (`0xAA`).

    The by-channel twin of :func:`rgbcw_by_mac`: identical inner `0xa8` colour
    block, but addressed to a network+channel instead of one MAC. Verified live
    on hardware.

    Wire layout::

        78 aa 0d <NET4> <CH> a8 <bri> <R> <G> <B> <C> <W> <decBri> ck

    Args:
        net4: the 4-byte network id (see :func:`net_bytes`).
        ch: channel byte 0..255.
        bri: master brightness 0..100 (clamped).
        r, g, b, c, w: red/green/blue/cold-white/warm-white channels, each 0..255
            (clamped). Default 0.
        dec_bri: the app's secondary "decimal brightness" byte; 0 in every capture.
    """
    net = net_bytes(net4)
    # 0xa8 is the inner colour tag (same as the by-MAC form); channels are 8-bit,
    # brightness 0..100.
    payload = [
        *net, clamp(ch, 0, 255),
        RGBCW_MAC_SUBOP,
        clamp(bri, 0, 100),
        clamp(r, 0, 255), clamp(g, 0, 255), clamp(b, 0, 255),
        clamp(c, 0, 255), clamp(w, 0, 255),
        clamp(dec_bri, 0, 255),
    ]
    return checksum([PREFIX, OP_RGBCW_CH, len(payload), *payload])


def hsi_by_channel(net4, ch: int, hue: int, sat: int = 100, lvl: int = 100) -> bytes:
    """Frame to stream an HSI colour to a whole channel (`0x92`, inner `0x86`).

    The by-channel twin of the `0x8F` DIY-HSI-by-MAC op — the streamed
    group-colour / music transport, addressed to a network+channel. Verified
    live on hardware.

    Wire layout::

        78 92 0a <NET4> <CH> 86 <hueLo hueHi> <sat> <lvl> ck

    Args:
        net4: the 4-byte network id (see :func:`net_bytes`).
        ch: channel byte 0..255.
        hue: 0..359 degrees (wrapped modulo 360), little-endian 16-bit.
        sat: saturation 0..100 (clamped).
        lvl: level / intensity 0..100 (clamped).
    """
    net = net_bytes(net4)
    hue %= 360
    payload = [
        *net, clamp(ch, 0, 255),
        OP_HSI,                              # inner 0x86 tags "HSI colour" in the envelope
        hue & 0xFF, (hue >> 8) & 0xFF,      # little-endian 16-bit hue
        clamp(sat, 0, 100), clamp(lvl, 0, 100),
    ]
    return checksum([PREFIX, OP_HSI_CH, len(payload), *payload])


def gel_by_channel(net4, ch: int, hue: int, sat: int, bri: int,
                   brand: int = GEL_BRAND_ROSCO, gel_no: int = 0,
                   dec_bri: int = 0) -> bytes:
    """Frame to set a lighting-gel colour on a whole channel (`0xAE`).

    The by-channel twin of :func:`gel_by_mac`: the payload byte order after the
    header is identical, so there is **no** inner sub-op. Verified live on
    hardware.

    Wire layout::

        78 ae 0c <NET4> <CH> <hueLo hueHi> <sat> <bri> <decBri> <brand> <gelNo> ck

    Args:
        net4: the 4-byte network id (see :func:`net_bytes`).
        ch: channel byte 0..255.
        hue: 0..359 degrees (wrapped modulo 360), little-endian 16-bit.
        sat: saturation 0..100 (clamped).
        bri: brightness 0..100 (clamped).
        brand: gel brand — 1 = ROSCO, 2 = LEE (:data:`GEL_BRAND_ROSCO` /
            :data:`GEL_BRAND_LEE`).
        gel_no: the gel's catalogue number, carried as metadata.
        dec_bri: the app's secondary "decimal brightness" byte; default 0.
    """
    net = net_bytes(net4)
    hue %= 360
    payload = [
        *net, clamp(ch, 0, 255),
        hue & 0xFF, (hue >> 8) & 0xFF,      # little-endian 16-bit hue
        clamp(sat, 0, 100), clamp(bri, 0, 100),
        clamp(dec_bri, 0, 255),
        int(brand) & 0xFF, int(gel_no) & 0xFF,
    ]
    return checksum([PREFIX, OP_GEL_CH, len(payload), *payload])


def xy_by_channel(net4, ch: int, bri: int, x: float, y: float) -> bytes:
    """Frame to set a CIE-1931 xy colour point on a whole channel (`0xB8`).

    The by-channel twin of :func:`xy_by_mac`. Verified live on hardware.

    Wire layout::

        78 b8 0b <NET4> <CH> <bri> <xLo xHi> <yLo yHi> 00 ck

    Args:
        net4: the 4-byte network id (see :func:`net_bytes`).
        ch: channel byte 0..255.
        bri: brightness 0..100 (clamped).
        x, y: CIE-1931 coordinates as floats in 0.0..1.0, each encoded as
            ``round(coord * XY_SCALE)`` little-endian 16-bit (the app's fixed-point
            form).
    """
    net = net_bytes(net4)
    xi = round(x * XY_SCALE)     # fixed-point: 0.0..1.0 float -> 0..10000 integer
    yi = round(y * XY_SCALE)
    payload = [
        *net, clamp(ch, 0, 255),
        clamp(bri, 0, 100),
        xi & 0xFF, (xi >> 8) & 0xFF,        # little-endian 16-bit x
        yi & 0xFF, (yi >> 8) & 0xFF,        # little-endian 16-bit y
        0x00,                                # trailing byte, always 0 in captures
    ]
    return checksum([PREFIX, OP_XY_CH, len(payload), *payload])


def pixel_palette_by_channel(net4, ch: int, tokens, effect: int = 1) -> bytes:
    """Pixel-effect colour-palette frame on a whole channel (`0xB1`).

    Wire layout::

        78 b1 <len> <NET4> <CH> <effect> 0x01 <N x 3-byte pixel_block> ck
            len = 5 (NET4+CH) + 2 (effect + 0x01 sub-index) + len(palette bytes)

    CAUTION — read this before trusting the field offsets: `0xB1` is confirmed
    as the by-channel pixel op, but this exact palette layout is derived
    **by analogy** to :func:`pixel_palette` (the by-MAC `0xB0` frame) with the
    6-byte MAC header swapped for the 5-byte `NET4+CH` header. The by-channel
    field offsets have **not** been independently verified. Only the palette form
    is provided — the by-channel pixel *params* layout is unknown, so none is
    built here.

    Args:
        net4: the 4-byte network id (see :func:`net_bytes`).
        ch: channel byte 0..255.
        tokens: palette tokens, each encoded via :func:`pixel_block`.
        effect: pixel effect id (default 1).
    """
    net = net_bytes(net4)
    palette: list[int] = []
    for token in tokens:
        palette += pixel_block(token)
    # len = 5 (NET4+CH) + 2 (effect + 0x01) + palette bytes.
    return checksum([PREFIX, OP_PIXEL_CH, 5 + 2 + len(palette)]
                    + list(net) + [clamp(ch, 0, 255), effect & 0xFF, 0x01] + palette)


def power_by_channel(net4, ch: int, on: bool) -> bytes:
    """Frame to switch a whole channel on or off (`0x98`, inner `0x81`).

    The by-channel twin of :func:`power`. **Not yet hardware-verified.**

    Wire layout::

        78 98 07 <NET4> <CH> 81 <01|02> ck

    Args:
        net4: the 4-byte network id (see :func:`net_bytes`).
        ch: channel byte 0..255.
        on: ``True`` for on (inner `0x81` + POWER_ON), ``False`` for off.
    """
    net = net_bytes(net4)
    payload = [
        *net, clamp(ch, 0, 255),
        OP_POWER,                            # inner 0x81 tags "power" in the envelope
        POWER_ON if on else POWER_OFF,
    ]
    return checksum([PREFIX, OP_POWER_CH, len(payload), *payload])


# --- provisioning / grouping ---------------------------------------------
# These put a tube onto a network + channel so the by-channel ops above reach it.
# All three verified live on hardware; 0xD4 is dual-use — see below.


def provision(mac6: bytes, ch: int, net4) -> bytes:
    """Frame to provision a tube onto a channel + network id (`0x9F`).

    Sent to a specific tube by its MAC to enroll it: assigns the channel it will
    answer to and the network id it belongs to. The tube ACKs with a `0x7F` reply
    (see :mod:`neewer.protocol.replies`). Verified live on hardware.

    Wire layout::

        78 9f 0c <MAC6> 01 <CH> <NET4> ck

    Args:
        mac6: the 6-byte target MAC (see :func:`mac_bytes`).
        ch: channel byte 0..255.
        net4: the 4-byte network id (see :func:`net_bytes`).
    """
    if len(mac6) != 6:
        raise ValueError(f"provision needs a 6-byte MAC, got {len(mac6)} bytes")
    net = net_bytes(net4)
    # The 0x01 is a constant flag observed in every provisioning capture.
    payload = [*mac6, 0x01, clamp(ch, 0, 255), *net]
    return checksum([PREFIX, OP_PROVISION, len(payload), *payload])


def assign_channel(mac6: bytes, ch: int, net4) -> bytes:
    """Frame to put a tube on a channel / group (`0x8C`).

    Sent to a tube by its MAC to move it onto a channel within a network. The tube
    ACKs with a `0x7F` reply. Verified live on hardware.

    Wire layout::

        78 8c 0b <MAC6> <CH> <NET4> ck

    Args:
        mac6: the 6-byte target MAC (see :func:`mac_bytes`).
        ch: channel byte 0..255.
        net4: the 4-byte network id (see :func:`net_bytes`).
    """
    if len(mac6) != 6:
        raise ValueError(f"assign_channel needs a 6-byte MAC, got {len(mac6)} bytes")
    net = net_bytes(net4)
    payload = [*mac6, clamp(ch, 0, 255), *net]
    return checksum([PREFIX, OP_ASSIGN_CHANNEL, len(payload), *payload])


def group_select(net4, ch: int) -> bytes:
    """Frame to select a group/channel before streaming colour to it (`0xD4`).

    Sent once per group after the tubes are assigned and before by-channel colour
    frames. Verified live on hardware.

    NOTE: `0xD4` is elsewhere associated with "music-gradient by channel", but in
    the grouping flow it acts as a plain group-select — the opcode is dual-use.

    Wire layout::

        78 d4 06 <NET4> <CH> 00 ck

    Args:
        net4: the 4-byte network id (see :func:`net_bytes`).
        ch: channel byte 0..255.
    """
    net = net_bytes(net4)
    payload = [*net, clamp(ch, 0, 255), 0x00]    # trailing 0x00 constant in the capture
    return checksum([PREFIX, OP_GROUP_SELECT, len(payload), *payload])


def identify(mac6: bytes) -> bytes:
    """Frame to make a light *identify* itself by flashing — the app's "find light".

    ``78 99 06 <MAC6> ck``. MAC-addressed (`0x99`). Useful for physically locating one
    tube in a rig, and as the ordering primitive for a "walk the fixtures" setup flow.

    Args:
        mac6: the 6-byte target MAC (see :func:`mac_bytes`).
    """
    if len(mac6) != 6:
        raise ValueError(f"identify needs a 6-byte MAC, got {len(mac6)} bytes")
    return checksum([PREFIX, OP_IDENTIFY, 0x06, *mac6])


def battery_query(mac6: bytes) -> bytes:
    """Frame to query battery level. ``mac6`` is the 6-byte MAC (see ``mac_bytes``)."""
    if len(mac6) != 6:
        raise ValueError(f"battery_query needs a 6-byte MAC, got {len(mac6)} bytes")
    return checksum([PREFIX, OP_BATTERY, 0x06, *mac6])


def temp_query(mac6: bytes) -> bytes:
    """Frame to query temperature + fan-mode by MAC (`0xB3`). Reply: `0x12`.

    This is the elicitor for the `0x12` temperature reply that
    :func:`neewer.protocol.replies.parse` already decodes — the pair was previously
    half-wired (the decoder existed with nothing to prompt it). Layout mirrors the
    other by-MAC read frames: ``78 b3 06 <MAC6> ck``.

    Fixture note: the **TL120C does not answer `0xB3`** (confirmed live); other
    fixtures do reply with `0x12`. So this is a read for those fixtures, not the
    TL120C.
    """
    if len(mac6) != 6:
        raise ValueError(f"temp_query needs a 6-byte MAC, got {len(mac6)} bytes")
    return checksum([PREFIX, OP_TEMP, 0x06, *mac6])


def streamer_support_query(mac6: bytes) -> bytes:
    """Ask whether a fixture supports the realtime streamer (`0xC4`). Reply: `0x17`.

    The **TL60** answers `78 17 07 <MAC6> <01=supported>` (confirmed live); the
    TL120C doesn't reply at all (the streamer is TL60-only). Layout mirrors the
    other by-MAC reads: ``78 c4 06 <MAC6>``.
    """
    if len(mac6) != 6:
        raise ValueError(f"streamer_support_query needs a 6-byte MAC, got {len(mac6)} bytes")
    return checksum([PREFIX, OP_STREAMER_SUPPORT, 0x06, *mac6])


# --- Query / read frames --------------------------------------------------
# These ask the light for state; the answer arrives asynchronously on the notify
# characteristic and is decoded by :mod:`neewer.protocol.replies`.

def version_query() -> bytes:
    """Direct firmware-version query (`0x80`). Reply: `0x00` → `78 80 00 f8`.

    Fixture note: the **TL120C never replies to `0x80`** (confirmed live). Use
    :func:`version_query_mac` (`0x9E`) for the TL120C; this direct form is for
    other fixtures.
    """
    return checksum([PREFIX, OP_VERSION, 0x00])


def light_state_query() -> bytes:
    """Direct light-state query (`0x85`). Reply: `0x02` power status.

    Fixture note: the **TL120C firmware does not handle `0x85`** directly. The
    daemon's read path uses :func:`state_query` (`0x8E`, MAC-addressed), which the
    firmware does handle; this direct form is for other fixtures.
    """
    return checksum([PREFIX, OP_LIGHT_STATE, 0x00])


def state_query(mac6: bytes) -> bytes:
    """State/power read by MAC (`0x8E`, inner `0x85`). Reply: `0x04`."""
    if len(mac6) != 6:
        raise ValueError(f"state_query needs a 6-byte MAC, got {len(mac6)} bytes")
    return checksum([PREFIX, OP_STATE_MAC, 0x07, *mac6, OP_LIGHT_STATE])


def version_query_mac(mac6: bytes) -> bytes:
    """Firmware-version read by MAC (`0x9E`). Reply: `0x08`."""
    if len(mac6) != 6:
        raise ValueError(f"version_query_mac needs a 6-byte MAC, got {len(mac6)} bytes")
    return checksum([PREFIX, OP_VERSION_MAC, 0x06, *mac6])


def raw(hexstr: str) -> bytes:
    """Parse a raw, pre-built hex frame such as ``'78 81 01 01 fb'``.

    Bytes may be separated by spaces or commas. The string is taken verbatim —
    the checksum is whatever you typed, NOT recomputed — so this is the escape
    hatch for replaying captured frames and for opcode fuzzing.
    """
    tokens = hexstr.replace(",", " ").split()
    if not tokens:
        raise ValueError("raw frame is empty")
    try:
        return bytes(int(tok, 16) for tok in tokens)
    except ValueError as exc:
        raise ValueError(f"invalid hex byte in raw frame {hexstr!r}: {exc}") from exc


# ---- pixel palette (0xB0, MAC-addressed) --------------------------------
#: Effect-1 parameter block for the pixel effect, copied VERBATIM from a captured
#: app frame (speed / direction / ...). Do not "tidy" these — they are observed
#: bytes. Only effect 1 is mapped so far (others need their own capture).
PIXEL_EFF1_PARAMS = (0x01, 0x00, 0x32, 0x02, 0x2E, 0x01, 0x01)


def pixel_block(token: str) -> list[int]:
    """Encode one palette segment (the app's ``createColoByteArray``) as 3 bytes.

    ``off`` -> a dark segment ``[0x20,0,0]``; ``k<kelvin>`` -> a CCT block
    ``[0x00, cct, gm]``; otherwise a hue 0-359 -> an HSI block. The ``0x10`` flag
    on the HSI high byte marks it HSI (vs ``0x00`` CCT / ``0x20`` off).
    """
    token = str(token).lower()
    if token == "off":
        return [0x20, 0x00, 0x00]
    if token.startswith("k"):
        cct = clamp(int(token[1:]) // 100, CCT_MIN, CCT_MAX)
        return [0x00, cct, GM_NEUTRAL]
    hue = int(token) % 360
    return [((hue >> 8) & 0x0F) | 0x10, hue & 0xFF, 100]


def pixel_params(mac6: bytes, effect: int = 1) -> bytes:
    """Pixel-effect parameter frame (subIndex ``0x00``), MAC-addressed.

    ``78 B0 <len> <MAC6> <params>``. The length byte is the value the hardware
    accepts (``3 + len(params)``), not a plain payload count — the 0xB0 opcode's
    length semantics differ from the direct command frames, so it's reproduced
    verbatim.
    """
    params = list(PIXEL_EFF1_PARAMS)
    return checksum([PREFIX, OP_PIXEL, 3 + len(params)] + list(mac6) + params)


def pixel_palette(mac6: bytes, tokens, effect: int = 1) -> bytes:
    """Pixel-effect colour-palette frame (subIndex ``0x01``), MAC-addressed.

    ``78 B0 <len> <MAC6> <effect> 0x01 <N x 3-byte blocks>``. May exceed the 20-byte
    ATT write cap; the caller chunks it (the device reassembles by header length).
    """
    palette: list[int] = []
    for token in tokens:
        palette += pixel_block(token)
    return checksum([PREFIX, OP_PIXEL, 6 + 2 + len(palette)]
                    + list(mac6) + [effect & 0xFF, 0x01] + palette)


def mac_bytes(addr: str) -> bytes:
    """Convert a colon-separated MAC string (``'AA:BB:CC:DD:EE:FF'``) to 6 bytes."""
    parts = addr.split(":")
    if len(parts) != 6:
        raise ValueError(f"expected a 6-octet MAC, got {addr!r}")
    try:
        octets = [int(p, 16) for p in parts]
    except ValueError as exc:
        raise ValueError(f"invalid hex octet in MAC {addr!r}: {exc}") from exc
    if any(o < 0 or o > 0xFF for o in octets):
        raise ValueError(f"MAC octet out of range in {addr!r}")
    return bytes(octets)


def net_bytes(net) -> bytes:
    """Convert a 4-byte network id (NET4) to exactly 4 bytes.

    Accepts either an 8-hex-char string (e.g. ``'ffffffff'``, the broadcast
    network; separators such as ``:`` or ``-`` are stripped) or an integer (sent
    big-endian). Raises ``ValueError`` if it does not resolve to 4 bytes — the
    same fail-loud contract as :func:`mac_bytes`.

    NET4 is the network id every by-channel/group frame is addressed to;
    ``ffffffff`` is the broadcast network that reaches all tubes.
    """
    if isinstance(net, int):
        if net < 0 or net > 0xFFFFFFFF:
            raise ValueError(f"NET4 int out of 4-byte range: {net}")
        return net.to_bytes(4, "big")          # big-endian, order preserved on the wire
    text = str(net).replace(":", "").replace("-", "").replace(" ", "")
    try:
        raw_bytes = bytes.fromhex(text)        # preserves byte order as written
    except ValueError as exc:
        raise ValueError(f"invalid hex in NET4 {net!r}: {exc}") from exc
    if len(raw_bytes) != 4:
        raise ValueError(f"expected a 4-byte NET4, got {len(raw_bytes)} bytes from {net!r}")
    return raw_bytes
