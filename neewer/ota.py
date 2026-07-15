"""Firmware OTA flashing over the custom ``0x78`` block transport.

This firmware update is not Nordic DFU: it rides the normal ``69400001`` control
service and streams the raw firmware image in fixed-size blocks under a
device-driven ACK protocol. This module sends the same frames the official app
sends, plus the one thing the app gets for free from its BLE stack and we do not:
paced 20-byte GATT fragmentation.

Why the pacing matters. These are two-chip fixtures: a BLE-UART radio module
forwards frames over an internal UART to a separate MCU. During OTA that UART
reassembler is the bottleneck, not the BLE link. Push the 20-byte fragments too
fast and it silently overruns, so the device drops fragments and asks for resends
(``0x06`` op=1). Confirmed on a TL60 RGB-3: ~6 ms spacing gave ~75 % resends,
~20 ms gave zero. So every fragment is a separate write-with-response GATT write
with a delay after it, rather than one ``write_gatt_char`` the OS chops up however
it likes. See ``neewer-hardware/firmware.md``.

The protocol, as sent:

* **Probe** ``0xD0`` -> reply ``0x1A``: ``frame[3] == 1`` means 4096-byte blocks
  ("OTA_PRO"), else 128-byte. Not every fixture answers (the TL120C ignores it),
  so a missing reply defaults to 128-byte "OTA".
* **Header** ``0x96``: ``<v1 v2 v3> <size:BE32> <checkCode:BE32> <name>`` where
  ``checkCode`` is a plain 32-bit additive sum over the whole image.
* **Blocks** ``0x97`` (<=128 B) or ``0xCF`` (<=4096 B). No block index travels in
  the frame; the device tracks the sequence and drives it.
* **Flow control** reply ``0x06``, op at ``frame[3]``: 0=next, 1=resend current,
  2=restart at block 0, 3=done (committed), 4=fail.

Safety: :func:`check` never sends a firmware byte (probe, validation, and a link
stability hold only). :func:`flash` requires the caller to opt in explicitly.
A dropped block fails cleanly and is retryable; nothing is committed until the
device sends op=3, so an interrupted flash leaves the old firmware intact. Stop
any daemon holding the light first, or it will fight this for the adapter/MAC.
"""
from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from .protocol import frames
from .transport import NOTIFY_UUID, WRITE_UUID

# --- Opcodes (see module docstring) ---------------------------------------

OP_OTA_PROBE = 0xD0     #: OTA-type probe -> reply 0x1A
OP_OTA_HEADER = 0x96    #: update-info header
OP_OTA_BLOCK = 0x97     #: image block, 128-byte form
OP_OTA_BLOCK_PRO = 0xCF  #: image block, 4096-byte form ("OTA_PRO")
REPLY_OTA_ACK = 0x06    #: flow-control ACK (op at byte 3)
REPLY_OTA_TYPE = 0x1A   #: probe reply (byte 3: 1 => OTA_PRO)

#: A few fixtures use ``0x85`` in place of the usual ``0x78`` prefix on OTA
#: frames. Our target fixtures (TL60 / TL120C) are the ``0x78`` kind; this is
#: exposed so the odd ones can still be driven.
ALT_PREFIX = 0x85

# --- Transfer parameters --------------------------------------------------

#: ATT MTU is 23, so a write carries at most 20 payload bytes. Frames longer than
#: this are split into 20-byte GATT fragments; continuation fragments carry no
#: ``0x78`` prefix, and the device reassembles them using the frame's length byte.
GATT_FRAGMENT = 20

#: Default delay after each fragment. ~20 ms keeps the two-chip UART reassembler
#: from overrunning (the resend-storm fix). Raise it on a marginal link.
DEFAULT_CHUNK_DELAY = 0.020

BLOCK_SIZE_STD = 128
BLOCK_SIZE_PRO = 4096

# Flow-control operations carried in a 0x06 reply at byte 3.
ACK_NEXT = 0
ACK_RESEND = 1
ACK_RESTART = 2
ACK_DONE = 3
ACK_FAIL = 4


class OtaError(RuntimeError):
    """An OTA flash could not proceed or the device rejected it."""


