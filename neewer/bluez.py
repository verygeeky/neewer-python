"""Best-effort BlueZ housekeeping: clear stale (zombie) links left by a hard kill.

A daemon that dies without a clean shutdown (SIGKILL, OOM, power loss) leaves its
BLE links half-open: BlueZ still reports the device ``Connected``, so the light
stops advertising and a freshly-started daemon's scanner never sees it — the tube
looks "stuck" until someone runs ``bluetoothctl disconnect`` by hand.

This closes that gap. On startup :class:`~neewer.fleet.NeewerCore` asks BlueZ (over
D-Bus, via ``dbus-fast`` — already a transitive dependency of ``bleak`` on Linux)
to ``Disconnect`` any device that is currently ``Connected`` and looks like one of
ours (matching our advertised-name prefixes or a configured MAC). We own nothing
yet at startup, so any such connection is by definition a leftover to reclaim.

Everything here is **best-effort and Linux/BlueZ-only**: on macOS/Windows (or with
no system bus / insufficient permission) it logs and returns, and the daemon just
proceeds to scan as before.
"""
from __future__ import annotations

import contextlib
import logging

log = logging.getLogger("neewer.bluez")

BLUEZ = "org.bluez"
DEVICE_IFACE = "org.bluez.Device1"
OBJECT_MANAGER = "org.freedesktop.DBus.ObjectManager"


def is_ours(address: str, name: str, prefixes, macs) -> bool:
    """Does a BlueZ device look like one of ours (a stale link to reclaim)?

    True if its MAC is one we're configured for, or its name starts with one of
    our claimed prefixes — the same match the scanner uses to adopt a tube.
    """
    if address and address.upper() in macs:
        return True
    return bool(name and name.startswith(tuple(prefixes)))


async def clear_stale_connections(prefixes, macs) -> list[str]:
    """Disconnect BlueZ devices that are ``Connected`` and match ours.

    Returns the MACs it disconnected (empty if none / unavailable). Never raises —
    any failure (no ``dbus-fast``, no system bus, not BlueZ, permission denied) is
    logged at debug and swallowed, because this is an optional self-heal, not a
    requirement for the daemon to run.
    """
    try:
        from dbus_fast import BusType
        from dbus_fast.aio import MessageBus
    except Exception:                       # not on Linux / dbus-fast absent
        return []

    macs = {m.upper() for m in macs}
    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    except Exception as exc:
        log.debug("BlueZ housekeeping skipped (no system bus: %s)", exc)
        return []

    cleared: list[str] = []
    try:
        root = bus.get_proxy_object(BLUEZ, "/", await bus.introspect(BLUEZ, "/"))
        manager = root.get_interface(OBJECT_MANAGER)
        for path, ifaces in (await manager.call_get_managed_objects()).items():
            dev = ifaces.get(DEVICE_IFACE)
            if not dev:
                continue
            connected = dev.get("Connected")
            if not (connected and connected.value):
                continue
            address = dev["Address"].value if "Address" in dev else ""
            name = dev["Name"].value if "Name" in dev else ""
            if not is_ours(address, name, prefixes, macs):
                continue
            try:
                node = bus.get_proxy_object(BLUEZ, path, await bus.introspect(BLUEZ, path))
                await node.get_interface(DEVICE_IFACE).call_disconnect()
                cleared.append(address.upper())
                log.info("cleared stale BlueZ link to %s (%s)", address, name or "?")
            except Exception as exc:
                log.debug("could not disconnect %s: %s", address, exc)
    except Exception as exc:
        log.debug("BlueZ housekeeping error: %s", exc)
    finally:
        with contextlib.suppress(Exception):
            bus.disconnect()
    return cleared
