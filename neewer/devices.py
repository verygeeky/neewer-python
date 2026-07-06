"""Shared device-identity config: aliases, positions, and groups.

The **single source of truth** for human-friendly device names, read by *both*
the daemon (:mod:`neewer.fleet`, via ``build_core``) and the standalone root
scripts (``ctl.py`` / ``pixel.py`` / ``flow.py``). Before this, the daemon had
``[core.positions]`` in its TOML and the root scripts had nothing but raw MACs;
this reconciles the two so a nickname means the same thing everywhere.

Config file (all sections optional; a missing file is not an error — you just
get an empty book)::

    # ~/.config/neewer/devices.toml   (override with $NEEWER_DEVICES)
    [aliases]                 # nickname -> MAC
    key  = "AA:BB:CC:DD:EE:01"
    fill = "AA:BB:CC:DD:EE:02"

    [positions]               # left-to-right order for flows; keyed by alias OR MAC
    key  = 4
    fill = 1

    [groups]                  # ordered, nestable: members may be aliases, MACs,
    keys    = ["key", "fill"] #   or other group names
    all_rgb = ["keys", "AA:BB:CC:DD:EE:03"]

    [units]                   # networkId (advert &XXXXXXXX suffix) -> display name
    "00900001" = "Key Left"   #   the fixture's durable unit-id; survives MAC rotation
    "01200001" = "Back Wall"

Design notes:

* Pure standard library (``tomllib`` only) so the copy-anywhere root scripts can
  import it without dragging in ``bleak`` or the rest of the package.
* MACs are normalised to upper-case; alias/group names are matched
  case-insensitively (stored lower-case).
* Group expansion is recursive with a cycle guard, and de-duplicates while
  preserving first-seen order — so ``all_rgb`` above yields three MACs even
  though ``keys`` is nested inside it.
"""
from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

#: A loose MAC matcher — six colon- or dash-separated hex byte pairs. Deliberately
#: permissive; we only use it to tell "this token is already an address" apart
#: from "this token is a name to look up".
_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}$")


def is_mac(token: str) -> bool:
    """Return ``True`` if ``token`` already looks like a BLE MAC address."""
    return bool(_MAC_RE.match(token.strip()))


def normalize_netid(netid) -> str | None:
    """Canonicalise a fixture ``networkId`` to lowercase 8-hex-digit form.

    Accepts either an ``int`` (the raw 32-bit id) or a string in any of the
    shapes the id appears in — the advert suffix (``"&00900002"``), a bare hex
    string (``"00900002"``), or a ``0x``-prefixed literal (``"0x00900002"``) —
    and returns e.g. ``"00900002"``. Case-insensitive, zero-padded to 8 digits.
    Returns ``None`` if the token isn't parseable as hex (so callers can treat a
    junk id the same as an absent one).
    """
    if netid is None:
        return None
    if isinstance(netid, int):
        return f"{netid & 0xFFFFFFFF:08x}"
    token = str(netid).strip().lstrip("&")
    if token.lower().startswith("0x"):
        token = token[2:]
    if not token:
        return None
    try:
        value = int(token, 16)
    except ValueError:
        return None
    return f"{value & 0xFFFFFFFF:08x}"


def config_path(explicit: str | None = None) -> Path:
    """Resolve the devices-config path by precedence.

    ``explicit`` arg > ``$NEEWER_DEVICES`` > ``$XDG_CONFIG_HOME/neewer/devices.toml``
    > ``~/.config/neewer/devices.toml``. The returned path may not exist — callers
    treat "no file" as "empty config".
    """
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("NEEWER_DEVICES")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "neewer" / "devices.toml"


