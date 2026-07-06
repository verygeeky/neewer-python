"""Hardware-free test doubles: a public :class:`MockTransport` for downstream suites.

Code built on :class:`neewer.Fleet` needs a way to test without a radio or a light
in range. This module provides one — a complete, importable implementation of the
:class:`neewer.transport.Transport` protocol backed by in-memory virtual tubes::

    from neewer import Fleet
    from neewer.testing import MockTransport, MockTube

    tube = MockTube(mac="AA:BB:CC:DD:EE:01", name="NW-20240047&00000000")
    fleet = Fleet(transport=MockTransport(tubes=[tube]))

The mock mirrors real hardware behaviour where it matters to ``Fleet``:

* **Scanning** surfaces an :class:`~neewer.transport.Advert` per tube — but only
  for tubes without a live link, because a connected light stops advertising. A
  dropped tube resumes advertising, which is exactly the signal the reconnect
  supervisor waits for.
* **Writes** are parsed with the real protocol constants and update per-tube
  virtual state (``power`` / ``hsi`` / ``cct``), plus a full write log. Frames
  the mock doesn't model are recorded in ``unknown``, never raised on.
* **Queries** (battery ``0x95`` / state ``0x8E`` / version ``0x9E``, all by-MAC)
  elicit well-formed reply frames — correct layout and checksum — on the notify
  path, so ``Fleet``'s auto-query-on-connect populates telemetry just like a
  real light would.
* **Failure injection**: :meth:`MockTube.drop` kills a link (fires the
  ``on_disconnect`` callback and resumes advertising), ``write_latency`` slows
  every write (set it above ``Fleet``'s write timeout to simulate a half-open
  link), and ``fail_writes`` makes writes raise :class:`MockWriteError`.

Everything here is stdlib-only; importing this module never touches ``bleak``.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .protocol import frames, replies
from .transport import Advert

__all__ = [
    "MockTransport",
    "MockTube",
    "MockWriteError",
    "battery_reply",
    "state_reply",
    "version_reply",
]


class MockWriteError(ConnectionError):
    """Raised by :meth:`MockTransport.write` on injected failure or a dead link."""


# --- reply-frame builders ---------------------------------------------------
# Replies share the command wire shape: [0x78, reply-code, len, payload..., ck]
# with ck = sum(preceding bytes) & 0xFF, which frames.checksum() computes. Each
# builder produces a frame that neewer.protocol.replies.parse() decodes.


def battery_reply(mac: str, percent: int) -> bytes:
    """A by-MAC battery reply (``0x05``): ``78 05 07 <MAC6> <pct> ck``.

    A ``percent`` above 100 is passed through as-is: some fixtures report the
    ``0xF0`` external-power sentinel there when mains-powered, which the decoder
    maps to ``power_source: "external"`` instead of a percentage.
    """
    mac6 = frames.mac_bytes(mac)
    # len 0x07 = 6 MAC bytes + 1 battery byte.
    return frames.checksum([replies.PREFIX, replies.R_BATTERY, 0x07, *mac6, int(percent) & 0xFF])


def state_reply(mac: str, mode: int = 1, on: bool = True) -> bytes:
    """A by-MAC state reply (``0x04``): ``78 04 08 <MAC6> <mode> <power> ck``.

    The power byte is ``0x01`` for on and ``0x02`` for off, mirroring the
    ``POWER_ON``/``POWER_OFF`` command literals (the decoder reads any non-1 as
    off). ``mode`` is the fixture's operating-mode byte, echoed verbatim.
    """
    mac6 = frames.mac_bytes(mac)
    power_byte = frames.POWER_ON if on else frames.POWER_OFF
    # len 0x08 = 6 MAC bytes + mode + power.
    return frames.checksum(
        [replies.PREFIX, replies.R_STATE_MAC, 0x08, *mac6, int(mode) & 0xFF, power_byte])


#: Two header bytes that precede the version triplet in a real ``0x08`` reply
#: (observed as ``01 0a`` on a TL120C-2). The decoder skips them; they are
#: reproduced so mock replies keep the real field offsets.
_VERSION_REPLY_HEADER = (0x01, 0x0A)


def version_reply(mac: str, version: str) -> bytes:
    """A by-MAC version reply (``0x08``): ``78 08 0b <MAC6> 01 0a <maj min pat> ck``.

    ``version`` must be a dotted ``major.minor.patch`` string of integers (e.g.
    ``"2.0.5"``); anything else raises ``ValueError``. The decoder reads the
    triplet at fixed offsets 11..13, after the MAC and the two header bytes.
    """
    parts = version.split(".")
    if len(parts) != 3:
        raise ValueError(f"version must be 'major.minor.patch', got {version!r}")
    try:
        major, minor, patch = (int(part) for part in parts)
    except ValueError as exc:
        raise ValueError(f"non-integer component in version {version!r}") from exc
    mac6 = frames.mac_bytes(mac)
    payload = [*mac6, *_VERSION_REPLY_HEADER, major & 0xFF, minor & 0xFF, patch & 0xFF]
    return frames.checksum([replies.PREFIX, replies.R_VERSION_MAC, len(payload), *payload])


# --- the virtual light ------------------------------------------------------

#: Query opcodes the mock answers (each elicits a reply on the notify path).
_QUERY_OPS = (frames.OP_BATTERY, frames.OP_STATE_MAC, frames.OP_VERSION_MAC)


@dataclass
class MockTube:
    """One simulated light behind a :class:`MockTransport`.

    Constructor fields configure the fixture's identity and what its query
    replies report; the ``init=False`` fields are runtime state the mock
    maintains from the frames written to it. Assert against them directly::

        await fleet.set_hsi("all", 240, 100, 80)
        assert tube.hsi == (240, 100, 80)
    """

    #: MAC address, e.g. ``"AA:BB:CC:DD:EE:01"`` (normalised to upper case).
    mac: str
    #: Advertised BLE name. The default is the ``NW-<batch>&<suffix>`` form a
    #: TL120C-2 advertises (batch ``NW-20240047``); use ``NW-20240012&00000000``
    #: for a TL90C. ``Fleet`` decodes the model from this at discovery time.
    name: str = "NW-20240047&00000000"
    #: Advertised signal strength in dBm.
    rssi: int = -50
    #: Firmware version the ``0x9E`` query reply reports.
    version: str = "2.0.5"
    #: Battery percentage the ``0x95`` query reply reports (values above 100,
    #: e.g. ``0xF0``, read back as the external-power sentinel).
    battery: int = 100
    #: Operating-mode byte the ``0x8E`` state reply echoes.
    mode: int = 1
    #: Power state, ``"on"`` or ``"off"``. Starts on (a light that was just
    #: switched on); updated by ``0x81`` power frames and reported by ``0x8E``.
    power: str = "on"

    # -- runtime state maintained from written frames -------------------------
    #: Last HSI colour applied, as ``(hue, sat, bri)``.
    hsi: tuple[int, int, int] | None = field(default=None, init=False)
    #: Last CCT white applied, as ``(bri, temp, gm)``.
    cct: tuple[int, int, int] | None = field(default=None, init=False)
    #: The most recent frame written, verbatim.
    last_frame: bytes | None = field(default=None, init=False)
    #: Every frame ever written, in order.
    writes: list[bytes] = field(default_factory=list, init=False)
    #: Frames whose opcode the mock does not model (recorded, never an error).
    unknown: list[bytes] = field(default_factory=list, init=False)
    #: Whether a central currently holds this tube's link.
    link_up: bool = field(default=False, init=False)

    _transport: "MockTransport | None" = field(default=None, init=False, repr=False)
    _on_disconnect: Callable[[], None] | None = field(default=None, init=False, repr=False)
    _on_notify: Callable[[bytes], None] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.mac = self.mac.upper()

    # -- failure injection -----------------------------------------------------
    def drop(self) -> None:
        """Simulate the BLE link dying out from under the central.

        Fires the ``on_disconnect`` callback registered at connect time (so a
        ``Fleet`` marks the tube down and its supervisor starts reconnecting)
        and resumes advertising, exactly as a real light does once its link is
        gone. A no-op if there is no link up.
        """
        if not self.link_up:
            return
        self.link_up = False
        callback = self._on_disconnect
        self._on_disconnect = None
        self._on_notify = None
        if callback is not None:
            callback()
        if self._transport is not None:
            self._transport._advertise(self)    # a free light advertises again

    # -- frame handling ----------------------------------------------------------
    def _apply(self, frame: bytes) -> None:
        """Record one written frame and apply the state change it encodes."""
        self.writes.append(frame)
        self.last_frame = frame
        if len(frame) < 3 or frame[0] != frames.PREFIX:
            self.unknown.append(frame)          # not a 0x78 command frame at all
            return
        op = frame[1]                           # the opcode (tag) byte
        try:
            if op == frames.OP_POWER:
                # 78 81 01 <01 on | 02 off> ck
                self.power = "on" if frame[3] == frames.POWER_ON else "off"
            elif op == frames.OP_HSI:
                # 78 86 04 <hueLo hueHi> <sat> <bri> ck — hue is little-endian 16-bit
                hue = frame[3] | (frame[4] << 8)
                self.hsi = (hue, frame[5], frame[6])
            elif op == frames.OP_CCT:
                # 78 87 04 <bri> <temp> <gm> <curve> ck — temp in hundreds of Kelvin
                self.cct = (frame[3], frame[4], frame[5])
            elif op in _QUERY_OPS:
                self._answer_query(op, frame)
            else:
                self.unknown.append(frame)      # unmodelled opcode: recorded, not an error
        except IndexError:
            self.unknown.append(frame)          # truncated frame: recorded, not an error

    def _answer_query(self, op: int, frame: bytes) -> None:
        """Push the reply a query frame elicits, if it is addressed to this tube."""
        # Every query the mock answers is MAC-addressed with the 6 MAC bytes at
        # offsets 3..8; a real light stays silent on a query for another MAC.
        if bytes(frame[3:9]) != frames.mac_bytes(self.mac):
            return
        if op == frames.OP_BATTERY:
            self._push(battery_reply(self.mac, self.battery))
        elif op == frames.OP_STATE_MAC:
            self._push(state_reply(self.mac, self.mode, self.power == "on"))
        else:                                   # frames.OP_VERSION_MAC
            self._push(version_reply(self.mac, self.version))

    def _push(self, reply: bytes) -> None:
        """Deliver one notification frame through the subscribed callback.

        Scheduled with ``call_soon`` so the reply lands *after* the write that
        elicited it returns — the same ordering as a real notification. Falls
        back to a synchronous call when no event loop is running (plain unit
        tests poking the tube directly).
        """
        callback = self._on_notify
        if callback is None or not self.link_up:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            callback(reply)
        else:
            loop.call_soon(callback, reply)


# --- the transport ------------------------------------------------------------


class MockTransport:
    """A :class:`neewer.transport.Transport` backed by :class:`MockTube` objects.

    Inject it into a ``Fleet`` (``Fleet(transport=MockTransport(tubes=[...]))``)
    or drive it directly in unit tests. The opaque handles the protocol talks
    about are the tubes themselves: an :class:`~neewer.transport.Advert` carries
    the ``MockTube`` as its ``handle``, and :meth:`connect` returns that same
    tube as the client.

    Args:
        tubes: the virtual lights this radio can see.
        write_latency: seconds every write sleeps before completing. Raise it
            above ``Fleet``'s ``write_timeout`` to simulate a half-open link.
        fail_writes: when true, every write raises :class:`MockWriteError`.
            Flip it at runtime to fail just a window of writes.
    """

    def __init__(self, tubes: Iterable[MockTube] = (),
                 write_latency: float = 0.0, fail_writes: bool = False) -> None:
        #: The virtual lights, keyed by upper-cased MAC.
        self.tubes: dict[str, MockTube] = {}
        self.write_latency = write_latency
        self.fail_writes = fail_writes
        self.scanning = False
        self._on_advert: Callable[[Advert], None] | None = None
        for tube in tubes:
            self.add_tube(tube)

    def add_tube(self, tube: MockTube) -> None:
        """Register another virtual light; it advertises at once if a scan is on."""
        tube._transport = self
        self.tubes[tube.mac] = tube
        if not tube.link_up:
            self._advertise(tube)

    # -- Transport protocol ------------------------------------------------------
    async def start_scan(self, on_advert: Callable[[Advert], None]) -> None:
        """Begin "scanning": every tube without a live link advertises once.

        Adverts are delivered synchronously, before this returns, so a test can
        assert discovery immediately. Tubes freed later (:meth:`MockTube.drop`,
        :meth:`disconnect`) re-advertise on their own, mirroring real lights.
        """
        self._on_advert = on_advert
        self.scanning = True
        for tube in self.tubes.values():
            if not tube.link_up:                # a connected light stops advertising
                self._advertise(tube)

    async def stop_scan(self) -> None:
        """Stop scanning (idempotent); no further adverts are delivered."""
        self.scanning = False
        self._on_advert = None

    async def connect(self, handle: Any, on_disconnect: Callable[[], None]) -> Any:
        """Take the tube's link and return it as the connected client.

        ``handle`` is the ``MockTube`` a previous advert carried (a MAC string
        is also accepted for direct use). Connecting to a tube whose link is
        already held raises ``ConnectionError`` — one central at a time, like
        the hardware.
        """
        tube = self._tube_for(handle)
        if tube.link_up:
            raise ConnectionError(f"{tube.mac} is already held by another central")
        tube.link_up = True
        tube._on_disconnect = on_disconnect
        return tube

    def is_connected(self, client: Any) -> bool:
        """Whether the client's (tube's) link is currently up."""
        tube: MockTube = client
        return tube.link_up

    async def subscribe(self, client: Any, on_notify: Callable[[bytes], None]) -> None:
        """Register the notification callback query replies are pushed through."""
        tube: MockTube = client
        tube._on_notify = on_notify

    async def write(self, client: Any, data: bytes) -> None:
        """Write one command frame to the tube (after latency / failure hooks)."""
        if self.write_latency > 0:
            await asyncio.sleep(self.write_latency)
        if self.fail_writes:
            raise MockWriteError("injected write failure (fail_writes is set)")
        tube: MockTube = client
        if not tube.link_up:
            raise MockWriteError(f"write to {tube.mac} with no link up")
        tube._apply(bytes(data))

    async def disconnect(self, client: Any) -> None:
        """Release the tube's link (best-effort, idempotent).

        Mirrors the real backend: the disconnect callback fires for a deliberate
        disconnect too, and the freed tube resumes advertising.
        """
        tube: MockTube = client
        tube.drop()

    # -- internals -----------------------------------------------------------------
    def _tube_for(self, handle: Any) -> MockTube:
        """Resolve a connect handle (a :class:`MockTube` or a MAC string) to a tube."""
        if isinstance(handle, MockTube):
            return handle
        mac = str(handle).upper()
        try:
            return self.tubes[mac]
        except KeyError:
            raise ConnectionError(f"no such mock tube: {handle!r}") from None

    def _advertise(self, tube: MockTube) -> None:
        """Surface one advert for ``tube`` if a scan is running (else a no-op)."""
        if not self.scanning or self._on_advert is None:
            return
        self._on_advert(Advert(tube.mac, tube.name, tube.rssi, tube))
