# neewer

Python control library for **Neewer TL-series** RGB tube lights (TL120C / TL90C
and friends) over **Bluetooth LE** — no app, no pairing.

```python
import asyncio
from neewer import Fleet

async def main():
    async with Fleet() as fleet:              # scan + connect everything in range
        await fleet.set_hsi("all", 240, 100, 100)     # all lights blue

asyncio.run(main())
```

**New here?** [QUICKSTART.md](QUICKSTART.md) walks from install to working CLI
control (Linux / Windows / macOS), plus a LedFx screen-reactive-lighting bonus.

The **typed methods** (`set_hsi`, `power`, `set_cct`, …) are the primary API. A
one-line string grammar (`fleet.dispatch("all hsi 240 100 100")`) is also
available as a convenience — see [The string grammar](#the-string-grammar).

Or from the terminal:

```console
$ neewer scan                     # list the lights it can see
$ neewer all hsi 240 100 100      # everything blue
$ neewer all power off
$ neewer t1 cct 100 5600          # position 1 -> 5600 K
```

## What you get

- **`neewer.Fleet`** — the batteries-included BLE client: continuous discovery,
  persistent connections, an auto-reconnect supervisor (exponential backoff +
  jitter) that survives a light being switched off or grabbed by another device,
  target addressing (`all` / `t<N>` / MAC / alias / group), and typed methods for
  every action. Subscribe with `fleet.subscribe(cb)` to be notified on any state
  change (connect / disconnect / telemetry / command) instead of polling.
- **`neewer.protocol`** — the pure, **standard-library-only** frame/reply/model
  layer, including the typed command model (`neewer.protocol.commands`, one
  dataclass per action, with the shared `commands.ACTIONS` argument-order
  registry) and `neewer.protocol.dmx` DMX-over-IP personalities (`hsi`, `cct`,
  `rgb`, `rgbw`) plus `WriteGovernor`, a self-tuning per-connection write pacer
  that keeps each tube at or below its measured BLE delivery rate (dropping the
  newest frame rather than backing up the Bluetooth transmit queue). It never
  imports `bleak`, so a non-BLE transport (an ESP32 bridge, a UART gateway) can
  reuse all the frame knowledge without a radio.
- **`neewer.transport`** — the radio seam: a `Transport` Protocol with a
  bleak-backed default (`BleakTransport`). `Fleet` takes `transport=` so you can
  inject a fake (for tests) or an alternative backend. `neewer.fleet` itself
  imports without `bleak`; only the transport pulls it in, lazily.
- **`neewer.errors`** — typed errors (`UnknownTarget` / `UnknownAction` /
  `Unsupported` / …); command failures raise, they don't return sentinel strings.
- **`neewer.grammar`** — the opt-in `<target> <action> [args]` string grammar
  (`parse`, `dispatch`, OSC mapping) layered over the typed API. Register extra
  verbs with `fleet.register_verb(name, handler)`.
- **`neewer.effects`** — animation engines (comet / hue-flow / palette) that run
  against a held `Fleet`.
- **`neewer.devices`** — the device book: give your lights human names, physical
  positions, and groups in `~/.config/neewer/devices.toml`.

The package ships type hints (`py.typed`).

## Install

```console
$ pip install neewer
```

Requires Python 3.11+. The only runtime dependency is
[`bleak`](https://github.com/hbldh/bleak). Developed on Linux; for Windows see
[docs/INSTALL-WINDOWS.md](docs/INSTALL-WINDOWS.md) (draft — feedback welcome).

## The typed API

Every action is a method on `Fleet`; the first argument is always a target
(`all` / `t<N>` / MAC / alias / group):

| Method | Example |
|---|---|
| `power(target, on)` | `await fleet.power("all", False)` |
| `set_hsi(target, hue, sat=100, bri=100)` | `await fleet.set_hsi("t1", 240, 100, 80)` |
| `set_cct(target, bri, temp, gm=50)` | `await fleet.set_cct("t1", 100, 56)` |
| `set_bri(target, bri)` | `await fleet.set_bri("all", 50)` |
| `set_rgbcw(target, bri, r, g, b, c, w)` | `await fleet.set_rgbcw("t1", 60, 0, 0, 0, 255, 0)` |
| `set_xy(target, bri, x, y)` | `await fleet.set_xy("t1", 50, 0.3127, 0.329)` |
| `set_gel(target, hue, sat, bri, brand, gel_no)` | `await fleet.set_gel("t1", 45, 100, 50)` |
| `scene(target, effect, *params)` | `await fleet.scene("all", 3)` |
| `pixel(target, colors)` | `await fleet.pixel("t1", ["0", "off", "240"])` |
| `identify(target)` | `await fleet.identify("t2")` |
| `raw(target, hexstr)` | `await fleet.raw("t1", "78 81 01 01 fb")` |
| `flow(mode, **opts)` / `query(target)` / `render_state(target)` | `await fleet.flow("comet")` |

Failures raise `neewer.errors.NeewerError` subclasses (`UnknownTarget`,
`Unsupported`, …). See [`examples/`](examples/) for runnable scripts.

## The string grammar

For REPLs, wire protocols, and one-liners, `neewer.grammar` parses a line of
`<target> <action> [args]` and dispatches it to the typed API. `fleet.dispatch()`
is a thin convenience over it:

| Action | Example | Effect |
|---|---|---|
| `power on\|off` | `all power off` | toggle |
| `hsi <h> <s> <i>` | `all hsi 240 100 100` | hue/saturation/intensity |
| `cct <bri> <temp> [gm]` | `t1 cct 100 5600` | white, colour temperature |
| `bri <0-100>` | `all bri 50` | brightness only |
| `rgbcw <bri> <r> <g> <b> <c> <w>` | `t1 rgbcw 60 0 0 0 255 0` | RGB + cold/warm white (TL120C) |
| `xy <bri> <x> <y>` | `t1 xy 50 0.3127 0.329` | CIE-1931 point (TL120C) |
| `gel <hue> <sat> <bri> [brand] [no]` | `t1 gel 45 100 50 lee 7` | gel colour (TL120C) |
| `scene <effect> [params...]` | `all scene 3` | built-in scene |
| `pixel <colour...>` | `t1 pixel 0 off 240 k3200` | per-segment palette (TL120C) |
| `identify` | `t2 identify` | flash to locate |
| `flow <mode> [k=v...]` | `all flow comet` | start a running effect |
| `scan` / `state` / `query` | `state` | list / read / refresh cached state |

Targets: `all`, `t<N>` (physical position), a MAC, or an alias/group from your
device book.

## Related projects

- [neewerd](https://github.com/verygeeky/neewerd) — a ready-made control
  **daemon** built on this library: holds the BLE links and exposes the lights
  over socket / MQTT (Home Assistant discovery) / OSC / HTTP + web UI /
  Art-Net / sACN, plus an MCP server.
- [neewer-hardware](https://github.com/verygeeky/neewer-hardware) — the
  hardware and wire-protocol **reference** this library implements: frame
  format, the full opcode table, provisioning, DMX, and per-model capabilities.

## License

MIT.
