"""Golden-byte and invariant tests for :mod:`neewer.protocol.frames`.

These frames are reverse-engineered protocol bytes confirmed against real
hardware. The exact-byte assertions here are the backbone of the suite: if a
refactor changes any builder's output, that is a protocol regression and must
fail loudly. Every builder also gets a checksum-invariant check so the
``sum & 0xFF`` trailer can never silently drift.
"""
from __future__ import annotations

import pytest

from neewer.protocol import frames


def _csum_ok(frame: bytes) -> bool:
    """A frame's last byte must equal the low byte of the sum of all the rest."""
    return frame[-1] == (sum(frame[:-1]) & 0xFF)


# --- checksum / clamp primitives -----------------------------------------

def test_checksum_appends_low_byte_of_sum():
    # 0x78 + 0x81 + 0x01 + 0x01 = 0xFB, which fits in one byte.
    assert frames.checksum([0x78, 0x81, 0x01, 0x01]) == b"\x78\x81\x01\x01\xfb"


def test_checksum_wraps_modulo_256():
    # sum = 0x1FE; only the low byte (0xFE) is kept.
    assert frames.checksum([0xFF, 0xFF]) == b"\xff\xff\xfe"


@pytest.mark.parametrize("value,lo,hi,expected", [
    (50, 0, 100, 50),     # in range
    (-5, 0, 100, 0),      # below low bound
    (250, 0, 100, 100),   # above high bound
    (3.9, 0, 100, 3),     # float is truncated by int(), not rounded
])
def test_clamp(value, lo, hi, expected):
    assert frames.clamp(value, lo, hi) == expected


# --- power ----------------------------------------------------------------

def test_power_on_golden():
    assert frames.power(True) == b"\x78\x81\x01\x01\xfb"


def test_power_off_golden():
    assert frames.power(False) == b"\x78\x81\x01\x02\xfc"


# --- hsi ------------------------------------------------------------------

def test_hsi_golden():
    # hue 240 -> little-endian f0 00; sat/bri 100 -> 0x64 each.
    assert frames.hsi(240, 100, 100) == b"\x78\x86\x04\xf0\x00\x64\x64\xba"


def test_hsi_hue_wraps_modulo_360():
    # 400 deg wraps to 40 deg, so it must equal an explicit hue of 40.
    assert frames.hsi(400, 100, 100) == frames.hsi(40, 100, 100)
    # A full turn wraps to 0.
    assert frames.hsi(360) == frames.hsi(0)


def test_hsi_clamps_sat_and_bri():
    over = frames.hsi(0, 200, 200)
    assert over == frames.hsi(0, 100, 100)
    under = frames.hsi(0, -10, -10)
    assert under == frames.hsi(0, 0, 0)


def test_hsi_low_byte_carries_hue_over_255():
    # hue 300 -> 0x012C, so low byte 0x2C, high byte 0x01.
    frame = frames.hsi(300)
    assert frame[3] == 0x2C
    assert frame[4] == 0x01


# --- cct ------------------------------------------------------------------

def test_cct_golden():
    # App's GM-capable 4-byte form: len=0x04, payload = bri, temp, gm, dim-curve(0).
    assert frames.cct(50, 56, 50) == b"\x78\x87\x04\x32\x38\x32\x00\x9f"


def test_cct_length_byte_matches_payload_count():
    frame = frames.cct(50, 56, 50)
    assert frame[2] == 0x04                      # len == 4 payload bytes
    assert len(frame) == 3 + 4 + 1               # header + payload + checksum
    assert frame[6] == 0                          # trailing dimming-curve byte


def test_cct_temp_clamped_to_hardware_range():
    # Below CCT_MIN clamps up to 32; above CCT_MAX clamps down to 85.
    assert frames.cct(100, 10)[4] == frames.CCT_MIN
    assert frames.cct(100, 999)[4] == frames.CCT_MAX


def test_cct_gm_default_is_neutral():
    assert frames.cct(100, 56)[5] == frames.GM_NEUTRAL


# --- scene ----------------------------------------------------------------

def test_scene_length_byte_counts_effect_plus_params():
    # No params: length is 1 (the effect id alone).
    assert frames.scene(1) == b"\x78\x88\x01\x01\x02"
    # Two params: length is 3 (effect + 2 params).
    frame = frames.scene(5, 1, 2)
    assert frame[2] == 3
    assert frame == b"\x78\x88\x03\x05\x01\x02\x0b"


# --- scene_by_mac (0x91) --------------------------------------------------

def test_scene_by_mac_golden_effect1():
    # Effect 1 (Lightning), params bri=0x32 cct=0x37 rate=0x05.
    mac = bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0x01])
    frame = frames.scene_by_mac(mac, 0x01, 0x32, 0x37, 0x05)
    assert frame == bytes.fromhex("78 91 0b aa bb cc dd ee 01 8b 01 32 37 05 0b".replace(" ", ""))


