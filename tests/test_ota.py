"""Tests for :mod:`neewer.ota`, the firmware OTA block transport.

No radio: the pure frame builders/parsers are checked directly, and the transfer
state machine runs against a :class:`FakeOtaLink` that plays back ``0x06`` ACKs.
The header golden value is a frame captured verbatim from a real TL60 RGB-3 flash
(``neewer-hardware/firmware.md``), so it validates the builder byte-for-byte.
"""
from __future__ import annotations

import asyncio
import math

import pytest

from neewer import ota


def run(coro):
    return asyncio.run(coro)


# --- pure builders --------------------------------------------------------

def test_check_code_is_additive_sum():
    assert ota.check_code(b"") == 0
    assert ota.check_code(b"\x01\x02\x03") == 6
    assert ota.check_code(b"\xff\xff") == 0x1FE


def test_header_matches_captured_tl60_frame():
    # Verbatim from a live TL60 RGB-3 flash: 142420-byte image, checkCode 0xCBBE77.
    #   78 96 15 | 03 00 05 | 00 02 2c 54 | 00 cb be 77 | "TL60 RGB-3" | 0e
    expected = bytes.fromhex(
        "789615" "030005" "00022c54" "00cbbe77" "544c3630205247422d33" "0e"
    )
    frame = ota.header_frame(
        version=(3, 0, 5), size=142420, code=0x00CBBE77, name="TL60 RGB-3"
    )
    assert frame == expected
    # The length byte is name-bytes + 11.
    assert frame[2] == len(b"TL60 RGB-3") + 11
    # And the checksum is self-consistent.
    assert frame[-1] == sum(frame[:-1]) & 0xFF


def test_header_alt_prefix_for_classify6():
    frame = ota.header_frame((1, 0, 0), 4, 0, "X", alt_header=True)
    assert frame[0] == ota.ALT_PREFIX


def test_block_frame_128_form():
    data = bytes(range(128))
    frame = ota.block_frame(data)
    assert frame[0] == 0x78
    assert frame[1] == ota.OP_OTA_BLOCK
    assert frame[2] == 128  # length byte = data length
    assert frame[3:-1] == data
    assert frame[-1] == sum(frame[:-1]) & 0xFF


def test_block_frame_short_tail():
    data = b"\xaa\xbb\xcc"
    frame = ota.block_frame(data)
    assert frame[2] == 3
    assert frame[3:-1] == data


def test_block_frame_4096_form_length_is_le16_plus_one():
    data = bytes(4096)
    frame = ota.block_frame(data, ota_pro=True)
    assert frame[1] == ota.OP_OTA_BLOCK_PRO
    n = len(data) + 1
    assert frame[2] == (n & 0xFF)
    assert frame[3] == ((n >> 8) & 0xFF)
    assert frame[4:-1] == data


def test_probe_frame():
    assert ota.probe_frame() == ota.frames.checksum([0x78, 0xD0, 0x00])


def test_parse_ack_ops():
    assert ota.parse_ack(bytes([0x78, 0x06, 0x01, ota.ACK_NEXT, 0x00])) == 0
    assert ota.parse_ack(bytes([0x78, 0x06, 0x01, ota.ACK_DONE, 0x00])) == 3
    # A longer 06 01 frame is trimmed to 5 bytes first.
    assert ota.parse_ack(bytes([0x78, 0x06, 0x01, ota.ACK_RESEND, 0x00, 0x99])) == 1
    # Not a 0x06 frame.
    assert ota.parse_ack(bytes([0x78, 0x08, 0x06, 0x01, 0x02, 0x03])) is None


def test_parse_ota_type():
    assert ota.parse_ota_type(bytes([0x78, 0x1A, 0x01, 0x01, 0x00])) is True   # OTA_PRO
    assert ota.parse_ota_type(bytes([0x78, 0x1A, 0x01, 0x00, 0x00])) is False  # 128-byte
    assert ota.parse_ota_type(bytes([0x78, 0x06, 0x01, 0x00, 0x00])) is None


def test_looks_like_arm_image():
    good = (0x200066C8).to_bytes(4, "little") + b"\x00\x00\x00\x08" + bytes(8)
    ok, sp = ota.looks_like_arm_image(good)
    assert ok and sp == 0x200066C8
    bad = b"not an arm image at all"
    ok2, _ = ota.looks_like_arm_image(bad)
    assert not ok2


def test_version_helpers():
    assert ota.version_from_filename("TL60-3_V3.0.5_20250908.bin") == (3, 0, 5)
    assert ota.version_from_filename("no-version-here.bin") is None
    assert ota.parse_version("2.0.5") == (2, 0, 5)
    with pytest.raises(ValueError):
        ota.parse_version("2.0")


# --- fake link + state machine --------------------------------------------

def _arm_image(nbytes: int) -> bytes:
    """A byte blob that passes the vector-table sanity check."""
    head = (0x20006000).to_bytes(4, "little") + b"\x01\x00\x00\x08"
    body = bytes((i * 7) & 0xFF for i in range(nbytes - len(head)))
    return head + body


