# Installing on Windows

> **Status: draft.** The library is developed and continuously tested on Linux;
> these Windows steps are believed-correct but not yet validated end-to-end on a
> real Windows box. Anything marked *(verify)* is exactly the part we want
> feedback on — please [open an issue](https://github.com/verygeeky/neewer-python/issues)
> with what worked and what didn't.

## Requirements

- **Windows 10** Fall Creators Update (build 16299) or later, or **Windows 11**
  — the BLE backend ([bleak](https://github.com/hbldh/bleak)) uses the WinRT
  Bluetooth API introduced there.
- A **Bluetooth 4.0+ (LE)** adapter — built-in or USB. No special drivers: the
  library talks through the standard Windows Bluetooth stack (no Zadig, no
  WinUSB replacement, no pairing).
- **Python 3.11 or newer**, 64-bit.

## 1. Install Python

Either from [python.org](https://www.python.org/downloads/) (tick **"Add
python.exe to PATH"** in the installer), or with winget:

```console
winget install --id Python.Python.3.12
```

Verify in a new terminal:

```console
py --version
```

## 2. Install the library + CLI

Recommended — [pipx](https://pipx.pypa.io/), which puts the `neewer` command on
your PATH in an isolated environment:

```console
py -m pip install --user pipx
py -m pipx ensurepath
```

Open a **new** terminal (so the PATH change applies), then:

```console
pipx install neewer
```

Or with plain pip:

```console
py -m pip install neewer
```

The Windows BLE backend (`winrt-*` wheels) is pulled in automatically by the
platform markers — there is nothing Bluetooth-specific to install by hand.

If after a plain-pip install the terminal doesn't recognise `neewer`, the
Scripts directory isn't on your PATH; the module form always works:

```console
py -m neewer.cli scan
```

## 3. Put the light in Bluetooth mode

- The tube's physical **2.4G / BT switch** must be on **BT**. In 2.4G mode the
  light is invisible to every Bluetooth client, this library included.
- **Close the NEEWER app** on your phone (or move the phone away). A light
  accepts **one** Bluetooth central at a time — if the app holds it, it stops
  advertising and nothing else can find it.
- Do **not** pair the light in Windows Settings ("Add a device"). The protocol
  is pairing-free; a light paired at the OS level may be held by Windows and
  become unconnectable from the library *(verify — if you paired one earlier,
  remove it in Settings → Bluetooth & devices before testing)*.

## 4. First contact

```console
neewer scan
```

Expect one line per light in range, advertising as `NW-<8 digits>&<8 hex>`
(e.g. `NW-20240047&00000000` — the digits identify the model, and the library
decodes that automatically).

Then prove control works:

```console
neewer all hsi 240 100 100     # everything blue
neewer all cct 100 5600        # neutral white, full brightness
neewer all power off
```

## 5. Use it from Python

```python
import asyncio
from neewer import Fleet

async def main():
    async with Fleet() as fleet:               # scan + connect everything in range
        await fleet.set_hsi("all", 240, 100, 80)

asyncio.run(main())
```

The full typed API and the string grammar are documented in the
[README](../README.md).

## 6. Optional: the device book

Aliases, positions, and groups live in a TOML file. On Windows the default
location is:

```
%USERPROFILE%\.config\neewer\devices.toml
```

(Yes, a dotfile path — the library uses the same location on every OS. Set the
`NEEWER_DEVICES` environment variable to put it somewhere else.)

```toml
[aliases]
key  = "AA:BB:CC:DD:EE:01"
fill = "AA:BB:CC:DD:EE:02"

[positions]
key  = 1
fill = 2
```

One Windows-relevant caveat: these lights **rotate their MAC address on every
power-cycle**, so raw-MAC aliases go stale. Prefer positions/groups, target by
advertised name, or use the `[units]` mapping (see `README`) which survives
rotation.

## Windows-specific notes

- The BlueZ zombie-link self-heal that runs at `Fleet` startup is Linux-only;
  on Windows it is skipped silently. Nothing replaces it — if a light gets
  stuck "connected" after a hard kill, toggle the light's power.
- Requires `neewer >= 0.1.2` for clean behaviour with injected/mock transports.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `neewer scan` finds nothing | Bluetooth off; light in 2.4G mode; light held by the phone app; light paired in Windows Settings *(verify)* |
| `neewer` not recognised | Scripts dir not on PATH — use `py -m neewer.cli …` or pipx |
| `ImportError: winrt…` | Python too old (< 3.11) or 32-bit; Windows build < 16299 |
| Connects but commands are slow / time out | Marginal adapter or distance — cheap USB BLE dongles struggle; try the built-in radio or move closer |

## Known-untested on Windows *(please report)*

- Everything above end-to-end (this is a draft).
- Multi-light fleets (>3 concurrent connections) on the WinRT stack.
- The `neewerd` daemon: it is developed and deployed on Linux; its command
  socket and packaging assume a Unix-y host. The **library** is the supported
  surface on Windows for now.
