# Changelog

All notable changes to the `neewer` library are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/); the project uses semantic
versioning — **pre-1.0, a minor bump may change the public API.**

## [Unreleased]

### Added
- **`Fleet(passive_scan=True)`**: an opt-in passive scan (less airtime and radio
  energy than continuous active scanning). The tube name is in the primary
  advertising PDU, so identity is unaffected, and a BlueZ advertisement-monitor
  filter is built from the name prefixes. Passive mode needs
  `bluetoothd --experimental` on Linux; if it can't start, the transport logs a
  warning and falls back to active scanning rather than a fleet that silently
  discovers nothing. Confirmed on real hardware: the fallback still discovers
  tubes on a stock stack.

## [0.1.3] — 2026-07-14

### Added
- **`neewer.ota` and the `neewer-ota` CLI**: firmware flashing over the custom
  `0x78` OTA block transport (rides the normal control service, not Nordic DFU).
  Pure, testable frame builders and parsers (`header_frame`, `block_frame`,
  `parse_ack`) plus an ACK-driven `flash()` and a read-only `check()` engine over
  an injectable `OtaLink`. Fragments are paced ~20 ms apart with
  write-with-response, which keeps the two-chip UART reassembler from overrunning.
  The CLI is dry-run by default; `--confirm` is required to write. Confirmed
  working on a TL60 RGB-3 and a TL120C-2.

### Fixed
- The `neewer` CLI now answers a missing/unusable Bluetooth stack (a VM with no
  adapter, BlueZ not running, the radio off) with a one-screen diagnosis and
  exit code 1 instead of a raw traceback. `NEEWER_DEBUG=1` restores the
  traceback; non-Bluetooth errors still raise.

## [0.1.2] — 2026-07-05

### Fixed
- `Fleet.start()` no longer runs the BlueZ zombie-link self-heal when a custom
  transport is injected. The heal is bleak-backend-only; with a
  `neewer.testing.MockTransport` it would disconnect real lights that another
  process on the machine legitimately holds — exactly the environment a
  downstream test suite runs in.

## [0.1.1] — 2026-07-05