# --- Pure frame builders / parsers ----------------------------------------
# The golden test asserts the header against a frame captured from a real TL60
# flash, so these are pinned to known-good bytes.


def check_code(image: bytes) -> int:
    """The header's ``checkCode``: a 32-bit additive sum over the whole image.

    This is the transfer's integrity check (independent of the manifest MD5). The
    device recomputes it and refuses to commit on a mismatch.
    """
    return sum(image) & 0xFFFFFFFF


def _be32(value: int) -> list[int]:
    """32-bit big-endian, the byte order the header fields use."""
    return [(value >> 24) & 0xFF, (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF]


def probe_frame(alt_header: bool = False) -> bytes:
    """``0xD0`` OTA-type probe."""
    prefix = ALT_PREFIX if alt_header else frames.PREFIX
    return frames.checksum([prefix, OP_OTA_PROBE, 0x00])


def header_frame(
    version: tuple[int, int, int],
    size: int,
    code: int,
    name: str,
    alt_header: bool = False,
) -> bytes:
    """``0x96`` update-info header.

    ``name`` is the cosmetic model string; the device ignores it, but it is part
    of the frame (and the length byte), so we send it for byte-for-byte parity.
    """
    v1, v2, v3 = version
    name_bytes = name.encode("ascii")
    # len byte = 3 version + 4 size + 4 checkCode + name = name + 11.
    length = len(name_bytes) + 11
    prefix = ALT_PREFIX if alt_header else frames.PREFIX
    payload = [prefix, OP_OTA_HEADER, length & 0xFF, v1 & 0xFF, v2 & 0xFF, v3 & 0xFF]
    payload += _be32(size)
    payload += _be32(code)
    payload += list(name_bytes)
    return frames.checksum(payload)


def block_frame(data: bytes, ota_pro: bool = False, alt_header: bool = False) -> bytes:
    """One image block: ``0x97`` (<=128 B) or ``0xCF`` (<=4096 B).

    The 128-byte form is a single length byte; the 4096-byte form is a
    little-endian 16-bit ``len + 1``.
    """
    prefix = ALT_PREFIX if alt_header else frames.PREFIX
    if ota_pro:
        n = len(data) + 1  # the 4096-byte form's length field counts data bytes + 1
        payload = [prefix, OP_OTA_BLOCK_PRO, n & 0xFF, (n >> 8) & 0xFF] + list(data)
    else:
        payload = [prefix, OP_OTA_BLOCK, len(data) & 0xFF] + list(data)
    return frames.checksum(payload)


def parse_ack(frame: bytes) -> Optional[int]:
    """Return the flow-control op from a ``0x06`` ACK, or ``None`` if not one.

    A longer ``06 01 ...`` frame is trimmed to 5 bytes first, then a 5-byte
    ``0x06`` frame yields its op at byte 3.
    """
    b = frame
    if len(b) > 5 and b[1] == REPLY_OTA_ACK and b[2] == 0x01:
        b = b[:5]
    if len(b) == 5 and b[1] == REPLY_OTA_ACK:
        return b[3]
    return None


def parse_ota_type(frame: bytes) -> Optional[bool]:
    """Return ``True`` for OTA_PRO (4096 B), ``False`` for OTA (128 B), else ``None``.

    A 5-byte ``0x1A`` reply whose byte 3 is 1 means OTA_PRO.
    """
    if len(frame) == 5 and frame[1] == REPLY_OTA_TYPE:
        return frame[3] == 1
    return None


def looks_like_arm_image(image: bytes) -> tuple[bool, int]:
    """Sanity-check that ``image`` starts with an ARM Cortex-M vector table.

    The images are plaintext: the first word is the initial stack pointer, which
    lives in SRAM (``0x2000_0000``..``0x2004_0000`` on these parts). Returns
    ``(ok, initial_sp)``. A quick guard against flashing a truncated or wrong file.
    """
    if len(image) < 8:
        return False, 0
    initial_sp = int.from_bytes(image[0:4], "little")
    ok = 0x20000000 <= initial_sp <= 0x20040000
    return ok, initial_sp


_VERSION_IN_NAME = re.compile(r"[_-]V(\d+)\.(\d+)\.(\d+)", re.IGNORECASE)


def version_from_filename(path: str | Path) -> Optional[tuple[int, int, int]]:
    """Pull an ``M.M.P`` version out of a filename like ``TL60-3_V3.0.5_...bin``."""
    match = _VERSION_IN_NAME.search(Path(path).name)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def parse_version(text: str) -> tuple[int, int, int]:
    """Parse ``"3.0.5"`` into ``(3, 0, 5)``."""
    parts = text.strip().split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"version must be M.M.P (e.g. 3.0.5), got {text!r}")
    a, b, c = (int(p) for p in parts)
    return a, b, c