class DeviceBook:
    """Parsed aliases / positions / groups with name<->MAC resolution.

    Construct via :func:`load`; the constructor takes already-parsed raw dicts so
    it stays trivially testable without touching the filesystem.
    """

    def __init__(self, aliases=None, positions=None, groups=None, models=None,
                 units=None):
        # nickname (lower) -> MAC (upper)
        self.aliases: dict[str, str] = {
            str(k).lower(): str(v).upper() for k, v in (aliases or {}).items()
        }
        # group name (lower) -> ordered list of raw member tokens (verbatim)
        self.groups: dict[str, list[str]] = {
            str(k).lower(): list(v) for k, v in (groups or {}).items()
        }
        # position map keyed by MAC (upper). Keys given as an alias are resolved
        # to their MAC here so the daemon can look up purely by address.
        self.positions: dict[str, int] = {}
        for key, pos in (positions or {}).items():
            mac = self.aliases.get(str(key).lower(), str(key).upper())
            self.positions[mac] = int(pos)
        # declared fixture model per device (MAC upper -> model name); an alias key
        # is resolved to its MAC. Used for per-model capability gating (see models.py).
        self.models: dict[str, str] = {}
        for key, model in (models or {}).items():
            mac = self.aliases.get(str(key).lower(), str(key).upper())
            self.models[mac] = str(model)
        # unit-name map keyed by normalised networkId (lowercase 8-hex). Maps a
        # fixture's durable ``&networkId`` unit-id (advert suffix) to a friendly
        # display label, so a rig can name tubes stably across BLE-MAC rotation.
        # Un-parseable keys are dropped rather than raising.
        self.units: dict[str, str] = {}
        for key, label in (units or {}).items():
            netid = normalize_netid(key)
            if netid is not None:
                self.units[netid] = str(label)

    # ---- resolution ------------------------------------------------------
    def resolve_one(self, token: str) -> str | None:
        """Resolve a single token (alias or MAC) to one MAC, or ``None``.

        Groups are *not* resolved here (they expand to many); use :meth:`expand`
        for the general case. This is the helper the single-target root scripts
        (``ctl.py probe/send``, ``pixel.py``) use.
        """
        token = token.strip()
        if token.lower() in self.aliases:
            return self.aliases[token.lower()]
        if is_mac(token):
            return token.upper()
        return None

    def expand(self, token: str, _seen: frozenset[str] = frozenset()) -> list[str]:
        """Expand a token to an ordered, de-duplicated list of MACs.

        Resolves groups (recursively, nested groups allowed), then aliases, then
        bare MACs. Unknown names yield an empty list. Cycles are broken by the
        ``_seen`` guard so ``a=[b]`` / ``b=[a]`` can't loop forever.
        """
        token = token.strip()
        low = token.lower()
        if low in self.groups and low not in _seen:
            seen = _seen | {low}
            out: list[str] = []
            for member in self.groups[low]:
                for mac in self.expand(member, seen):
                    if mac not in out:
                        out.append(mac)
            return out
        one = self.resolve_one(token)
        return [one] if one else []

    def model_for(self, mac: str) -> str | None:
        """The declared fixture model for a MAC, or ``None`` if not configured."""
        return self.models.get(mac.upper())

    def unit_name(self, netid) -> str | None:
        """The configured display label for a fixture ``networkId``, or ``None``.

        ``netid`` may be an ``int`` or any string form of the id (``"00900002"``,
        ``"&00900002"``, ``"0x00900002"``, upper- or lower-case) — it is
        normalised the same way the ``[units]`` keys were. Returns ``None`` for an
        un-parseable id or one with no configured label.
        """
        key = normalize_netid(netid)
        if key is None:
            return None
        return self.units.get(key)

    def __bool__(self) -> bool:
        """Truthy only if the book actually carries any config."""
        return bool(self.aliases or self.positions or self.groups
                    or self.models or self.units)


def load(path: str | None = None) -> DeviceBook:
    """Load the devices config, returning an (possibly empty) :class:`DeviceBook`.

    A missing file is fine — it yields an empty book. A malformed file raises,
    because a typo in your names shouldn't fail silently.
    """
    p = config_path(path)
    if not p.exists():
        return DeviceBook()
    with open(p, "rb") as f:
        data = tomllib.load(f)
    return DeviceBook(
        aliases=data.get("aliases"),
        positions=data.get("positions"),
        groups=data.get("groups"),
        models=data.get("models"),
        units=data.get("units"),
    )