def test_scene_by_mac_length_and_subop():
    mac = bytes([0x11, 0x22, 0x33, 0x44, 0x55, 0x66])
    frame = frames.scene_by_mac(mac, 7, 1, 2)
    assert frame[:2] == b"\x78\x91"
    assert frame[2] == 6 + 4          # MAC6 + [subop, effect, 2 params]
    assert frame[3:9] == mac
    assert frame[9] == frames.SCENE_MAC_SUBOP
    assert frame[10] == 7             # effect id
    assert _csum_ok(frame)


def test_scene_by_mac_rejects_wrong_mac_length():
    with pytest.raises(ValueError):
        frames.scene_by_mac(bytes(5), 1)


# --- by-MAC colour modes: rgbcw / xy / gel (TL120C) -----------------------

MAC_TL = bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0x01])


def test_rgbcw_by_mac_golden():
    # bri=50, R=0 G=127 B=250 C=0 W=0 -> verified live on hardware.
    frame = frames.rgbcw_by_mac(MAC_TL, bri=50, r=0, g=127, b=250, c=0, w=0)
    assert frame.hex() == "78a90eaabbccddee01a832007ffa0000007f"


def test_rgbcw_by_mac_length_subop_and_invariant():
    frame = frames.rgbcw_by_mac(MAC_TL, 50, 0, 127, 250, 0, 0)
    assert frame[:2] == b"\x78\xa9"
    assert frame[2] == 0x0E                # 6 MAC + subop + bri + 5ch + decBri
    assert frame[3:9] == MAC_TL
    assert frame[9] == frames.RGBCW_MAC_SUBOP
    assert _csum_ok(frame)


def test_rgbcw_by_mac_clamps_bri_and_channels():
    # bri > 100 clamps to 100; a channel > 255 clamps to 255.
    frame = frames.rgbcw_by_mac(MAC_TL, bri=250, r=300, g=127, b=250, c=0, w=0)
    assert frame[10] == 100                # clamped brightness
    assert frame[11] == 255                # clamped R channel
    assert _csum_ok(frame)


def test_rgbcw_by_mac_rejects_wrong_mac_length():
    with pytest.raises(ValueError):
        frames.rgbcw_by_mac(bytes(5), 50)


def test_xy_by_mac_golden():
    # D65 white point 0.3127, 0.3290 -> verified live on hardware.
    frame = frames.xy_by_mac(MAC_TL, bri=50, x=0.3127, y=0.3290)
    assert frame.hex() == "78b70caabbccddee0132370cda0c0093"


def test_xy_by_mac_encodes_fixed_point_little_endian():
    frame = frames.xy_by_mac(MAC_TL, 50, 0.3127, 0.3290)
    assert frame[2] == 0x0C                # 6 MAC + bri + 2 + 2 + 1
    assert frame[10:12] == b"\x37\x0c"     # x = 3127 little-endian
    assert frame[12:14] == b"\xda\x0c"     # y = 3290 little-endian
    assert frame[14] == 0x00               # trailing i3 byte
    assert _csum_ok(frame)


def test_xy_by_mac_clamps_bri():
    frame = frames.xy_by_mac(MAC_TL, bri=250, x=0.3, y=0.3)
    assert frame[9] == 100
    assert _csum_ok(frame)


def test_xy_by_mac_rejects_wrong_mac_length():
    with pytest.raises(ValueError):
        frames.xy_by_mac(bytes(5), 50, 0.3, 0.3)


def test_gel_by_mac_golden():
    # our own amber frame (no app capture): hue 45, sat 100, bri 50, ROSCO #1.
    frame = frames.gel_by_mac(MAC_TL, hue=45, sat=100, bri=50, brand=1, gel_no=1)
    assert frame.hex() == "78ad0daabbccddee012d006432000101f4"


def test_gel_by_mac_length_and_fields():
    frame = frames.gel_by_mac(MAC_TL, 45, 100, 50, brand=1, gel_no=1)
    assert frame[:2] == b"\x78\xad"
    assert frame[2] == 0x0D                # 6 MAC + 2 hue + sat + bri + decBri + brand + gelNo
    assert frame[9:11] == b"\x2d\x00"      # hue 45 little-endian
    assert frame[14] == 1                  # brand
    assert frame[15] == 1                  # gel number
    assert _csum_ok(frame)


def test_gel_by_mac_hue_wraps_and_clamps():
    # hue 405 wraps to 45; sat/bri over 100 clamp.
    assert frames.gel_by_mac(MAC_TL, 405, 100, 50, 1, 1) == \
        frames.gel_by_mac(MAC_TL, 45, 100, 50, 1, 1)
    frame = frames.gel_by_mac(MAC_TL, 45, 200, 200, 1, 0)
    assert frame[11] == 100 and frame[12] == 100


