# Quickstart

Get from nothing to controlling a Neewer tube light from the command line, then
(bonus) drive it as a screen-reactive light with LedFx.

**Platform status:** the steps are written for all three desktop OSes. Linux is
what the project is developed and tested on. **Windows and macOS steps are
marked _(unverified)_** — believed correct from the platform docs, but not yet
run end-to-end on real hardware. If you try one, please
[report back](https://github.com/verygeeky/neewer-python/issues).

| Platform | CLI control | LedFx bonus |
|---|---|---|
| Linux | ✅ verified | ✅ verified |
| Windows | ⚠️ unverified | ⚠️ unverified |
| macOS | ⚠️ unverified (MAC-addressed colour modes have a [known gap](https://github.com/verygeeky/neewer-python/issues/3)) | ⚠️ unverified |

---

## Part 1 — Working CLI control

### Step 1: Prerequisites

- **Python 3.11 or newer**, 64-bit.
- A **Bluetooth 4.0+ (LE)** adapter — built-in or a USB dongle.
- A Neewer **TL-series** tube (TL120C / TL90C / TL60) or another Infinity-family
  fixture, powered on.

Platform notes:

- **Linux** — BlueZ must be installed and running (`systemctl status bluetooth`).
  On a headless/VM host you also need a real adapter passed through; a VM has
  none by default.
- **Windows** _(unverified)_ — Windows 10 build 16299+ or Windows 11 (the WinRT
  Bluetooth API). No drivers, no Zadig. See
  [docs/INSTALL-WINDOWS.md](docs/INSTALL-WINDOWS.md) for the detailed walkthrough.
- **macOS** _(unverified)_ — macOS 11+. The first `neewer` run triggers a
  Bluetooth permission prompt for your terminal app; grant it (or add the
  terminal under System Settings → Privacy & Security → Bluetooth).

### Step 2: Install

The recommended installer is [pipx](https://pipx.pypa.io/) — it puts the
`neewer` command on your PATH in its own isolated environment.

**Linux / macOS:**
```console
python3 -m pip install --user pipx
python3 -m pipx ensurepath
# open a NEW terminal so PATH updates, then:
pipx install neewer
```

**Windows** _(unverified)_**:**
```console
py -m pip install --user pipx
py -m pipx ensurepath
# open a NEW terminal, then:
pipx install neewer
```

> **The new-terminal step matters.** `pipx ensurepath` adds `~/.local/bin`
> (Linux/mac) or the Scripts dir (Windows) to your PATH, but only new shells pick
> that up. If `neewer` still isn't found afterward, the module form always works:
> `python3 -m neewer.cli scan` (or `py -m neewer.cli scan` on Windows).

Plain `pip install neewer` works too (into a venv or `--user`); pipx just keeps
the CLI isolated from your other packages.

### Step 3: Put the light in Bluetooth mode

Three things trip up first contact — check all three:

1. **BT mode.** The tube's physical **2.4G / BT** switch must be on **BT**. In
   2.4G mode the light is invisible to every Bluetooth client.
2. **Nothing else holds it.** A light accepts **one** Bluetooth central at a
   time. Close the NEEWER phone app (or move the phone away) — while the app
   holds a light it stops advertising and nothing else can find it.
3. **Don't OS-pair it.** The protocol is pairing-free. Do not "Add a device" in
   Windows/macOS Bluetooth settings; a light paired at the OS level may be held
   by the OS and become unconnectable from the library.

### Step 4: First contact

Scan for lights in range:

```console
neewer scan
```

You should get one JSON entry per light, advertising as `NW-<8 digits>&<8 hex>`
(e.g. `NW-20240047&00000000`). The digits identify the model, which the library
decodes automatically.

If you instead get **"Bluetooth is unavailable"** with a short checklist, the
adapter/stack isn't ready — work through the causes it names (no adapter, BlueZ
down, radio off). That message is the friendly form; `NEEWER_DEBUG=1 neewer scan`
shows the full traceback.

### Step 5: Control a light

```console
neewer all hsi 240 100 100     # everything blue (hue 240, sat 100, intensity 100)
neewer all cct 100 5600        # neutral white, full brightness, 5600 K
neewer all bri 40              # dim to 40%
neewer all power off
```

Target syntax: `all`, `t<N>` (a physical position you configure in the device
book), a raw `MAC`, or an alias/group. The full grammar (`hsi` / `cct` / `power`
/ `bri` / `rgbcw` / `xy` / `gel` / `scene` / `pixel` / `flow`) is in the
[README](README.md).

> **macOS colour caveat** _(unverified)_ — direct commands (`power`, `hsi`,
> `cct`, `scene`) are expected to work, but the **MAC-addressed** modes
> (`rgbcw`, `xy`, `gel`, `pixel`) rely on a MAC that CoreBluetooth hides, so on a
> TL120C those extra colour modes may not work until
> [issue #3](https://github.com/verygeeky/neewer-python/issues/3) is resolved.

### Step 6 (optional): Name your lights

Aliases, positions, and groups live in a TOML device book so you can say `key`
instead of a MAC. Default location:

- **Linux / macOS:** `~/.config/neewer/devices.toml`
- **Windows** _(unverified)_**:** `%USERPROFILE%\.config\neewer\devices.toml`

```toml
[aliases]
key  = "AA:BB:CC:DD:EE:01"
fill = "AA:BB:CC:DD:EE:02"

[positions]      # left-to-right order, for flows and t<N> targets
key  = 1
fill = 2

[groups]
pair = ["key", "fill"]
```

Then `neewer key hsi 30 100 80` or `neewer pair power off`.

> **Heads-up: these lights rotate their MAC on every power-cycle.** Raw-MAC
> aliases go stale. Prefer positions/groups, or the `[units]` mapping (see the
> README) which keys off a stable per-unit id instead.

That's working CLI control. The rest is a bonus.

---

## Part 2 (bonus) — Screen-reactive lighting with LedFx

[LedFx](https://www.ledfx.app/) turns audio (or, with add-ons, your screen) into
real-time colour and streams it over the network as **DMX-over-IP**. The
`neewerd` daemon can receive that stream (Art-Net or sACN/E1.31) and drive the
tubes over BLE, so LedFx effects play on your Neewer lights.

This part needs the **`neewerd` daemon**, not just the CLI library. neewerd is
developed and run on **Linux**; on Windows/macOS treat it as _unverified_.

### Step 1: Install the daemon

```console
pipx install "neewerd[all]"        # or: pip install "neewerd[all]"
```

The `[all]` extra pulls in the optional MQTT / OSC / sACN backends. For Art-Net
only, `pipx install neewerd` is enough.

### Step 2: Write a minimal config

Create `neewerd.toml`. Map each fixture to a DMX address with the **`rgb`**
personality (LedFx emits RGB):

```toml
log_level = "INFO"

[core.positions]                 # your lights, left-to-right
"AA:BB:CC:DD:EE:01" = 1
"AA:BB:CC:DD:EE:02" = 2

[modules.artnet]
enabled = true
host = "0.0.0.0"                 # receive broadcast Art-Net
port = 6454
send_hz = 30.0                  # send-loop tick rate (writes only on change)
min_interval = 0.04             # per-tube floor between BLE writes (~25 Hz)

# One "pixel" per fixture. Each rgb fixture is 3 channels, so pack addresses
# 1, 4, 7, ... — space every fixture by its personality's channel count.
[modules.artnet.patch.t1]
universe = 0
address = 1
personality = "rgb"

[modules.artnet.patch.t2]
universe = 0
address = 4
personality = "rgb"
```

Personalities and their channel footprints:

| Personality | Channels | Use |
|---|---|---|
| `rgb` | 3 (R, G, B) | RGB sources like LedFx |
| `hsi` | 4 (hue-MSB, hue-LSB, sat, intensity) | hue-based consoles |
| `cct` | 3 (intensity, temperature, green/magenta) | white mixing |
| `rgbw` | 4 (R, G, B, W) | RGB + dedicated cold/warm white |

> Space each fixture's `address` by its personality's channel count. Three `rgb`
> fixtures pack at 1, 4, 7. If you switch to `rgbw` (4 ch), space them 1, 5, 9.

### Step 3: Start the daemon

```console
neewerd neewerd.toml
```

It scans, connects your tubes, and listens for Art-Net on UDP 6454. Leave it
running.

### Step 4: Point LedFx at it

1. In LedFx, add a device of type **Art-Net**.
2. Set the **IP** to the machine running neewerd (`127.0.0.1` if same box).
3. **Universe 0**, **start channel 1**.
4. Set the **pixel count** to your number of fixtures (one pixel each), and the
   **channel/RGB order** to `RGB`.
5. **White channel: OFF.** LedFx's "Accurate"/white mode adds a 4th byte per
   pixel — that would shift your DMX addresses. Keep it 3 bytes/pixel to match
   the `rgb` patch. (If you deliberately use the `rgbw` personality instead, turn
   white ON and space patch addresses by 4.)

Play audio (or a screen-capture effect) in LedFx — the tubes should track it.

### Sanity check before blaming LedFx

Confirm the daemon path works on its own first. On **Linux / macOS**, `neewerctl`
talks to the running daemon over its Unix command socket:

```console
# with neewerd running, from another terminal:
neewerctl all hsi 120 100 80        # should turn everything green
```

(On **Windows** the daemon's Unix socket isn't available; sanity-check with the
plain CLI against the lights directly — `neewer all hsi 120 100 80` — after
stopping the daemon so it isn't holding them.)

If the sanity check works but LedFx doesn't, the issue is in the LedFx device
config (IP / universe / channel count / RGB order), not the tubes.

### Tuning for a busy rig

- Too many fixtures stuttering? Raise `min_interval` (fewer writes/tube) — the
  `rgbw` personality is heavier and wants more headroom than `rgb`.
- The daemon has a **per-tube adaptive write governor** that auto-tunes each
  light's write rate to its measured BLE throughput; it's on by default and
  needs no config. The optional `rate_max` / `probe_interval` knobs (see
  `neewerd.example.toml`) only pin the bounds.

---

## Where to go next

- [README](README.md) — the full typed Python API and command grammar.
- [docs/INSTALL-WINDOWS.md](docs/INSTALL-WINDOWS.md) — detailed Windows setup.
- [neewerd](https://github.com/verygeeky/neewerd) — the daemon's own docs
  (MQTT / Home Assistant, OSC, HTTP REST + web UI, sACN).
- [neewer-hardware](https://github.com/verygeeky/neewer-hardware) — the
  protocol and hardware reference.
