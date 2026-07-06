"""Golden-byte tests for the query/read frame builders in :mod:`neewer.protocol.frames`.

These ask a light for state; like every other frame they are reverse-engineered
protocol bytes and must not silently change. We assert the exact output and the
checksum invariant, and that MAC-addressed queries validate their MAC length.
"""
from __future__ import annotations

import pytest

from neewer.protocol import frames

MAC = frames.mac_bytes("aa:bb:cc:dd:ee:01")


def _checksum_ok(frame: bytes) -> bool:
    return frame[-1] == sum(frame[:-1]) & 0xFF


def test_version_query_direct_exact_bytes():
    # The canonical direct version query: 78 80 00 f8.
    assert frames.version_query() == bytes([0x78, 0x80, 0x00, 0xF8])


def test_light_state_query_exact_bytes():
    # Documented as 78 85 00 fd.
    assert frames.light_state_query() == bytes([0x78, 0x85, 0x00, 0xFD])


def test_battery_query_by_mac():
    frame = frames.battery_query(MAC)
    assert frame[:3] == bytes([0x78, 0x95, 0x06])
    assert frame[3:9] == MAC
    assert _checksum_ok(frame)


def test_state_query_wraps_inner_light_state_opcode():
    # 0x8E carries the MAC then the inner 0x85 light-state opcode.
    frame = frames.state_query(MAC)
    assert frame[:3] == bytes([0x78, 0x8E, 0x07])
    assert frame[3:9] == MAC
    assert frame[9] == frames.OP_LIGHT_STATE      # 0x85 inner op
    assert _checksum_ok(frame)


def test_version_query_mac():
    frame = frames.version_query_mac(MAC)
    assert frame[:3] == bytes([0x78, 0x9E, 0x06])
    assert frame[3:9] == MAC
    assert _checksum_ok(frame)


@pytest.mark.parametrize("builder", [
    frames.battery_query, frames.state_query, frames.version_query_mac,
])
def test_mac_addressed_queries_reject_bad_mac_length(builder):
    with pytest.raises(ValueError):
        builder(b"\x01\x02\x03")                  # not 6 bytes
