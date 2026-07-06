"""NeewerCore — the component that owns the Bluetooth.

Responsibilities:

* **Discovery** — periodically scan for tubes whose advertised name matches one
  of the configured prefixes and register each one.
* **Connection ownership** — hold a persistent BLE link to every reachable tube
  via a per-tube supervisor task that reconnects automatically.
* **Addressing** — translate a target word (``all`` / ``t<N>`` / a MAC) into the
  set of tubes to act on.
* **Dispatch** — the ``dispatch(line)`` API that every I/O module calls; it parses
  a command line, builds the frame, and writes it.
* **Effects** — start/stop the in-process animations in :mod:`.effects`.

Reachability caveat: a tube obeys Bluetooth only when its physical switch is in
*Bluetooth* mode (advertising, GATT-accessible). A tube flipped to 2.4 G mode, or
already held by another central, simply won't connect — the supervisor keeps
retrying so it joins the moment it becomes free.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from . import bluez, effects
from .devices import DeviceBook
from .errors import UnknownEffect, UnknownTarget, Unsupported
from .protocol import commands, frames, models, replies
from .transport import Advert, BleakTransport, Transport

log = logging.getLogger("neewer.fleet")

#: Advertised-name prefixes to claim — the prefixes Neewer fixtures are known
#: to advertise (the official app filters on the same set).
DEFAULT_PREFIXES = ("NW-", "NWR", "NEEWER", "SL", "DL", "NH")

# Tuning for the discovery / connection loops, in seconds.
_INITIAL_SETTLE = 4.0        # let the shared scanner collect adverts before start() returns
_STALE_SETTLE = 1.5          # after clearing a zombie link, let the light re-advertise
_ATT_PAYLOAD = 20            # MTU(23) - 3: max bytes per write-no-response ATT write
_PIXEL_SETTLE = 0.05         # gap between the pixel params frame and the palette frame
_SUPERVISE_CONNECTED = 5.0   # poll interval while a tube is connected
_SUPERVISE_RETRY_BASE = 4.0  # first reconnect delay while a tube is disconnected
_SUPERVISE_RETRY_MAX = 60.0  # cap on the exponential reconnect backoff
_CONNECT_TIMEOUT = 15.0      # deadline for one connect attempt (#7): a hung BlueZ
                             # connect otherwise stalls that tube's supervisor forever
_PROBE_MISSES = 3            # consecutive failed liveness probes before we drop (#47)


def _retry_delay(backoff: float) -> float:
    """A jittered reconnect delay in ``[backoff, 2*backoff)``, capped at the max.

    The jitter spreads a fleet's reconnect attempts out so they don't hammer
    BlueZ's single scan session in lock-step after a mass drop (#6).
    """
    return min(backoff + random.uniform(0, backoff), _SUPERVISE_RETRY_MAX)


def _next_backoff(backoff: float) -> float:
    """Grow the reconnect backoff: double it, capped at :data:`_SUPERVISE_RETRY_MAX`."""
    return min(backoff * 2, _SUPERVISE_RETRY_MAX)


@dataclass
class TubeState:
    """A tube's last-known state — a typed replacement for the old free-form dict.

    Fields are populated from decoded status replies (:func:`neewer.protocol.replies.parse`)
    and the last command sent. All are optional: a fresh tube has none until it
    answers a ``query`` or takes a command. :meth:`as_dict` renders the populated
    fields as the JSON shape status modules already consume (SSE / MQTT / HA), so
    typing the state didn't change the wire snapshot.
    """

    #: The last command line applied to this tube (for optimistic UIs / MQTT).
    last: str | None = None
    #: Power as reported by the light: ``"on"`` / ``"off"``.
    power: str | None = None
    #: Battery charge 0..100 (only meaningful on battery power).
    battery: int | None = None
    #: Raw battery byte before the external-power sentinel is stripped.
    battery_raw: int | None = None
    #: ``"external"`` when mains-powered, else ``"battery"`` context.
    power_source: str | None = None
    #: Firmware version string, e.g. ``"2.0.5"``.
    version: str | None = None
    #: Reported temperature in °C.
    temp_c: int | None = None
    #: Reported operating mode byte.
    mode: int | None = None
    #: MAC echoed by a MAC-addressed reply.
    mac: str | None = None
    #: Hex of a frame we couldn't decode (diagnostics).
    raw: str | None = None
    #: Monotonic timestamp (``time.monotonic()``) of the last notify from this
    #: tube — the delivery-feedback signal for the write governor's RTT canary
    #: (#46). Set by the fleet, not decoded from the reply, so it is deliberately
    #: NOT in ``_REPLY_FIELDS``/``as_dict`` (a monotonic float means nothing to a
    #: status consumer and would churn the wire snapshot on every notify).
    last_reply_at: float | None = None
    #: Any reply key we don't have a typed field for yet (forward-compatible).
    extra: dict = field(default_factory=dict)

    #: Typed telemetry fields a decoded reply may set (``last`` is command-set).
    _REPLY_FIELDS = ("power", "battery", "battery_raw", "power_source", "version",
                     "temp_c", "mode", "mac", "raw")

    def update_from_reply(self, parsed: dict) -> None:
        """Merge a decoded status reply into the state (unknown keys go to ``extra``)."""
        for key, value in parsed.items():
            if key in self._REPLY_FIELDS:
                setattr(self, key, value)
            else:
                self.extra[key] = value

    def as_dict(self) -> dict:
        """Render the populated fields as the snapshot dict status modules consume."""
        out: dict = {}
        if self.last is not None:
            out["last"] = self.last
        for key in self._REPLY_FIELDS:
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        out.update(self.extra)
        return out


class Tube:
    """One physical light: its identity, transport client, and last-known state."""

    def __init__(self, mac: str, name: str = "", position: int | None = None,
                 model: str | None = None):
        self.mac = mac
        self.name = name
        #: 1-based physical position used to order tubes for flows (or ``None``).
        self.position = position
        #: Fixture model (e.g. ``"TL120C"``) for capability gating, or ``None`` if
        #: unknown. Set from config, else inferred from the firmware version.
        self.model = model
        #: Opaque connected client from the transport (``None`` while down). Fleet
        #: never inspects it — it hands it back to the transport to write/disconnect.
        self.client = None
        self.connected = False
        #: Last advertised signal strength in dBm (nearer 0 = stronger), or ``None``
        #: until first seen. Refreshed on every advertisement by the scanner; a
        #: *connected* tube stops advertising, so this holds the value from the last
        #: discovery/rescan and can be stale while the link is up.
        self.rssi: int | None = None
        #: Typed last-known state (last command sent, decoded telemetry).
        self.state = TubeState()
        #: Consecutive liveness probes this tube failed to answer (#47). Reset by
        #: any successful probe; at :data:`_PROBE_MISSES` the link is declared
        #: half-open and dropped so the supervisor reconnects it.
        self.probe_misses = 0


class Fleet:
    """Owns discovery, connections, addressing, dispatch and effects."""

    def __init__(self, prefixes=DEFAULT_PREFIXES, positions=None,
                 rescan_interval: float = 20.0, book: DeviceBook | None = None,
                 transport: Transport | None = None, write_timeout: float = 0.5,
                 liveness_interval: float = 30.0):
        self.prefixes = tuple(prefixes)
        #: The BLE seam. Defaults to the bleak-backed transport; a test or an
        #: alternative backend injects its own (see :mod:`neewer.transport`).
        self.transport: Transport = transport or BleakTransport()
        #: Per-write deadline (seconds). A write-without-response to a *healthy* link
        #: returns in milliseconds; one to a **half-open** link (BlueZ still reports
        #: "connected" but the ACL is dead) blocks for seconds. Unbounded, that single
        #: hung write stalls every caller — the DMX send loop gathers all tubes, so one
        #: dead tube backs the whole fleet up by minutes. So we cap it and treat a
        #: timeout as a drop (see :meth:`write`).
        self._write_timeout = write_timeout
        #: Liveness-probe staleness threshold (seconds); ``0`` disables (#47). A
        #: half-open link can pass every *write* (write-without-response "succeeds"
        #: at the D-Bus level into a dead ACL — observed live: 5 of 6 tubes dark
        #: while the daemon wrote happily to all 6), so writes prove nothing; only
        #: a reply does. When a connected tube hasn't been heard from for this
        #: long, the supervisor sends a canary query; :data:`_PROBE_MISSES`
        #: consecutive silent probes = half-open, drop and reconnect.
        self.liveness_interval = liveness_interval
        #: Extra whole-line command verbs a *consumer* registers (see
        #: :meth:`register_verb`). The core grammar knows nothing about these — it's
        #: how a daemon layers its own policy verbs (e.g. presets) on top of the
        #: library's command line without the library owning that policy.
        self.verbs: dict[str, Callable] = {}
        #: Shared aliases/groups (from ~/.config/neewer/devices.toml). Empty book
        #: if none configured, so resolve() keeps its original all/t<N>/MAC behaviour.
        self.book = book or DeviceBook()
        # Position map is keyed by upper-cased MAC for case-insensitive lookup.
        # The device book's positions form the base; an explicit `positions` arg
        # (the daemon's [core.positions]) overrides per-MAC.
        self.positions = dict(self.book.positions)
        self.positions.update({k.upper(): v for k, v in (positions or {}).items()})
        # Kept for config/back-compat; with the shared scanner there is no longer
        # a periodic rescan loop (discovery is continuous via detection_callback).
        self.rescan_interval = rescan_interval
        self.tubes: dict[str, Tube] = {}
        #: Latest advertised transport handle per MAC, refreshed by the scan callback.
        #: The supervisor connects to the cached handle instead of re-scanning.
        self._devices: dict = {}
        self._supervisors: dict[str, asyncio.Task] = {}
        self._scanning = False
        self._effect_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._shutdown = False
        #: Change-event subscribers, invoked (no args) whenever observable tube state
        #: changes — connect/disconnect, a status notify, or a command. Lets the SSE
        #: stream and MQTT push on change instead of polling. See :meth:`subscribe`.
        self._subscribers: set = set()

    # ---- change events ---------------------------------------------------
    def subscribe(self, callback) -> Callable[[], None]:
        """Register ``callback`` (called with no args) on any tube-state change.

        Returns an unsubscribe function. The callback runs on the event loop and
        should be cheap — set an ``asyncio.Event``, enqueue a publish — not do work
        inline. Errors in a callback are swallowed so one bad subscriber can't break
        the others or the caller that triggered the change.
        """
        self._subscribers.add(callback)
        return lambda: self._subscribers.discard(callback)

    def _emit(self) -> None:
        """Notify every subscriber that observable state changed (best-effort)."""
        for callback in list(self._subscribers):
            try:
                callback()
            except Exception:
                log.debug("state subscriber errored", exc_info=True)

    # ---- consumer-registered verbs --------------------------------------
    def register_verb(self, name: str, handler: Callable) -> None:
        """Register a whole-line command verb the core grammar doesn't define.

        ``handler`` is an ``async (fleet, args: list[str]) -> str`` callable; when a
        dispatched line's first word is ``name`` the grammar hands it the remaining
        words verbatim (before any target/action parsing). This is the seam a daemon
        uses to add its own policy verbs — presets, scenes-from-config, macros —
        without the library taking an opinion on that policy. See :func:`neewer.grammar.dispatch`.
        """
        self.verbs[name] = handler

    # ---- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        """Start ONE continuous scan and let it discover/track every tube.

        A single core-owned scan replaces both the old periodic ``discover()`` loop
        and the per-tube ``find_device_by_address()`` calls — they all contended for
        BlueZ's single scan session, causing missed adverts and slow reconnects. Now
        one scan feeds a handle cache (via the transport); supervisors connect from it.
        """
        log.info("core starting; scanning for tubes...")
        # Self-heal: a previous hard kill can leave BlueZ holding a zombie link to
        # one of our tubes, so the light won't advertise and we'd never find it.
        # Disconnect any such stale link first (best-effort, Linux/BlueZ only).
        cleared = await bluez.clear_stale_connections(self.prefixes, self.positions.keys())
        if cleared:
            log.info("cleared %d stale BlueZ link(s) from a prior run; letting tubes re-advertise",
                     len(cleared))
            await asyncio.sleep(_STALE_SETTLE)      # give the light time to advertise again
        await self.transport.start_scan(self._on_advert)
        self._scanning = True
        # Give initial adverts a moment to arrive so callers that immediately
        # address a tube find it already registered.
        await asyncio.sleep(_INITIAL_SETTLE)

    async def __aenter__(self) -> "Fleet":
        """Context-manager entry: start scanning/connecting and return self.

        Lets a throwaway script hold the whole fleet for the duration of a
        ``with`` block and tear it down cleanly on exit -- the 4-5 line
        hello-world::

            async with Fleet() as fleet:
                await fleet.dispatch("all hsi 240 100 100")
        """
        await self.start()
        return self

    async def __aexit__(self, *exc) -> bool:
        await self.stop()
        return False

    def _on_advert(self, advert: Advert) -> None:
        """Scan callback: cache matching handles and register new tubes.

        Runs on the event loop, synchronously, for every advertisement the transport
        surfaces. A connected light stops advertising, so a tube re-appearing here
        after a drop is exactly the signal that it is reconnectable again.
        """
        if not advert.name.startswith(self.prefixes):
            return
        mac = advert.address.upper()
        self._devices[mac] = advert.handle
        if mac not in self.tubes:
            # Model precedence: device-book [models] config wins; else decode the
            # advertised name via the app's own decoder (authoritative, no query
            # needed); else it stays None and the version reply infers it later.
            model = self.book.model_for(mac) or models.name_model(advert.name, mac)
            tube = Tube(mac, advert.name, self.positions.get(mac), model)
            self.tubes[mac] = tube
            self._supervisors[mac] = asyncio.create_task(self._supervise(mac))
            log.info("discovered %s (%s) pos=%s", mac, advert.name, tube.position)
        # Refresh last-seen signal on every advert (dBm; None on backends that omit it).
        self.tubes[mac].rssi = advert.rssi

    async def _supervise(self, mac: str) -> None:
        """Keep one tube connected until shutdown, reconnecting from the cache.

        Whenever the tube is down and we have a cached handle for it, attempt a
        connect; otherwise idle. The disconnect callback flips ``connected`` so the
        next iteration reconnects (the scan will have refreshed the cache once the
        dropped tube starts advertising again).

        Reconnect uses **exponential backoff with jitter** (#6): a flat retry made
        the 12×TL120C + 5×TL90C fleet stampede BlueZ's single scan session in
        lock-step after a mass drop (power blip, adapter reset). Each failed attempt
        doubles the delay up to :data:`_SUPERVISE_RETRY_MAX`, and a random component
        spreads the herd out; a successful connect resets the backoff.
        """
        tube = self.tubes[mac]
        backoff = _SUPERVISE_RETRY_BASE
        while not self._shutdown:
            if tube.connected:
                backoff = _SUPERVISE_RETRY_BASE     # healthy: reset for the next drop
                await asyncio.sleep(_SUPERVISE_CONNECTED)
                await self._check_liveness(tube)
                continue
            handle = self._devices.get(mac)
            if handle is not None:
                await self._try_connect(tube, handle)
                if tube.connected:
                    backoff = _SUPERVISE_RETRY_BASE
                    continue                        # poll-connected on the next loop
            # Still down (no handle cached yet, or the attempt failed): wait a
            # jittered backoff window, then grow it up to the cap.
            await asyncio.sleep(_retry_delay(backoff))
            backoff = _next_backoff(backoff)

    async def _check_liveness(self, tube: Tube) -> None:
        """Detect a half-open link by demanding a reply, not a successful write (#47).

        Runs from the supervisor's connected-poll. Does nothing while recent
        traffic proves the link: any notify advances ``state.last_reply_at``
        (streaming TL120C-2s ACK every applied colour frame with ``0x05``, so a
        healthy busy rig is never probed). Once the tube has been silent for
        :attr:`liveness_interval`, send a canary query (:meth:`canary` — a by-MAC
        state query timed against the notify it elicits); after
        :data:`_PROBE_MISSES` consecutive silent probes, declare the link
        half-open and drop it so the supervisor reconnects.

        A tube that has **never** replied is exempt: some fixtures are
        deaf-but-controllable (no notify support), and we cannot tell that apart
        from dead — probing them would drop-cycle a working light forever.
        """
        if not self.liveness_interval or not tube.connected:
            return
        last = tube.state.last_reply_at
        if last is None:
            return                      # never replied: can't distinguish deaf from dead
        if time.monotonic() - last < self.liveness_interval:
            return                      # recent reply proves the link; nothing to do
        if await self.canary(tube.mac) is not None:
            tube.probe_misses = 0       # answered: link is live (reply reset staleness)
            return
        tube.probe_misses += 1
        if tube.probe_misses < _PROBE_MISSES:
            return
        log.warning("%s silent for %d liveness probes — dropping half-open link",
                    tube.mac, tube.probe_misses)
        tube.probe_misses = 0
        self._on_drop(tube.mac)         # supervisor loop reconnects on its next pass

    async def _try_connect(self, tube: Tube, handle) -> None:
        """Attempt one connection to ``tube`` using a cached transport handle.

        BUG-1 guard: after ``connect()`` returns we re-check ``is_connected`` and
        only then set the tube's fields. There is **no await** between that check
        and the assignment, so the transport's disconnect callback (which runs on
        this same loop) cannot interleave and leave us "connected" with a dead client.

        The attempt is bounded by :data:`_CONNECT_TIMEOUT` (#7): a BlueZ connect can
        hang past bleak's own timeout (D-Bus call with no reply), and unbounded it
        stalls this tube's supervisor forever. A timeout just falls through to the
        supervisor's backoff-and-retry, same as any failed attempt.
        """
        try:
            async with asyncio.timeout(_CONNECT_TIMEOUT):
                client = await self.transport.connect(
                    handle, on_disconnect=lambda m=tube.mac: self._on_drop(m))
            if not self.transport.is_connected(client):
                return                          # dropped during connect; retry later
            tube.client = client
            tube.connected = True
            log.info("connected %s", tube.mac)
            self._emit()                        # a tube came online
        except Exception as exc:
            # Expected when the tube is in 2.4 G mode or held by another central.
            log.debug("connect %s failed: %s", tube.mac, exc)
            return

        # Subscribe to status pushes so reads (battery/power/version/…) land in
        # tube.state. Best-effort: a light that won't notify is still controllable.
        try:
            await self.transport.subscribe(
                client, lambda data, m=tube.mac: self._on_notify(m, data))
        except Exception as exc:
            log.debug("notify subscribe %s failed: %s", tube.mac, exc)

        # Auto-query on connect: elicit version (→ model inference) + battery/state so a
        # freshly-connected tube self-identifies and populates telemetry immediately,
        # instead of showing "generic"/blank until something else polls it. Fire-and-
        # forget so it never holds up the supervisor loop; best-effort by design.
        asyncio.create_task(self._auto_query_on_connect(tube.mac))

    async def _auto_query_on_connect(self, mac: str) -> None:
        """Best-effort battery/state/version query fired when a tube connects.

        Reuses :meth:`query` (which is idempotent and MAC-addressed); a failure or a
        tube that dropped again before it ran is swallowed — the next connect retries.
        """
        try:
            await self.query(mac)
        except Exception as exc:
            log.debug("auto-query %s failed: %s", mac, exc)

    def _on_notify(self, mac: str, data: bytearray) -> None:
        """Notify callback: decode a status frame into the tube's state."""
        parsed = replies.parse(bytes(data))
        tube = self.tubes.get(mac)
        if tube is not None:
            tube.state.update_from_reply(parsed)
            # Stamp the reply time (monotonic, same clock as asyncio's default
            # loop) so the canary can measure a query->notify round trip (#46).
            tube.state.last_reply_at = time.monotonic()
            # Fallback only: if neither config nor the advertised-name decoder
            # identified the model, infer it from the firmware version just returned
            # (best-effort; config and name-decode both take precedence).
            if tube.model is None and "version" in parsed:
                inferred = models.infer_model(parsed["version"])
                if inferred:
                    tube.model = inferred
            self._emit()                        # fresh telemetry landed
        log.debug("notify %s: %s", mac, parsed)

    def _on_drop(self, mac: str) -> None:
        """Disconnect callback: mark the tube down so the supervisor reconnects."""
        tube = self.tubes.get(mac)
        if tube is not None:
            tube.connected = False
            tube.client = None
            log.info("dropped %s (will reconnect)", mac)
            self._emit()                        # a tube went offline

    async def stop(self) -> None:
        """Shut down cleanly: stop scanning, stop supervisors, then disconnect.

        Order matters (BUG-4): we must cancel the supervisor tasks and the scan
        *before* disconnecting, otherwise a supervisor would immediately try to
        reconnect the very tube we are tearing down. A clean disconnect here is
        what frees the BLE link — a hard kill instead strands it (the light stays
        "connected" in BlueZ and stops advertising).
        """
        self._shutdown = True

        for task in self._supervisors.values():
            task.cancel()
        for task in self._supervisors.values():
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._supervisors.clear()

        if self._scanning:
            await self.transport.stop_scan()
            self._scanning = False

        await self.cancel_effect()

        for tube in self.tubes.values():
            if tube.client and tube.connected:
                try:
                    await self.transport.disconnect(tube.client)
                except Exception:
                    pass

    # ---- addressing ------------------------------------------------------
    def ordered(self) -> list[str]:
        """Return connected tube MACs in physical-position order.

        Tubes with an assigned position sort first by that position; the rest
        fall back to MAC order, so the result is deterministic either way.
        """
        connected = [t for t in self.tubes.values() if t.connected]
        return [t.mac for t in sorted(
            connected,
            key=lambda t: (t.position is None,
                           t.position if t.position is not None else 0,
                           t.mac))]

    def resolve(self, target: str) -> list[str]:
        """Resolve a target word into the list of connected MACs it refers to.

        * ``all``        -> every connected tube (position order).
        * ``t<N>``       -> the connected tube at 1-based position N (may be empty).
        * a group name   -> every connected member, in the group's declared order
                            (from the shared device book; nested groups flattened).
        * an alias       -> that tube, if known and connected.
        * a MAC string   -> that tube, if known and connected (else empty).

        The built-ins (``all`` / ``t<N>``) are matched first, so a group or alias
        can't shadow them.
        """
        target = target.strip()
        if target == "all":
            return self.ordered()
        if target.lower().startswith("t") and target[1:].isdigit():
            pos = int(target[1:])
            return [t.mac for t in self.tubes.values()
                    if t.connected and t.position == pos]
        # Aliases / groups (and bare MACs) via the shared device book. expand()
        # already handles a bare MAC, so if it comes back empty the target is a
        # name we don't know — fall through to a direct MAC check for parity with
        # the pre-book behaviour (which returns [] for anything unknown).
        macs = self.book.expand(target) or [target.upper()]
        return [m for m in macs if m in self.tubes and self.tubes[m].connected]

    # ---- io --------------------------------------------------------------
    async def write(self, mac: str, frame: bytes) -> bool:
        """Write one frame to one tube. Returns ``False`` if it isn't connected.

        The write is bounded by :data:`_write_timeout`. A timeout means the link is
        **half-open** — BlueZ still says "connected" but the ACL is dead and the write
        will never land — so we drop the tube (the supervisor then reconnects it) and
        fail fast. This is what stops one dead light from stalling the whole fleet: an
        unbounded hung write would block the DMX send loop's ``gather`` for seconds
        every tick, replaying minutes-old frames on the *healthy* tubes.
        """
        tube = self.tubes.get(mac)
        if not tube or not tube.connected or not tube.client:
            return False
        try:
            # asyncio.timeout, not wait_for: on 3.11, wait_for can swallow an
            # external cancellation when the inner write has already completed,
            # leaving the caller (e.g. an effect engine being cancelled) running.
            async with asyncio.timeout(self._write_timeout):
                await self.transport.write(tube.client, frame)
            return True
        except asyncio.TimeoutError:
            log.warning("write to %s exceeded %.2fs — dropping half-open link",
                        mac, self._write_timeout)
            self._on_drop(mac)          # mark down so the supervisor reconnects
            return False
        except Exception as exc:        # transport/OS error mid-write: fail this write
            log.debug("write to %s failed: %s", mac, exc)
            return False

    async def write_many(self, macs, frame: bytes) -> None:
        """Write the same frame to several tubes, ignoring per-tube failures."""
        for mac in macs:
            try:
                await self.write(mac, frame)
            except Exception as exc:
                log.debug("write to %s failed: %s", mac, exc)

    # ---- typed direct-frame API -----------------------------------------
    # These are the primary surface: structured args in, a human-readable result
    # string out. Every mutating command cancels a running effect first, so manual
    # control always wins over an animation. The string grammar (:mod:`.grammar`)
    # and every wire module are thin layers on top of these.
    async def power(self, target: str, on: bool) -> str:
        """Turn the target tube(s) on or off (``0x81``)."""
        return await self._apply_direct(
            target, "power", commands.Power(on).frame(),
            f"{target} power {'on' if on else 'off'}")

    async def set_hsi(self, target: str, hue: int, sat: int = 100, bri: int = 100) -> str:
        """Set an HSI colour on the target tube(s) (``0x86``)."""
        return await self._apply_direct(
            target, "hsi", commands.HSI(hue, sat, bri).frame(),
            f"{target} hsi {hue} {sat} {bri}")

    async def set_cct(self, target: str, bri: int, temp: int,
                      gm: int = frames.GM_NEUTRAL) -> str:
        """Set white colour-temperature + brightness on the target tube(s) (``0x87``)."""
        return await self._apply_direct(
            target, "cct", commands.CCT(bri, temp, gm).frame(),
            f"{target} cct {bri} {temp} {gm}")

    async def set_bri(self, target: str, bri: int) -> str:
        """Set brightness only (white-ish) on the target tube(s) (``0x86``)."""
        return await self._apply_direct(
            target, "bri", commands.Brightness(bri).frame(), f"{target} bri {bri}")

    async def raw(self, target: str, hexstr: str) -> str:
        """Send a literal frame (fuzzing / replay) to the target tube(s)."""
        return await self._apply_direct(
            target, "raw", commands.Raw(hexstr).frame(), f"{target} raw {hexstr}")

    async def _apply_direct(self, target: str, verb: str, frame: bytes, last: str) -> str:
        """Write one direct frame to every resolved tube (manual control wins).

        Resolves ``target``, cancels any running effect, writes ``frame`` to each
        tube and records ``last`` as its last-command state. Returns the standard
        ``ok <verb> -> N tube(s)`` reply, or ``no tubes ...`` if nothing resolved.
        """
        macs = self.resolve(target)
        if not macs:
            raise UnknownTarget(target)
        await self.cancel_effect()          # manual control overrides any running effect
        await self.write_many(macs, frame)
        for mac in macs:
            self.tubes[mac].state.last = last
        self._emit()
        return f"ok {verb} -> {len(macs)} tube(s)"

    # ---- dispatch (string grammar, opt-in convenience over the typed API) -
    async def dispatch(self, line: str) -> str:
        """Parse and execute one command *line*; return a human-readable result.

        A thin back-compat convenience: it defers to :func:`neewer.grammar.dispatch`,
        which parses the line and calls the typed methods above. The typed methods —
        not this string front-end — are the library's contract; the grammar is
        imported lazily so the ``Fleet`` class body never depends on it.
        """
        from . import grammar

        return await grammar.dispatch(self, line)

    # ---- pixel palette ---------------------------------------------------
    async def pixel(self, target: str, colors, effect: int = 1) -> str:
        """Paint a per-segment pixel palette on the target tube(s) (``0xB0``, TL120C).

        ``colors`` is one token per segment band (a hue ``0-359``, ``off`` (dark),
        or ``k<kelvin>``). The effect is MAC-addressed, so the params + palette
        frames embed *each tube's own MAC* and are written to that tube — the palette
        is chunked to the ATT payload cap (the device reassembles by header length).
        An empty palette or a bad colour token raises ``ValueError`` (http → 400).
        """
        cmd = commands.Pixel(tuple(colors), effect)     # validates the palette is non-empty
        line = f"{target} pixel {' '.join(colors)}"

        async def send(mac: str):
            if not self._caps(mac).pixel:   # only send 0xB0 to pixel-capable fixtures
                return False
            mac6 = frames.mac_bytes(mac)
            ok = await self._write_chunked(mac, cmd.params_frame(mac6))
            await asyncio.sleep(_PIXEL_SETTLE)
            ok = await self._write_chunked(mac, cmd.palette_frame(mac6)) and ok  # may raise
            return True if ok else None     # None: capable but the write failed

        return await self._run_per_mac(target, line, send, label="pixel", skip_noun="pixel")

    def _caps(self, mac: str) -> models.Capabilities:
        """The capability set for a tube (permissive ``GENERIC`` if model unknown)."""
        tube = self.tubes.get(mac)
        return models.capabilities(tube.model if tube else None)

    async def _run_per_mac(self, target, line, send, *, label, skip_noun):
        """Resolve, cancel effects, then apply a per-tube ``send`` with skip-and-report.

        The shared spine of the by-MAC commands (:meth:`pixel` and the by-MAC colour
        modes via :meth:`_run_by_mac`). ``send(mac)`` returns ``True`` if the command
        was applied to that tube (counted as sent; its ``state.last`` is updated),
        ``False`` if the tube is incapable (counted and skipped), or ``None`` if it
        was capable but the write failed (counted as neither, matching the original
        per-command loops). Raises :class:`~neewer.errors.UnknownTarget` for an empty
        target and :class:`~neewer.errors.Unsupported` if every addressed tube skipped.

        (:meth:`scene` keeps its own loop — it has two distinct success paths, the
        direct ``0x88`` and by-MAC ``0x91``, and a different detail message, so it
        doesn't fit this single-capability shape.)
        """
        macs = self.resolve(target)
        if not macs:
            raise UnknownTarget(target)
        await self.cancel_effect()          # manual control overrides any animation
        sent = skipped = 0
        for mac in macs:
            outcome = await send(mac)
            if outcome:                     # True -> applied
                sent += 1
                self.tubes[mac].state.last = line
            elif outcome is False:          # incapable -> skip-and-report
                skipped += 1
            # None -> capable but the write failed; counted as neither, as before.
        if sent == 0 and skipped:
            raise Unsupported(
                f"{label} unsupported on target {target!r} ({skipped} non-{skip_noun} tube(s))")
        self._emit()
        msg = f"ok {label} -> {sent} tube(s)"
        if skipped:
            msg += f" ({skipped} lack {skip_noun} support)"
        return msg

    # ---- by-MAC colour modes (TL120C) ------------------------------------
    async def set_rgbcw(self, target: str, bri: int, r: int = 0, g: int = 0,
                        b: int = 0, c: int = 0, w: int = 0) -> str:
        """Set RGB + Cold/Warm white on the target tube(s) (``0xA9``, TL120C by-MAC).

        ``bri`` is 0..100; the five channels are each 0..255 and default to 0.
        Skips-and-reports fixtures that lack the capability (there is no confirmed
        direct fallback for this mode).
        """
        return await self._run_by_mac(
            target, commands.RGBCW(bri, r, g, b, c, w),
            f"{target} rgbcw {bri} {r} {g} {b} {c} {w}")

    async def set_xy(self, target: str, bri: int, x: float, y: float) -> str:
        """Set a CIE-1931 xy colour point on the target tube(s) (``0xB7``, TL120C by-MAC).

        ``bri`` is an int (0..100); ``x``/``y`` are floats in 0..1 (out of range
        raises ``ValueError`` → http 400). Skips-and-reports incapable fixtures.
        """
        return await self._run_by_mac(
            target, commands.XY(bri, x, y), f"{target} xy {bri} {x} {y}")

    async def set_gel(self, target: str, hue: int, sat: int, bri: int,
                      brand: int = frames.GEL_BRAND_ROSCO, gel_no: int = 0) -> str:
        """Set a lighting-gel colour on the target tube(s) (``0xAD``, TL120C by-MAC).

        ``hue``/``sat``/``bri`` are ints; ``brand`` is the numeric brand byte
        (default ROSCO — the ``rosco``/``lee`` spelling is parsed by the grammar);
        ``gel_no`` is the catalogue number. Skips-and-reports incapable fixtures.
        """
        return await self._run_by_mac(
            target, commands.Gel(hue, sat, bri, brand, gel_no),
            f"{target} gel {hue} {sat} {bri} {brand} {gel_no}")

    async def _run_by_mac(self, target: str, cmd, line: str) -> str:
        """Skip-and-report driver for the single-capability by-MAC colour modes.

        For each tube checks ``cmd.CAPABILITY``; a capable tube gets ``cmd.frame(mac6)``
        — its own MAC embedded — written to it, an incapable one is skipped-and-reported.
        There is no direct fallback because none is confirmed for these opcodes. The
        resolve / cancel / count / report spine is shared via :meth:`_run_per_mac`.
        """
        mode = cmd.CAPABILITY

        async def send(mac: str):
            if not getattr(self._caps(mac), mode):   # only send to capable fixtures
                return False
            return True if await self.write(mac, cmd.frame(frames.mac_bytes(mac))) else None

        return await self._run_per_mac(target, line, send, label=mode, skip_noun=mode)

    async def scene(self, target: str, effect: int, *params: int) -> str:
        """Send a built-in scene, choosing the frame each fixture actually honours.

        Two transports carry the same effect catalogue: the direct ``0x88`` and the
        MAC-addressed ``0x91`` (inner ``0x8b``). The TL120C's firmware has no ``0x88``
        handler (that frame no-ops) but *does* handle ``0x91``, so per fixture we pick:

        * ``scene_legacy`` tube (TL90C, generic) -> direct ``0x88``;
        * else ``scene_mac`` tube (TL120C) -> by-MAC ``0x91`` with the tube's own MAC;
        * else the fixture honours neither -> skip it and report.

        The ``<effect> <params…>`` tail is identical for both frames, so the caller
        passes the same ``effect`` + ``params`` regardless of fixture.
        """
        macs = self.resolve(target)
        if not macs:
            raise UnknownTarget(target)
        cmd = commands.Scene(effect, tuple(params))
        await self.cancel_effect()
        line = f"{target} scene {' '.join(str(v) for v in (effect, *params))}"

        legacy = mac_frame = unsupported = 0
        for mac in macs:
            caps = self._caps(mac)
            if caps.scene_legacy:
                await self.write(mac, cmd.legacy_frame())
                legacy += 1
            elif caps.scene_mac:
                await self.write(mac, cmd.mac_frame(frames.mac_bytes(mac)))
                mac_frame += 1
            else:
                unsupported += 1
                continue
            self.tubes[mac].state.last = line

        sent = legacy + mac_frame
        if sent == 0:
            raise Unsupported(
                f"scene unsupported on target {target!r} "
                f"({unsupported} tube(s) honour neither 0x88 nor 0x91 scene)")
        self._emit()
        msg = f"ok scene -> {sent} tube(s)"
        detail = []
        if mac_frame:
            detail.append(f"{mac_frame} via 0x91")
        if unsupported:
            detail.append(f"{unsupported} unsupported")
        if detail:
            msg += " (" + ", ".join(detail) + ")"
        return msg

    async def identify(self, target: str) -> str:
        """Flash the target tube(s) to physically locate them (``0x99``, by MAC).

        Each tube is addressed by its own MAC. A running effect is cancelled first so
        the flash is actually visible (an effect stream would immediately overwrite
        it). One-shot: the light blinks and returns to its prior output on its own.
        """
        macs = self.resolve(target)
        if not macs:
            raise UnknownTarget(target)
        await self.cancel_effect()
        cmd = commands.Identify()
        for mac in macs:
            await self.write(mac, cmd.frame(frames.mac_bytes(mac)))
            self.tubes[mac].state.last = f"{mac} identify"
        self._emit()
        return f"ok identify -> {len(macs)} tube(s)"

    async def _write_chunked(self, mac: str, frame: bytes,
                             chunk: int = _ATT_PAYLOAD) -> bool:
        """Write one frame in ``<=chunk``-byte ATT pieces (for frames past the MTU)."""
        tube = self.tubes.get(mac)
        if not tube or not tube.connected or not tube.client:
            return False
        for i in range(0, len(frame), chunk):
            await self.transport.write(tube.client, frame[i:i + chunk])
        return True

    # ---- reads -----------------------------------------------------------
    def render_state(self, target: str) -> str:
        """Return the cached state snapshot as JSON, optionally filtered by target."""
        snap = self.snapshot()
        if target and target != "all":
            wanted = set(self.resolve(target))
            snap = {mac: st for mac, st in snap.items() if mac in wanted}
        return json.dumps(snap)

    async def query(self, target: str) -> str:
        """Ask each target tube for battery / state / version.

        The frames are MAC-addressed; the answers arrive asynchronously on the
        notify characteristic (decoded by :func:`_on_notify`), so the caller reads
        the result a beat later with a ``state`` command.
        """
        macs = self.resolve(target)
        if not macs:
            raise UnknownTarget(target)
        for mac in macs:
            mac6 = frames.mac_bytes(mac)
            for query in (frames.battery_query(mac6),
                          frames.state_query(mac6),
                          frames.version_query_mac(mac6)):
                await self.write(mac, query)
                await asyncio.sleep(0.05)       # don't outrun the light's reply queue
        return f"ok query -> {len(macs)} tube(s) (replies via notify; read with 'state')"

    async def canary(self, mac: str, timeout: float = 1.0) -> float | None:
        """One round-trip latency probe: query the tube, time the notify (#46).

        ``write-without-response`` gives no delivery ACK, so this synthesizes one:
        a by-MAC state query (``0x8E``) elicits a notify, and the time from issue
        to ``state.last_reply_at`` advancing is the round trip. A rising canary
        RTT means the per-connection BlueZ TX queue is building — feed the result
        to ``WriteGovernor.on_delivery(now, rtt)`` (the pluggable congestion
        signal; the governor also works on the issue-rate estimate alone).

        Returns the RTT in seconds, or ``None`` if the tube isn't connected, the
        write failed, or no reply arrived within ``timeout``. Cheap enough to run
        periodically per tube; the reply also refreshes the cached telemetry.
        """
        tube = self.tubes.get(mac)
        if tube is None or not tube.connected:
            return None
        # Wake on any state change (a notify fires _emit); cheaper than polling.
        wakeup = asyncio.Event()
        unsubscribe = self.subscribe(wakeup.set)
        try:
            baseline = tube.state.last_reply_at
            start = time.monotonic()
            if not await self.write(mac, frames.state_query(frames.mac_bytes(mac))):
                return None
            deadline = start + timeout
            while True:
                wakeup.clear()
                reply_at = tube.state.last_reply_at
                if reply_at is not None and (baseline is None or reply_at > baseline):
                    return time.monotonic() - start
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None             # no reply: the link is stalled/deaf
                try:
                    async with asyncio.timeout(remaining):
                        await wakeup.wait()
                except asyncio.TimeoutError:
                    return None
        finally:
            unsubscribe()

    # ---- effects ---------------------------------------------------------
    async def flow(self, mode: str, **opts) -> str:
        """Start a named host-streamed effect on all connected tubes (typed alias)."""
        return await self.start_effect(mode, opts)

    async def start_effect(self, mode: str, opts: dict) -> str:
        """Start a named effect on all connected tubes, replacing any running one.

        The whole swap (cancel old → spawn new) is under ``self._lock`` so two
        concurrent starts can't both spawn and orphan a task that keeps writing
        forever (BUG-2). The lookup runs before taking the lock to fail fast.
        """
        fn = effects.REGISTRY.get(mode)
        if not fn:
            raise UnknownEffect(mode)
        async with self._lock:
            await self._cancel_effect_locked()
            tubes = self.ordered()
            if not tubes:
                raise UnknownTarget("all")      # nothing connected to run the effect on
            self._effect_task = asyncio.create_task(fn(self, tubes, **opts))
        return f"ok effect {mode} on {len(tubes)} tube(s)"

    async def cancel_effect(self) -> None:
        """Cancel the running effect (if any) and wait for it to unwind."""
        async with self._lock:
            await self._cancel_effect_locked()

    async def _cancel_effect_locked(self) -> None:
        """Cancel the effect task. Caller must hold ``self._lock``."""
        task = self._effect_task
        self._effect_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            # The task's own expected cancellation — swallow it. But if *we* are
            # being cancelled (e.g. shutdown racing this await), propagate (BUG-5).
            current = asyncio.current_task()
            if current is not None and current.cancelling() > 0:
                raise
        except Exception:
            log.debug("effect task errored during cancel", exc_info=True)

    # ---- state -----------------------------------------------------------
    def snapshot(self) -> dict:
        """Return a JSON-serialisable view of every known tube, for status modules.

        ``caps`` is the tube's :class:`~neewer.protocol.models.Capabilities` as a
        plain dict (the permissive GENERIC set while the model is unknown), so
        every status consumer — the SSE stream, MQTT, HA — can gate its widgets
        without re-deriving model knowledge. Additive: consumers that predate the
        key simply ignore it.
        """
        return {mac: {"name": t.name, "pos": t.position, "connected": t.connected,
                      "model": t.model, "rssi": t.rssi,
                      "caps": asdict(self._caps(mac)), **t.state.as_dict()}
                for mac, t in self.tubes.items()}


#: Back-compat alias for the daemon's historical class name. New code uses ``Fleet``.
NeewerCore = Fleet
