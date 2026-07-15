"""The transport seam under :class:`~neewer.fleet.Fleet` — the BLE radio, abstracted.

``Fleet`` owns the roster, addressing, the reconnect supervisor and dispatch, but
it does **not** own the radio. A :class:`Transport` owns the radio: scan for
advertisements, hold a link to a device, write frames, deliver notifications, and
disconnect. Splitting this out means:

* the reconnect supervisor can be exercised against a **fake transport** with no
  hardware (see ``tests/test_fleet_async.py``), and
* an alternative backend (an ESP32/UART bridge, a mesh gateway) can drive the same
  ``Fleet`` by implementing this Protocol — the "bring your own transport" claim in
  the package docstring is now *code*, not prose.

The default :class:`BleakTransport` wraps ``bleak``. It imports ``bleak`` lazily,
inside the methods that actually talk to the radio, so importing *this* module (the
Protocol, the :class:`Advert` value type, and any fake transport) never pulls in a
BLE stack.

Handles are opaque: ``scan`` surfaces an :class:`Advert` whose ``handle`` the same
transport later accepts in :meth:`Transport.connect`, and ``connect`` returns a
``client`` the same transport accepts in ``write``/``subscribe``/``disconnect``.
``Fleet`` never inspects either — it only shuttles them back to the transport.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Protocol

log = logging.getLogger("neewer.transport")

#: GATT characteristic the lights accept command writes on (write-no-response).
WRITE_UUID = "69400002-b5a3-f393-e0a9-e50e24dcca99"
#: GATT characteristic the lights push status notifications on.
NOTIFY_UUID = "69400003-b5a3-f393-e0a9-e50e24dcca99"

#: AD structure type for the Complete Local Name (Bluetooth assigned numbers).
#: A tube carries its ``NW-...`` name in this field of the primary advertising
#: PDU, so a passive scan filtered on this type still sees everything identity
#: needs. Kept as a plain int so building the match spec needs no bleak import.
_COMPLETE_LOCAL_NAME = 0x09


def name_or_patterns(prefixes) -> list[tuple[int, int, bytes]]:
    """Passive-scan match patterns for the tube-name prefixes.

    Returns ``(start_position, ad_type, content)`` tuples. Each tube's Complete
    Local Name begins with one of the claimed prefixes (``NW-``, ``NWR``, ...), so
    a BlueZ advertisement monitor built from these wakes the host only for our
    lights. Pure (no bleak import) so it is unit-testable without a radio.
    """
    return [(0, _COMPLETE_LOCAL_NAME, prefix.encode("ascii")) for prefix in prefixes]


@dataclass(frozen=True)
class Advert:
    """One advertisement a transport surfaced.

    ``address`` and ``name`` are what :class:`~neewer.fleet.Fleet` matches/roster on;
    ``rssi`` is last-seen signal (dBm, or ``None`` on a backend that omits it); and
    ``handle`` is the opaque device object the *same* transport accepts in
    :meth:`Transport.connect`.
    """

    address: str
    name: str
    rssi: int | None
    handle: Any


class Transport(Protocol):
    """The radio operations :class:`~neewer.fleet.Fleet` needs. Backend-agnostic."""

    async def start_scan(self, on_advert: Callable[[Advert], None]) -> None:
        """Begin scanning; call ``on_advert(Advert)`` for every advertisement seen."""
        ...

    async def stop_scan(self) -> None:
        """Stop scanning (idempotent / best-effort)."""
        ...

    async def connect(self, handle: Any, on_disconnect: Callable[[], None]) -> Any:
        """Connect to ``handle`` and return an opaque client.

        ``on_disconnect`` is invoked (with no args) if the link later drops. May
        raise, or return a client that is already not-connected (a drop during
        connect) — the caller re-checks :meth:`is_connected`.
        """
        ...

    def is_connected(self, client: Any) -> bool:
        """Whether ``client``'s link is currently up."""
        ...

    async def subscribe(self, client: Any, on_notify: Callable[[bytes], None]) -> None:
        """Subscribe to status notifications; ``on_notify(bytes)`` per frame."""
        ...

    async def write(self, client: Any, data: bytes) -> None:
        """Write one command frame to ``client`` (write-no-response)."""
        ...

    async def disconnect(self, client: Any) -> None:
        """Disconnect ``client`` (best-effort)."""
        ...


