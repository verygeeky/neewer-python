"""Golden-byte and invariant tests for the by-channel (group/mesh) frame layer.

These are the ``<NET4><CH>``-addressed twins of the by-MAC colour ops plus the
provisioning/grouping frames. As in :mod:`test_frames`, the exact-byte assertions
are the backbone: a change to any builder's output is a protocol regression. Each
builder also gets a checksum-invariant check and a LEN == payload-count check.

Provenance mirrors the docstrings: the colour twins + provisioning are verified
live; the 0xB1 palette layout is by analogy to the 0xB0 by-MAC frame; power is
not yet hardware-verified.
"""
from __future__ import annotations

import pytest

from neewer.protocol import frames


def _csum_ok(frame: bytes) -> bool:
    """A frame's last byte must equal the low byte of the sum of all the rest."""
    return frame[-1] == (sum(frame[:-1]) & 0xFF)


def _len_matches_payload(frame: bytes) -> bool:
    """The LEN byte (frame[2]) must equal the payload byte count (all but 78/op/len/ck)."""
    return frame[2] == len(frame) - 4


NET_BCAST = "ffffffff"
MAC_TL = bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0x01])


# --- net_bytes ------------------------------------------------------------

def test_net_bytes_from_hex_string():
    assert frames.net_bytes("ffffffff") == b"\xff\xff\xff\xff"
    # order preserved, separators stripped
    assert frames.net_bytes("01:02:03:04") == b"\x01\x02\x03\x04"
    assert frames.net_bytes("01-02-03-04") == b"\x01\x02\x03\x04"


def test_net_bytes_from_int_big_endian():
    assert frames.net_bytes(0x01020304) == b"\x01\x02\x03\x04"
    assert frames.net_bytes(0xFFFFFFFF) == b"\xff\xff\xff\xff"


def test_net_bytes_wrong_length_raises():
    with pytest.raises(ValueError):
        frames.net_bytes("ffffff")        # 3 bytes
    with pytest.raises(ValueError):
        frames.net_bytes("ffffffffff")    # 5 bytes


def test_net_bytes_int_out_of_range_raises():
    with pytest.raises(ValueError):
        frames.net_bytes(0x1_00000000)
    with pytest.raises(ValueError):
        frames.net_bytes(-1)


def test_net_bytes_bad_hex_raises():
    with pytest.raises(ValueError):
        frames.net_bytes("zzzzzzzz")


# --- rgbcw_by_channel (0xAA) ----------------------------------------------

def test_rgbcw_by_channel_golden():
    frame = frames.rgbcw_by_channel(NET_BCAST, 1, bri=50, r=0, g=127, b=250)
    assert frame.hex() == "78aa0dffffffff01a832007ffa0000007f"
    assert frame[2] == 0x0D
    assert _len_matches_payload(frame)
    assert _csum_ok(frame)


def test_rgbcw_by_channel_subop_and_clamp():
    frame = frames.rgbcw_by_channel(NET_BCAST, 1, bri=250, r=300)
    assert frame[8] == frames.RGBCW_MAC_SUBOP
    assert frame[9] == 100                 # clamped brightness
    assert frame[10] == 255                # clamped R channel
    assert _csum_ok(frame)


def test_rgbcw_by_channel_rejects_bad_net():
    with pytest.raises(ValueError):
        frames.rgbcw_by_channel("ffff", 1, 50)


# --- hsi_by_channel (0x92) ------------------------------------------------

def test_hsi_by_channel_golden():
    frame = frames.hsi_by_channel(NET_BCAST, 1, 240, 100, 100)
    assert frame.hex() == "78920affffffff0186f00064644f"
    assert frame[2] == 0x0A
    assert _len_matches_payload(frame)
    assert _csum_ok(frame)


def test_hsi_by_channel_subop_hue_wrap_and_clamp():
    frame = frames.hsi_by_channel(NET_BCAST, 1, 400, 200, 200)
    assert frame[8] == frames.OP_HSI       # inner 0x86
    # 400 deg wraps to 40 -> 0x28 little-endian
    assert frame[9] == 0x28 and frame[10] == 0x00
    assert frame[11] == 100 and frame[12] == 100     # sat/lvl clamped
    assert _csum_ok(frame)


# --- gel_by_channel (0xAE) ------------------------------------------------

def test_gel_by_channel_golden():
    frame = frames.gel_by_channel(NET_BCAST, 1, 45, 100, 50, brand=1, gel_no=1)
    assert frame.hex() == "78ae0cffffffff012d006432000101f4"
    assert frame[2] == 0x0C
    assert _len_matches_payload(frame)
    assert _csum_ok(frame)


def test_gel_by_channel_no_inner_subop_and_fields():
    frame = frames.gel_by_channel(NET_BCAST, 1, 45, 100, 50, brand=2, gel_no=7)
    # first payload byte after NET4+CH is the hue low byte (no sub-op)
    assert frame[8:10] == b"\x2d\x00"      # hue 45 little-endian
    assert frame[13] == 2                  # brand
    assert frame[14] == 7                  # gel number
    assert _csum_ok(frame)


# --- xy_by_channel (0xB8) -------------------------------------------------

def test_xy_by_channel_golden():
    frame = frames.xy_by_channel(NET_BCAST, 1, 50, 0.3127, 0.3290)
    assert frame.hex() == "78b80bffffffff0132370cda0c0093"
    assert frame[2] == 0x0B
    assert _len_matches_payload(frame)
    assert _csum_ok(frame)


