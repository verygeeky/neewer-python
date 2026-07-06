"""Tests for :mod:`neewer.protocol.replies` — decoding status notifications.

We assert decoding against real-shaped frames (matching what a TL120C actually
sends) and that anything unrecognised or truncated is preserved as raw hex
rather than silently dropped.
"""
from __future__ import annotations

from neewer.protocol import replies

MAC = bytes.fromhex("aabbccddee01")


def test_parse_version_by_mac_from_real_capture():
    # A real-shaped TL120C version reply: version 2.0.5.
    frame = bytes.fromhex("780817aabbccddee01010a02000502544c313230")
    assert replies.parse(frame)["version"] == "2.0.5"


def test_parse_version_direct():
    frame = bytes([0x78, 0x00, 0x07, 0, 0, 0x01, 0x01, 0x09, 0, 0])
    assert replies.parse(frame)["version"] == "1.1.9"


def test_parse_power_direct_on_off():
    on = bytes([0x78, 0x02, 0x01, 0x01, 0x7C])
    off = bytes([0x78, 0x02, 0x01, 0x00, 0x7B])
    assert replies.parse(on)["power"] == "on"
    assert replies.parse(off)["power"] == "off"


def test_parse_battery_percentage():
    frame = bytes([0x78, 0x05, 0x07, *MAC, 0x50])     # 0x50 == 80
    out = replies.parse(frame)
    assert out["battery"] == 80
    assert out["battery_raw"] == 80
    assert out["mac"] == "aa:bb:cc:dd:ee:01"


def test_parse_battery_external_power_flag():
    # The TL120C reports 0xF0 here — a mains flag, not a percentage.
    frame = bytes([0x78, 0x05, 0x07, *MAC, 0xF0])
    out = replies.parse(frame)
    assert "battery" not in out                       # not a valid percentage
    assert out["power_source"] == "external"
    assert out["battery_raw"] == 240


def test_parse_state_by_mac_mode_and_power():
    frame = bytes([0x78, 0x04, 0x08, *MAC, 0x02, 0x01])
    out = replies.parse(frame)
    assert out["mode"] == 2
    assert out["power"] == "on"
    assert out["mac"] == "aa:bb:cc:dd:ee:01"


def test_parse_temperature_offset():
    # temp = (byte9 & 0xFF) - 50.
    frame = bytes([0x78, 0x12, 0x07, 0, 0, 0, 0, 0, 0, 0x50])
    assert replies.parse(frame)["temp_c"] == 30


def test_parse_ack_provisioning():
    # 78 7f 08 <MAC6> <acked-op> <status> ck — a tube ACKing a 0x9f provision.
    frame = bytes([0x78, 0x7F, 0x08, *MAC, 0x9F, 0x00, 0x00])
    out = replies.parse(frame)
    assert out["mac"] == "aa:bb:cc:dd:ee:01"
    assert out["ack_op"] == 0x9F
    assert out["ack_status"] == 0x00


def test_parse_ack_truncated_falls_back_to_raw():
    # ACK reply code but too short to carry the acked-op / status bytes.
    frame = bytes([0x78, 0x7F, 0x08, *MAC])
    assert "raw" in replies.parse(frame)


def test_unknown_opcode_preserved_as_raw():
    frame = bytes([0x78, 0x7E, 0x01, 0xAB, 0x00])
    assert replies.parse(frame) == {"raw": frame.hex(" ")}


def test_non_neewer_frame_is_raw():
    assert replies.parse(b"\x01\x02\x03") == {"raw": "01 02 03"}


def test_truncated_known_frame_falls_back_to_raw():
    # Battery reply code but too short to carry the percentage byte.
    frame = bytes([0x78, 0x05, 0x02, 0x01])
    assert "raw" in replies.parse(frame)


def test_streamer_support_reply_decoded():
    # 78 17 07 <MAC6> 01 ck -> a TL60 answering the 0xC4 support query
    data = bytes.fromhex("781707aabbccddee6001f3")
    out = replies.parse(data)
    assert out["streamer"] is True
    assert out["mac"] == "aa:bb:cc:dd:ee:60"