# --- The BLE link the engine drives ---------------------------------------


class OtaLink(Protocol):
    """The radio operations :func:`flash` / :func:`check` need.

    Deliberately narrower and lower-level than :class:`neewer.transport.Transport`:
    OTA needs paced, fragmented, acknowledged writes and a notify subscription,
    which the fire-and-forget control path does not. Injecting this Protocol lets
    the transfer be tested with no radio (see ``tests/test_ota.py``).
    """

    async def connect(self) -> None:
        """Find and connect to the target light. Raises on failure."""
        ...

    async def subscribe(self, on_notify: Callable[[bytes], None]) -> None:
        """Subscribe to status notifications; call ``on_notify(bytes)`` per frame."""
        ...

    async def send_fragmented(self, frame: bytes, chunk_size: int, delay: float) -> None:
        """Write ``frame`` as ``chunk_size``-byte GATT fragments, ``delay`` s apart."""
        ...

    def is_connected(self) -> bool:
        """Whether the link is currently up."""
        ...

    async def disconnect(self) -> None:
        """Disconnect (best-effort)."""
        ...


class BleakOtaLink:
    """The default :class:`OtaLink`, over ``bleak``.

    ``bleak`` is imported lazily so importing this module (the pure builders, the
    engine, a fake link) never requires a BLE stack. Writes use write-with-response:
    the write characteristic accepts it, which gives link-layer backpressure on top
    of the ``0x06`` block ACK.
    """

    def __init__(self, mac: str, scan_timeout: float = 20.0) -> None:
        self.mac = mac
        self.scan_timeout = scan_timeout
        self._client: Any = None

    async def connect(self) -> None:
        from bleak import BleakClient, BleakScanner

        device = await BleakScanner.find_device_by_address(self.mac, timeout=self.scan_timeout)
        if device is None:
            raise OtaError(
                f"light {self.mac} not found within {self.scan_timeout:.0f}s "
                "(is it in BT mode, powered on, and not held by another central?)"
            )
        self._client = BleakClient(device)
        await self._client.connect()

    async def subscribe(self, on_notify: Callable[[bytes], None]) -> None:
        await self._client.start_notify(
            NOTIFY_UUID, lambda _ch, data: on_notify(bytes(data))
        )

    async def send_fragmented(self, frame: bytes, chunk_size: int, delay: float) -> None:
        for start in range(0, len(frame), chunk_size):
            fragment = frame[start : start + chunk_size]
            await self._client.write_gatt_char(WRITE_UUID, fragment, response=True)
            if delay:
                await asyncio.sleep(delay)

    def is_connected(self) -> bool:
        return bool(self._client is not None and self._client.is_connected)

    async def disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass


# --- Results / callbacks --------------------------------------------------


@dataclass
class FlashResult:
    """Outcome of a completed :func:`flash`."""

    committed: bool     #: device sent op=3 (accepted the image)
    total_blocks: int
    resends: int        #: how many op=1 resend requests we serviced
    block_size: int
    ota_pro: bool


#: ``on_progress(block, total_blocks, pct, resends)``, called after each block.
ProgressCallback = Callable[[int, int, int, int], None]
#: ``on_event(message)`` narration for the caller/CLI (connect, probe, commit).
EventCallback = Callable[[str], None]


def _noop(*_args: Any) -> None:
    pass