def test_xy_by_channel_fixed_point_and_trailing_zero():
    frame = frames.xy_by_channel(NET_BCAST, 1, 50, 0.3127, 0.3290)
    assert frame[9:11] == b"\x37\x0c"      # x = 3127 little-endian
    assert frame[11:13] == b"\xda\x0c"     # y = 3290 little-endian
    assert frame[13] == 0x00               # trailing zero
    assert _csum_ok(frame)


# --- pixel_palette_by_channel (0xB1) --------------------------------------

def test_pixel_palette_by_channel_golden():
    frame = frames.pixel_palette_by_channel(NET_BCAST, 1, ["0", "240"])
    assert frame.hex() == "78b10dffffffff01010110006410f0640d"
    assert _len_matches_payload(frame)
    assert _csum_ok(frame)


def test_pixel_palette_by_channel_length_semantics():
    # len = 5 (NET4+CH) + 2 (effect + 0x01) + 3 bytes per token.
    frame = frames.pixel_palette_by_channel(NET_BCAST, 1, ["0", "240"])
    assert frame[2] == 5 + 2 + 2 * 3
    # effect + sub-index sit right after NET4+CH.
    assert frame[8] == 0x01                # effect
    assert frame[9] == 0x01                # palette sub-index


# --- power_by_channel (0x98) ----------------------------------------------

def test_power_by_channel_golden_on():
    frame = frames.power_by_channel(NET_BCAST, 1, True)
    assert frame.hex() == "789807ffffffff01810196"
    assert frame[2] == 0x07
    assert _len_matches_payload(frame)
    assert _csum_ok(frame)


def test_power_by_channel_inner_subop_and_off():
    on = frames.power_by_channel(NET_BCAST, 1, True)
    off = frames.power_by_channel(NET_BCAST, 1, False)
    assert on[8] == frames.OP_POWER        # inner 0x81
    assert on[9] == frames.POWER_ON
    assert off[9] == frames.POWER_OFF
    assert _csum_ok(off)


# --- provision (0x9F) -----------------------------------------------------

def test_provision_golden():
    frame = frames.provision(MAC_TL, 1, NET_BCAST)
    assert frame.hex() == "789f0caabbccddee010101ffffffff1e"
    assert frame[2] == 0x0C
    assert _len_matches_payload(frame)
    assert _csum_ok(frame)


def test_provision_flag_and_fields():
    frame = frames.provision(MAC_TL, 5, "01020304")
    assert frame[3:9] == MAC_TL
    assert frame[9] == 0x01                # constant flag
    assert frame[10] == 5                  # channel
    assert frame[11:15] == b"\x01\x02\x03\x04"


def test_provision_rejects_wrong_mac_length():
    with pytest.raises(ValueError):
        frames.provision(bytes(5), 1, NET_BCAST)


# --- assign_channel (0x8C) ------------------------------------------------

def test_assign_channel_golden():
    frame = frames.assign_channel(MAC_TL, 1, NET_BCAST)
    assert frame.hex() == "788c0baabbccddee0101ffffffff09"
    assert frame[2] == 0x0B
    assert _len_matches_payload(frame)
    assert _csum_ok(frame)


def test_assign_channel_fields():
    frame = frames.assign_channel(MAC_TL, 9, "01020304")
    assert frame[3:9] == MAC_TL
    assert frame[9] == 9                    # channel
    assert frame[10:14] == b"\x01\x02\x03\x04"


def test_assign_channel_rejects_wrong_mac_length():
    with pytest.raises(ValueError):
        frames.assign_channel(bytes(7), 1, NET_BCAST)


# --- group_select (0xD4) --------------------------------------------------

def test_group_select_golden():
    frame = frames.group_select(NET_BCAST, 1)
    assert frame.hex() == "78d406ffffffff01004f"
    assert frame[2] == 0x06
    assert _len_matches_payload(frame)
    assert _csum_ok(frame)


def test_group_select_trailing_zero():
    frame = frames.group_select("01020304", 7)
    assert frame[3:7] == b"\x01\x02\x03\x04"
    assert frame[7] == 7                    # channel
    assert frame[8] == 0x00                 # trailing constant


def test_group_select_rejects_bad_net():
    with pytest.raises(ValueError):
        frames.group_select("ff", 1)


# --- cross-cutting invariant ----------------------------------------------

def test_all_by_channel_builders_satisfy_invariants():
    samples = [
        frames.rgbcw_by_channel(NET_BCAST, 2, 40, 10, 20, 30, 40, 50),
        frames.hsi_by_channel(NET_BCAST, 2, 123, 50, 70),
        frames.gel_by_channel(NET_BCAST, 2, 45, 100, 50),
        frames.xy_by_channel(NET_BCAST, 2, 50, 0.3, 0.3),
        frames.pixel_palette_by_channel(NET_BCAST, 2, ["off", "k3200", "120"]),
        frames.power_by_channel(NET_BCAST, 2, False),
        frames.provision(MAC_TL, 2, NET_BCAST),
        frames.assign_channel(MAC_TL, 2, NET_BCAST),
        frames.group_select(NET_BCAST, 2),
    ]
    for frame in samples:
        assert _csum_ok(frame), frame.hex()
        assert _len_matches_payload(frame), frame.hex()