class FakeOtaLink:
    """A hardware-free :class:`neewer.ota.OtaLink` that scripts device ACKs.

    ``ota_pro`` decides the probe reply (or ``None`` to stay silent, forcing the
    128-byte default). ``drop_resend_at`` injects one op=1 (resend) the first time
    a given block index is sent, to exercise the resend path.
    """

    def __init__(self, ota_pro=None, drop_resend_at=None):
        self._ota_pro = ota_pro
        self._drop_resend_at = drop_resend_at
        self._resent = set()
        self._expect = 0                   # the device's own block counter
        self.on_notify = None
        self.connected = False
        self.frames: list[bytes] = []      # every full frame reassembled from fragments
        self.blocks_sent: list[int] = []   # device-side block index per block frame received

    async def connect(self):
        self.connected = True

    async def subscribe(self, on_notify):
        self.on_notify = on_notify

    def is_connected(self):
        return self.connected

    async def disconnect(self):
        self.connected = False

    async def send_fragmented(self, frame, chunk_size, delay):
        # Fragmentation is transparent to the protocol: no fragment exceeds the
        # GATT limit, and reassembly is just the whole frame back.
        fragments = [frame[i : i + chunk_size] for i in range(0, len(frame), chunk_size)]
        assert all(len(f) <= chunk_size for f in fragments)
        self.frames.append(frame)
        self._react(frame)

    def _emit(self, op):
        self.on_notify(bytes([0x78, 0x06, 0x01, op, 0x00]))

    def _react(self, frame):
        opcode = frame[1]
        if opcode == ota.OP_OTA_PROBE:
            if self._ota_pro is not None:
                self.on_notify(bytes([0x78, 0x1A, 0x01, 1 if self._ota_pro else 0, 0x00]))
            return
        if opcode == ota.OP_OTA_HEADER:
            # Device is ready for the first block.
            self._emit(ota.ACK_NEXT)
            return
        if opcode in (ota.OP_OTA_BLOCK, ota.OP_OTA_BLOCK_PRO):
            # This frame is for the block the device currently expects. The engine
            # keeps its index in lockstep with our counter (advance on NEXT, hold
            # on RESEND), so a resent block carries the same index.
            index = self._expect
            self.blocks_sent.append(index)
            if self._drop_resend_at == index and index not in self._resent:
                self._resent.add(index)
                self._emit(ota.ACK_RESEND)   # ask for the same block again
                return
            if index + 1 >= self._total_blocks:
                self._emit(ota.ACK_DONE)     # final block accepted → commit
            else:
                self._expect = index + 1
                self._emit(ota.ACK_NEXT)
            return

    # set by _flash() before the run so the fake knows when to commit
    _total_blocks = 0


def _flash(link, image, **kw):
    # Size the fake's block count off the type it will *report* (which the engine
    # probes for), so device and engine agree on when the last block lands. A
    # silent probe (link ota_pro None) means the engine's 128-byte default.
    block_size = ota.BLOCK_SIZE_PRO if link._ota_pro else ota.BLOCK_SIZE_STD
    link._total_blocks = math.ceil(len(image) / block_size)
    defaults = dict(version=(3, 0, 5), name="TL60 RGB-3", settle_secs=0, chunk_delay=0)
    defaults.update(kw)
    return run(ota.flash(link, image, **defaults))


def test_flash_drives_all_blocks_in_order_and_commits():
    image = _arm_image(128 * 5 + 40)  # 6 blocks (5 full + a tail)
    link = FakeOtaLink()
    result = _flash(link, image)
    assert result.committed is True
    assert result.total_blocks == 6
    assert result.resends == 0
    # Every block index sent exactly once, in order.
    assert link.blocks_sent == [0, 1, 2, 3, 4, 5]
    # The header went out before any block.
    assert link.frames[0][1] in (ota.OP_OTA_PROBE, ota.OP_OTA_HEADER)


def test_flash_services_a_resend_without_advancing():
    image = _arm_image(128 * 3)
    link = FakeOtaLink(drop_resend_at=1)
    result = _flash(link, image)
    assert result.committed is True
    assert result.resends == 1
    # Block 1 is sent twice (once dropped, once resent); order otherwise intact.
    assert link.blocks_sent == [0, 1, 1, 2]


def test_flash_probe_selects_4096_blocks():
    image = _arm_image(4096 * 2)
    link = FakeOtaLink(ota_pro=True)
    result = _flash(link, image, ota_pro=None)  # let it probe
    assert result.ota_pro is True
    assert result.block_size == ota.BLOCK_SIZE_PRO
    assert result.total_blocks == 2


def test_flash_defaults_to_128_when_probe_is_silent():
    image = _arm_image(128 * 2)
    link = FakeOtaLink(ota_pro=None)  # never answers the probe
    result = _flash(link, image, ota_pro=None, probe_timeout=0.05)
    assert result.ota_pro is False
    assert result.block_size == ota.BLOCK_SIZE_STD


def test_flash_rejects_a_non_arm_image():
    link = FakeOtaLink()
    with pytest.raises(ota.OtaError):
        run(ota.flash(link, b"junk", version=(1, 0, 0), name="X", settle_secs=0, chunk_delay=0))


def test_check_is_readonly_and_never_sends_firmware():
    image = _arm_image(128 * 4)
    link = FakeOtaLink(ota_pro=False)
    link._total_blocks = 4
    summary = run(ota.check(link, image, settle_secs=0, chunk_delay=0, probe_timeout=0.05))
    assert summary["total_blocks"] == 4
    assert summary["block_size"] == 128
    # Only the probe was ever written: no header, no blocks.
    sent_opcodes = {f[1] for f in link.frames}
    assert sent_opcodes == {ota.OP_OTA_PROBE}


def test_check_rejects_bad_image_before_connecting():
    link = FakeOtaLink()
    with pytest.raises(ota.OtaError):
        run(ota.check(link, b"tiny", settle_secs=0))
    assert link.connected is False