### Added
- **`neewer.testing`** — a public, stdlib-only `MockTransport` + `MockTube` so
  downstream suites can run a real `Fleet` with no radio: virtual tubes that
  advertise/stop-advertising like hardware, frame parsing into per-tube state
  (`power` / `hsi` / `cct` + full write log), checksummed query replies
  (battery / state / version) on the notify path, and failure injection
  (`drop()`, `write_latency`, `fail_writes`). (#1)

### Fixed
- `Fleet` now holds strong references to its fire-and-forget auto-query tasks
  (the event loop keeps tasks weakly; an unreferenced one could be
  garbage-collected mid-run) and cancels any still in flight on `stop()`.

## [0.1.0] — 2026-07-05

Initial extraction of the `neewer` library. Highlights:

### Added
- **Typed command API** on `Fleet` — `power`, `set_hsi`, `set_cct`, `set_bri`,
  `set_rgbcw`, `set_xy`, `set_gel`, `scene`, `pixel`, `identify`, `raw`, `flow`.
  This is the primary surface; structured args in, a result string out.
- **Typed command model** in `neewer.protocol.commands` — one frozen dataclass per
  action, the single source of argument-order truth, with self-validation and
  frame-building.
- **`neewer.grammar`** — the opt-in `<target> <action> [args]` string grammar
  (`parse` / `dispatch(fleet, line)` / presets / OSC mapping), moved out of the
  core library. `Fleet.dispatch(str)` is now a thin convenience over the typed API.
- **`neewer.errors`** — a typed error model (`NeewerError` +
  `UnknownTarget` / `UnknownAction` / `UnknownEffect` / `UnknownPreset` /
  `Unsupported`). Command failures raise instead of returning sentinel strings.
- **`neewer.transport`** — a `Transport` Protocol with a bleak-backed default
  (`BleakTransport`), injected into `Fleet`. `neewer.fleet` now imports without
  `bleak` (only the transport touches it, lazily).
- **Change-event API** — `Fleet.subscribe(callback)` fires on connect / disconnect
  / status notify / command, so consumers can push instead of poll.
- **`TubeState`** — typed per-tube state replacing the free-form dict; `as_dict()`
  preserves the existing snapshot shape.
- Reconnect supervisor uses **exponential backoff with jitter** to avoid a
  thundering herd on a large fleet.
- **`py.typed`** marker so type hints ship to consumers.
- **New DMX personalities** in `neewer.protocol.dmx` — `rgb` (3 channels R,G,B →
  HSI, for RGB sources such as LedFx) and `rgbw` (4 channels R,G,B,W → drives the
  tube's dedicated cold/warm white via the by-MAC RGBCW command). The DMX layer
  now builds per-target-MAC frames so by-MAC personalities work.
- **Concurrent DMX writes** — `dmx.send_tick` issues its per-tick BLE writes
  concurrently, for higher and flatter multi-fixture throughput.
- **Adaptive per-connection write pacing** — `neewer.protocol.dmx.WriteGovernor`,
  a self-tuning BBR-style controller that holds each tube's BLE write rate at or
  below its **measured** delivery rate. Because BLE write-without-response has no
  backpressure, a source faster than a link can drain will otherwise pile frames
  up in the Bluetooth transmit queue and run minutes behind; the governor instead
  **drops the newest frame** for an over-paced tube (frames are latest-wins, so no
  visual cost) rather than queueing. It reads back per-tube latency via a canary
  (`Fleet.canary`, a query/reply round-trip) and continuously re-probes, so a
  briefly-slow link recovers on its own — no sticky "slow" verdict. Wired into
  `dmx.send_tick(..., governors=...)`; with `governors=None` behaviour is
  byte-identical to before. Zero-config auto-tunes; optional bounds are exposed as
  Art-Net module knobs.
- **Bounded writes** — `Fleet.write` now enforces a write deadline; a write to a
  stalled or half-open link is dropped (and the reconnect supervisor takes over)
  so one unresponsive light can no longer stall the whole fleet's write fan-out.
- **Auto-query on connect** — a tube is queried for version / battery / state the
  moment it connects, so it identifies its model and reports telemetry immediately
  instead of showing generic until the first later query.
- **New fixture support** — TL60 (firmware `3.0.3` → `TL60 RGB-3`) with a
  `streamer` capability flag and a streamer-support query/reply; firmware `1.1.9`
  is now recognised as TL90C.
- **`frames.temp_query`** — a temperature/fan query that pairs with the existing
  temperature-reply decoder.
- **`commands.ACTIONS` registry** — the single source of per-action argument order,
  shared by transports (replaces duplicated arg-order tables).
- **`Fleet.register_verb(name, handler)`** — a generic hook to register custom
  command verbs. Presets are no longer a library concern: the string grammar's
  `preset` verb and its storage moved out to consumers.
- **Experimental group/mesh frame builders** — channel-addressed colour and
  provisioning frames, plus a `net_bytes` helper and an ACK-reply decoder. These
  are frame-layer only and not yet wired into the high-level API.
- **Bounded connect attempts** — one connection attempt is capped at 15 s, so a
  hung BlueZ connect can no longer stall a tube's reconnect supervisor forever.
- **Half-open-link liveness probe** — `Fleet(liveness_interval=30.0)`: a
  connected tube that has been silent past the threshold gets a canary query;
  three consecutive silent probes drop the link so the supervisor reconnects it.
  Catches links where writes still "succeed" into a dead ACL. `0` disables;
  fixtures that have never notified are exempt (deaf ≠ dead).

### Internal
- A shared `_run_per_mac` helper unifies the by-MAC command paths.
- Test suite is now 371 passing; the pure protocol layer has no Bluetooth
  dependency.

### Notes
- The pure `neewer.protocol` layer (and `neewer.grammar` / `neewer.errors`) never
  import `bleak`; only the injected transport does.