async def _resolve_type(
    link: OtaLink,
    type_future: "asyncio.Future[bool]",
    *,
    alt_header: bool,
    chunk_delay: float,
    timeout: float,
) -> bool:
    """Send the ``0xD0`` probe and wait for a ``0x1A`` reply; default to 128-byte.

    Probes then falls back cleanly for fixtures that never answer (e.g. the
    TL120C), which just use 128-byte OTA.
    """
    await link.send_fragmented(probe_frame(alt_header), GATT_FRAGMENT, chunk_delay)
    try:
        return await asyncio.wait_for(type_future, timeout)
    except asyncio.TimeoutError:
        return False


async def _settle(link: OtaLink, secs: float, on_event: EventCallback) -> None:
    """Hold the link for ``secs`` as the pre-flash gate, aborting if it drops.

    We check periodically rather than sleeping the whole window so a mid-hold drop
    fails fast, before any firmware byte is sent.
    """
    if secs <= 0:
        return
    on_event(f"checking link stability for {secs:.0f}s before flashing")
    steps = max(1, int(secs * 2))
    for _ in range(steps):
        await asyncio.sleep(secs / steps)
        if not link.is_connected():
            raise OtaError("link dropped during the stability check; aborting before flash")
    on_event("link held steady, OK to proceed")


async def check(
    link: OtaLink,
    image: bytes,
    *,
    alt_header: bool = False,
    chunk_delay: float = DEFAULT_CHUNK_DELAY,
    settle_secs: float = 20.0,
    probe_timeout: float = 3.0,
    on_event: EventCallback = _noop,
) -> dict:
    """Dry run: validate the image, resolve the OTA type, hold the link. No write.

    Sends only the read-only ``0xD0`` probe, never the header or a block, so it is
    always safe. Returns a summary dict (size, checkCode, block layout, type).
    Raises :class:`OtaError` on a failed image sanity check or an unstable link.
    """
    ok, initial_sp = looks_like_arm_image(image)
    if not ok:
        raise OtaError(
            f"{len(image)} B image does not start with a plausible ARM vector table "
            f"(initial SP {initial_sp:#010x} not in SRAM); refusing (wrong or truncated file?)"
        )

    type_future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    def on_notify(frame: bytes) -> None:
        is_pro = parse_ota_type(frame)
        if is_pro is not None and not type_future.done():
            type_future.set_result(is_pro)

    await link.connect()
    on_event("connected")
    await link.subscribe(on_notify)
    ota_pro = await _resolve_type(
        link, type_future, alt_header=alt_header, chunk_delay=chunk_delay, timeout=probe_timeout
    )
    block_size = BLOCK_SIZE_PRO if ota_pro else BLOCK_SIZE_STD
    await _settle(link, settle_secs, on_event)

    code = check_code(image)
    total_blocks = math.ceil(len(image) / block_size)
    summary = {
        "bytes": len(image),
        "check_code": code,
        "initial_sp": initial_sp,
        "ota_pro": ota_pro,
        "block_size": block_size,
        "total_blocks": total_blocks,
    }
    on_event(
        f"dry run OK: {len(image)} B, checkCode {code:#010x}, "
        f"{'OTA_PRO 4096' if ota_pro else 'OTA 128'}-byte blocks x {total_blocks}"
    )
    return summary