def test_gel_by_mac_rejects_wrong_mac_length():
    with pytest.raises(ValueError):
        frames.gel_by_mac(bytes(5), 45, 100, 50)


# --- identify (0x99) ------------------------------------------------------

def test_identify_golden_and_invariant():
    mac = bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0x01])
    frame = frames.identify(mac)
    assert frame[:3] == b"\x78\x99\x06"
    assert frame[3:9] == mac
    assert _csum_ok(frame)


def test_identify_rejects_wrong_mac_length():
    with pytest.raises(ValueError):
        frames.identify(bytes(5))


# --- battery_query --------------------------------------------------------

def test_battery_query_golden_and_invariant():
    mac = bytes([0x11, 0x22, 0x33, 0x44, 0x55, 0x66])
    frame = frames.battery_query(mac)
    assert frame[:3] == b"\x78\x95\x06"
    assert frame[3:9] == mac
    assert _csum_ok(frame)


def test_battery_query_rejects_wrong_length():
    with pytest.raises(ValueError):
        frames.battery_query(bytes(5))


# --- temp_query (0xB3, pairs with the 0x12 reply decoder) -----------------

def test_temp_query_golden_and_invariant():
    mac = bytes([0x11, 0x22, 0x33, 0x44, 0x55, 0x66])
    frame = frames.temp_query(mac)
    assert frame[:3] == b"\x78\xb3\x06"
    assert frame[3:9] == mac
    assert _csum_ok(frame)


def test_temp_query_rejects_wrong_length():
    with pytest.raises(ValueError):
        frames.temp_query(bytes(5))


# --- raw ------------------------------------------------------------------

def test_raw_passthrough_verbatim_spaces_and_commas():
    assert frames.raw("78 81 01 01 fb") == b"\x78\x81\x01\x01\xfb"
    assert frames.raw("78,81,01,01,fb") == b"\x78\x81\x01\x01\xfb"


def test_raw_does_not_recompute_checksum():
    # Trailing byte is wrong on purpose; raw must keep it as typed.
    assert frames.raw("78 81 01 01 00") == b"\x78\x81\x01\x01\x00"


def test_raw_empty_raises():
    with pytest.raises(ValueError):
        frames.raw("   ")


def test_raw_bad_hex_raises():
    with pytest.raises(ValueError):
        frames.raw("78 zz")


# --- mac_bytes ------------------------------------------------------------

def test_mac_bytes_golden():
    assert frames.mac_bytes("AA:BB:CC:DD:EE:FF") == bytes(
        [0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF])


def test_mac_bytes_wrong_octet_count_raises():
    with pytest.raises(ValueError):
        frames.mac_bytes("AA:BB:CC")


def test_mac_bytes_bad_hex_raises():
    with pytest.raises(ValueError):
        frames.mac_bytes("AA:BB:CC:DD:EE:ZZ")


def test_mac_bytes_octet_out_of_range_raises():
    # "1FF" parses as 511, which is out of byte range.
    with pytest.raises(ValueError):
        frames.mac_bytes("AA:BB:CC:DD:EE:1FF")


# --- cross-cutting invariant ---------------------------------------------

def test_all_builders_satisfy_checksum_invariant():
    samples = [
        frames.power(True),
        frames.power(False),
        frames.hsi(123, 50, 70),
        frames.cct(40, 45, 60),
        frames.scene(3, 9, 9),
        frames.battery_query(bytes(range(6))),
    ]
    for frame in samples:
        assert _csum_ok(frame), frame.hex()


# --- pixel palette (0xB0) -------------------------------------------------

def test_pixel_block_encodings():
    assert frames.pixel_block("off") == [0x20, 0x00, 0x00]
    assert frames.pixel_block("k3200") == [0x00, 32, 50]      # CCT block
    assert frames.pixel_block("0") == [0x10, 0x00, 100]       # hue 0, HSI flag 0x10
    assert frames.pixel_block("240") == [0x10, 240, 100]      # hue 240


def test_pixel_bad_token_raises():
    with pytest.raises(ValueError):
        frames.pixel_palette(frames.mac_bytes("AA:BB:CC:DD:EE:01"), ["notacolour"])


def test_streamer_support_query_golden_and_invariant():
    mac = bytes([0x11, 0x22, 0x33, 0x44, 0x55, 0x66])
    frame = frames.streamer_support_query(mac)
    assert frame[:3] == b"\x78\xc4\x06"
    assert frame[3:9] == mac
    assert _csum_ok(frame)


def test_streamer_support_query_rejects_wrong_length():
    with pytest.raises(ValueError):
        frames.streamer_support_query(bytes(5))
