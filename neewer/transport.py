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

from dataclasses import dataclass
from typing import Any, Callable, Protocol

#: GATT characteristic the lights accept command writes on (write-no-response).
WRITE_UUID = "69400002-b5a3-f393-e0a9-e50e24dcca99"
#: GATT characteristic the lights push status notifications on.
NOTIFY_UUID = "69400003-b5a3-f393-e0a9-e50e24dcca99"


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
    """

    def __init__(self) -> None:
        self._scanner = None

    async def start_scan(self, on_advert: Callable[[Advert], None]) -> None:
        from bleak import BleakScanner

        def _cb(device, adv) -> None:
            # Normalise bleak's (BLEDevice, AdvertisementData) into our Advert. The
            # device object is the handle we hand back to connect().
            name = adv.local_name or device.name or ""
            on_advert(Advert(device.address, name, adv.rssi, device))

        self._scanner = BleakScanner(detection_callback=_cb)
        await self._scanner.start()

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