class BleakTransport:
    """The default :class:`Transport` — one continuous ``bleak`` scanner + clients.

    ``bleak`` is imported lazily inside each method so this class can be defined and
    referenced without a BLE stack present; only *using* it needs ``bleak``.

    ``passive_scan`` requests a passive scan (no scan requests, less airtime and
    radio energy). The tube name is in the primary advertising PDU, so identity is
    unaffected; ``prefixes`` seed the BlueZ advertisement-monitor filter. Passive
    mode needs ``bluetoothd --experimental`` on Linux, so if it can't start we log
    a warning and fall back to active scanning rather than a fleet that silently
    discovers nothing.
    """

    def __init__(self, passive_scan: bool = False, prefixes=()) -> None:
        self._scanner = None
        self._passive = passive_scan
        self._prefixes = tuple(prefixes)

    async def start_scan(self, on_advert: Callable[[Advert], None]) -> None:
        from bleak import BleakScanner

        def _cb(device, adv) -> None:
            # Normalise bleak's (BLEDevice, AdvertisementData) into our Advert. The
            # device object is the handle we hand back to connect().
            name = adv.local_name or device.name or ""
            on_advert(Advert(device.address, name, adv.rssi, device))

        if self._passive and await self._start_passive(_cb):
            return
        self._scanner = BleakScanner(detection_callback=_cb)
        await self._scanner.start()

    async def _start_passive(self, cb: Callable) -> bool:
        """Try to start a passive scan; return ``True`` on success, ``False`` to fall back.

        Any failure to set up passive mode (a stack without the advertisement-monitor
        API, an older bleak, ``--experimental`` disabled) degrades to active scanning
        with a warning — the mandate is to never leave the fleet unable to discover.
        """
        if not self._prefixes:
            log.warning("passive scan requested without name prefixes; using active scan")
            return False
        try:
            from bleak import BleakScanner
            from bleak.args.bluez import BlueZScannerArgs, OrPattern
            from bleak.assigned_numbers import AdvertisementDataType

            or_patterns = [
                OrPattern(start, AdvertisementDataType(ad_type), content)
                for start, ad_type, content in name_or_patterns(self._prefixes)
            ]
            scanner = BleakScanner(
                detection_callback=cb,
                scanning_mode="passive",
                bluez=BlueZScannerArgs(or_patterns=or_patterns),
            )
            await scanner.start()
        except Exception as exc:
            # Broad by design: whatever went wrong, active scanning still works, and
            # a degraded-but-working fleet beats a clean failure that finds nothing.
            log.warning(
                "passive scan unavailable (%s); falling back to active scan. "
                "On Linux, passive mode needs bluetoothd --experimental.",
                exc,
            )
            return False
        self._scanner = scanner
        return True

    async def stop_scan(self) -> None:
        if self._scanner is not None:
            try:
                await self._scanner.stop()
            except Exception:
                pass
            self._scanner = None

    async def connect(self, handle: Any, on_disconnect: Callable[[], None]) -> Any:
        from bleak import BleakClient

        # bleak calls the disconnected_callback with the client; Fleet only wants the
        # "it dropped" signal, so drop the arg.
        client = BleakClient(handle, disconnected_callback=lambda _c: on_disconnect())
        await client.connect()
        return client

    def is_connected(self, client: Any) -> bool:
        return bool(client.is_connected)

    async def subscribe(self, client: Any, on_notify: Callable[[bytes], None]) -> None:
        await client.start_notify(NOTIFY_UUID, lambda _ch, data: on_notify(bytes(data)))

    async def write(self, client: Any, data: bytes) -> None:
        await client.write_gatt_char(WRITE_UUID, data, response=False)

    async def disconnect(self, client: Any) -> None:
        await client.disconnect()