async def flash(
    link: OtaLink,
    image: bytes,
    *,
    version: tuple[int, int, int],
    name: str,
    ota_pro: Optional[bool] = None,
    alt_header: bool = False,
    chunk_delay: float = DEFAULT_CHUNK_DELAY,
    settle_secs: float = 20.0,
    probe_timeout: float = 3.0,
    header_timeout: float = 1.2,
    header_retries: int = 5,
    ack_timeout: float = 30.0,
    on_progress: ProgressCallback = _noop,
    on_event: EventCallback = _noop,
) -> FlashResult:
    """Flash ``image`` over the OTA block transport. This writes firmware.

    The device drives the transfer: after the header it sends a ``0x06`` for every
    block (op=0 advance, op=1 resend, op=2 restart, op=3 done, op=4 fail). We only
    react. ``ota_pro`` forces the block size; left ``None`` it is probed (default
    128-byte). Raises :class:`OtaError` on failure; on success the device has
    committed and is rebooting (and typically powers fully off, needing a manual
    restart).
    """
    ok, initial_sp = looks_like_arm_image(image)
    if not ok:
        raise OtaError(
            f"{len(image)} B image does not start with a plausible ARM vector table "
            f"(initial SP {initial_sp:#010x}); refusing to flash"
        )

    acks: asyncio.Queue[int] = asyncio.Queue()
    type_future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    def on_notify(frame: bytes) -> None:
        # A probe reply, only relevant until we've resolved the type.
        is_pro = parse_ota_type(frame)
        if is_pro is not None and not type_future.done():
            type_future.set_result(is_pro)
            return
        # Otherwise a flow-control ACK drives the block loop. Everything else
        # (stray version/status notifies during OTA) is ignored.
        op = parse_ack(frame)
        if op is not None:
            acks.put_nowait(op)

    await link.connect()
    on_event("connected")
    await link.subscribe(on_notify)

    if ota_pro is None:
        ota_pro = await _resolve_type(
            link, type_future, alt_header=alt_header, chunk_delay=chunk_delay, timeout=probe_timeout
        )
    block_size = BLOCK_SIZE_PRO if ota_pro else BLOCK_SIZE_STD

    await _settle(link, settle_secs, on_event)

    size = len(image)
    code = check_code(image)
    total_blocks = math.ceil(size / block_size)
    on_event(
        f"flashing {size} B as {total_blocks} x {block_size}-byte blocks "
        f"(checkCode {code:#010x}); fragment spacing {chunk_delay * 1000:.0f} ms"
    )

    async def send_block(index: int) -> None:
        start = index * block_size
        chunk = image[start : start + block_size]
        await link.send_fragmented(
            block_frame(chunk, ota_pro=ota_pro, alt_header=alt_header), GATT_FRAGMENT, chunk_delay
        )

    header = header_frame(version, size, code, name, alt_header=alt_header)

    # Send the header, retrying on silence until the device's first ACK arrives.
    op: Optional[int] = None
    for attempt in range(1, header_retries + 1):
        await link.send_fragmented(header, GATT_FRAGMENT, chunk_delay)
        try:
            op = await asyncio.wait_for(acks.get(), header_timeout)
            break
        except asyncio.TimeoutError:
            on_event(f"no ACK for update header (attempt {attempt}/{header_retries}), resending")
    if op is None:
        raise OtaError("device never acknowledged the update header")

    # The ACK-driven block loop.
    index = -1
    resends = 0
    while True:
        if op == ACK_NEXT:
            index += 1
            if index >= total_blocks:
                # The device asked for a block past the end without sending "done".
                # Keep waiting for the commit/fail rather than reading past the image.
                pass
            else:
                await send_block(index)
                sent = min((index + 1) * block_size, size)
                on_progress(index, total_blocks, sent * 100 // size, resends)
        elif op == ACK_RESEND:
            resends += 1
            if 0 <= index < total_blocks:
                await send_block(index)
        elif op == ACK_RESTART:
            index = 0
            await send_block(index)
        elif op == ACK_DONE:
            on_event(f"device committed the image (resends: {resends})")
            return FlashResult(
                committed=True,
                total_blocks=total_blocks,
                resends=resends,
                block_size=block_size,
                ota_pro=ota_pro,
            )
        elif op == ACK_FAIL:
            raise OtaError(f"device reported OTA failure (op=4) at block {index}")

        try:
            op = await asyncio.wait_for(acks.get(), ack_timeout)
        except asyncio.TimeoutError:
            raise OtaError(
                f"device went silent for {ack_timeout:.0f}s at block {index}/{total_blocks}. "
                "Transfer stalled (nothing was committed; the old firmware is intact)"
            )


# --- CLI ------------------------------------------------------------------


def _build_parser():
    import argparse

    parser = argparse.ArgumentParser(
        prog="neewer-ota",
        description="Flash Neewer firmware over the custom 0x78 OTA block transport. "
        "Stop any daemon holding the light first. Dry-run by default; pass --confirm to write.",
    )
    parser.add_argument("mac", help="target light MAC address (BT mode, powered on)")
    parser.add_argument("--file", required=True, help="firmware .bin to flash")
    parser.add_argument(
        "--version",
        help="version M.M.P written into the header (default: parsed from the filename)",
    )
    parser.add_argument(
        "--name",
        default="TL60 RGB-3",
        help="cosmetic model name in the header (the device ignores it)",
    )
    parser.add_argument("--check", action="store_true", help="dry run: never writes firmware")
    parser.add_argument("--confirm", action="store_true", help="actually flash (required to write)")
    parser.add_argument(
        "--chunk-delay-ms",
        type=float,
        default=DEFAULT_CHUNK_DELAY * 1000,
        help="delay between 20-byte GATT fragments (default 20; raise on a marginal link)",
    )
    parser.add_argument("--settle-secs", type=float, default=20.0, help="pre-flash link hold")
    parser.add_argument(
        "--seconds", type=float, default=20.0, help="scan timeout to find the light"
    )
    parser.add_argument(
        "--ota-pro",
        dest="ota_pro",
        action="store_true",
        default=None,
        help="force 4096-byte blocks (default: probe, fall back to 128-byte)",
    )
    parser.add_argument(
        "--block-128",
        dest="ota_pro",
        action="store_false",
        help="force 128-byte blocks (skip the probe)",
    )
    parser.add_argument(
        "--alt-header",
        action="store_true",
        help="use the 0x85 header prefix (the fixtures that need it)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="skip the ARM vector-table sanity check on the image",
    )
    return parser


def _resolve_version(args) -> tuple[int, int, int]:
    if args.version:
        return parse_version(args.version)
    guessed = version_from_filename(args.file)
    if guessed is None:
        raise SystemExit(
            "neewer-ota: could not parse a version from the filename; pass --version M.M.P"
        )
    return guessed


def main(argv=None) -> int:
    import sys

    parser = _build_parser()
    args = parser.parse_args(argv)

    image = Path(args.file).read_bytes()
    if not args.force:
        ok, initial_sp = looks_like_arm_image(image)
        if not ok:
            print(
                f"neewer-ota: {args.file} does not look like an ARM firmware image "
                f"(initial SP {initial_sp:#010x}); pass --force to override.",
                file=sys.stderr,
            )
            return 1

    version = _resolve_version(args)
    chunk_delay = args.chunk_delay_ms / 1000.0
    link = BleakOtaLink(args.mac, scan_timeout=args.seconds)

    def on_event(message: str) -> None:
        print(f"neewer-ota: {message}", file=sys.stderr)

    def on_progress(block: int, total: int, pct: int, resends: int) -> None:
        # Terse periodic progress: every ~2 % and always the last block.
        if pct % 2 == 0 or block == total - 1:
            print(f"  {pct:3d}%  block {block + 1}/{total}  resends {resends}", file=sys.stderr)

    async def run() -> int:
        try:
            if args.confirm:
                on_event(f"flashing {args.file} v{'.'.join(map(str, version))} to {args.mac}")
                result = await flash(
                    link,
                    image,
                    version=version,
                    name=args.name,
                    ota_pro=args.ota_pro,
                    alt_header=args.alt_header,
                    chunk_delay=chunk_delay,
                    settle_secs=args.settle_secs,
                    on_progress=on_progress,
                    on_event=on_event,
                )
                on_event(
                    f"done. {result.total_blocks} blocks, {result.resends} resends. "
                    "The light commits and usually powers fully off; switch it back on."
                )
                return 0
            # Default (and explicit --check) is the safe dry run.
            if not args.check:
                on_event("dry run (no firmware written). Pass --confirm to actually flash.")
            await check(
                link,
                image,
                alt_header=args.alt_header,
                chunk_delay=chunk_delay,
                settle_secs=args.settle_secs,
                on_event=on_event,
            )
            return 0
        except OtaError as exc:
            print(f"neewer-ota: {exc}", file=sys.stderr)
            return 1
        finally:
            await link.disconnect()

    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
