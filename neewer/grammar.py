"""The string command grammar — an opt-in convenience *over* the typed API.

This is the historical ``<target> <action> [args...]`` line grammar that the
daemon's wire modules (socket / OSC / HTTP / MQTT) speak. It lives **outside the
core library contract on purpose**: :class:`neewer.fleet.Fleet`'s typed methods
(:meth:`~neewer.fleet.Fleet.set_hsi`, :meth:`~neewer.fleet.Fleet.power`, …) are
the real API, and :func:`dispatch` is a thin shim that parses one line, coerces
its string arguments to the right types, and calls the matching typed method. A
transport that already holds structured arguments (Art-Net, an MCP tool, the
console UI) should call the typed methods directly and skip this module entirely.

Because it is opt-in, importing :mod:`neewer.protocol` — the pure frame/model
layer — does not pull the grammar in, and neither does the ``Fleet`` class body
(``Fleet.dispatch`` imports it lazily). Nothing here imports ``bleak``.

The grammar
-----------
A command is a single whitespace-separated line::

    <target> <action> [args...]

Target
    * ``all``            — every connected tube, in physical-position order.
    * ``t<N>``           — the tube at 1-based physical position N (e.g. ``t1``).
    * an alias / group   — from the shared device book.
    * ``AA:BB:CC:..``    — a specific tube by MAC address.

Actions
    ``power`` · ``hsi`` · ``cct`` · ``bri`` · ``scene`` · ``pixel`` · ``rgbcw`` ·
    ``xy`` · ``gel`` · ``identify`` · ``raw`` and the whole-daemon verbs
    ``flow`` · ``stop`` · ``query`` · ``state`` (which carry no leading target). A
    consumer may register further verbs (e.g. a daemon's ``preset``) via
    :meth:`~neewer.fleet.Fleet.register_verb`. See each
    :class:`~neewer.protocol.commands` dataclass for the canonical argument order.

:func:`parse` only *shapes* a line into a :class:`Command`; the per-action
argument coercion and the fleet call live in :func:`dispatch`, so error messages
can reference live state (which tubes exist, etc.).
"""
from __future__ import annotations

from dataclasses import dataclass

from . import effects
from .errors import UnknownAction
from .protocol import frames

#: Verbs that stand alone with no target word in front of them (an optional
#: target may follow them instead, e.g. ``state t1``). Consumer-registered verbs
#: (see :meth:`neewer.fleet.Fleet.register_verb`) are handled separately, before
#: parsing, and need not appear here.
TARGETLESS_ACTIONS = ("flow", "stop", "query", "state")


@dataclass(frozen=True)
class Command:
    """A parsed command line. Immutable so it can be passed around freely."""

    target: str
    action: str
    args: list[str]


def parse(line: str) -> Command:
    """Parse a command line into a :class:`Command`.

    Raises:
        ValueError: if the line is empty or names a target with no action.
    """
    parts = line.strip().split()
    if not parts:
        raise ValueError("empty command")

    # Whole-daemon verbs ("flow ...", "stop") carry no target word; we normalise
    # their target to "all" so downstream code has one shape to reason about.
    if parts[0] in TARGETLESS_ACTIONS:
        return Command(target="all", action=parts[0], args=parts[1:])

    if len(parts) < 2:
        raise ValueError(f"command needs an action after the target: {line!r}")

    return Command(target=parts[0], action=parts[1], args=parts[2:])


def osc_to_command(address: str, args) -> str:
    """Map an OSC address + args to a command line.

    The OSC address path becomes the leading words of the command and the OSC
    arguments become the trailing words::

        /neewer/all/hsi    240 100 80   ->  'all hsi 240 100 80'
        /neewer/t1/bri     80           ->  't1 bri 80'
        /neewer/all/flow   palette ...  ->  'all flow palette ...'

    A leading ``neewer`` namespace segment is stripped if present.
    """
    segments = [s for s in address.strip("/").split("/") if s]
    if segments and segments[0] == "neewer":
        segments = segments[1:]

    if isinstance(args, str):
        argstr = args
    else:
        argstr = " ".join(str(a) for a in args)

    return (" ".join(segments) + " " + argstr).strip()


# --- dispatch: line -> typed method call ----------------------------------
async def dispatch(fleet, line: str) -> str:
    """Parse and execute one command line against ``fleet``; return its reply.

    This is the thin string front-end to the typed API: it shapes the line, maps
    the action to the matching :class:`~neewer.fleet.Fleet` method, coerces the
    string arguments to that method's types, and calls it. Every branch delegates
    to a typed method — no frame is built here.

    Raises a :mod:`neewer.errors` exception (``UnknownAction``/``UnknownTarget``/
    ``Unsupported``/``UnknownPreset``/``UnknownEffect``) or ``ValueError`` on
    malformed arguments; a transport maps those to a status. Handled outcomes come
    back as the method's human-readable string.
    """
    # Consumer-registered verbs (e.g. a daemon's presets) intercept first, with the
    # raw trailing words — so a consumer can add whole-line verbs the core grammar
    # doesn't know, and the library keeps no opinion on that policy. See
    # :meth:`neewer.fleet.Fleet.register_verb`.
    parts = line.strip().split()
    verbs = getattr(fleet, "verbs", None)
    if parts and verbs and parts[0] in verbs:
        return await verbs[parts[0]](fleet, parts[1:])

    cmd = parse(line)
    action, args, target = cmd.action, cmd.args, cmd.target

    # Whole-daemon verbs first (no target, or the target follows the verb).
    if action == "stop":
        await fleet.cancel_effect()     # manual "stop" cancels any running animation
        return "ok stopped"
    if action == "flow":
        mode, opts = effects.parse_opts(args)
        return await fleet.flow(mode, **opts)
    if action == "state":
        return fleet.render_state(args[0] if args else target)
    if action == "query":
        return await fleet.query(args[0] if args else target)

    # Colour / effect actions with bespoke argument shapes.
    if action == "pixel":
        return await fleet.pixel(target, args)
    if action == "rgbcw":
        if not args:
            raise ValueError("need at least 1 arg: <bri> [r] [g] [b] [c] [w]")
        bri, r, g, b, c, w = _ints(args, ("bri", "r", "g", "b", "c", "w"),
                                   required=1, total=6, defaults=(0, 0, 0, 0, 0, 0))
        return await fleet.set_rgbcw(target, bri, r, g, b, c, w)
    if action == "xy":
        if len(args) < 3:
            raise ValueError("need 3 args: <bri> <x> <y>")
        try:
            bri, x, y = int(args[0]), float(args[1]), float(args[2])
        except ValueError as exc:
            raise ValueError(f"xy expects <bri:int> <x:float> <y:float>: {exc}") from exc
        return await fleet.set_xy(target, bri, x, y)
    if action == "gel":
        if len(args) < 3:
            raise ValueError("need at least 3 args: <hue> <sat> <bri> [brand] [gelNo]")
        hue, sat, bri = _ints(args[:3], ("hue", "sat", "bri"),
                              required=3, total=3, defaults=())
        brand = _parse_gel_brand(args[3]) if len(args) > 3 else frames.GEL_BRAND_ROSCO
        try:
            gel_no = int(args[4]) if len(args) > 4 else 0
        except ValueError as exc:
            raise ValueError(f"gel number must be an integer: {exc}") from exc
        return await fleet.set_gel(target, hue, sat, bri, brand, gel_no)
    if action == "scene":
        values = _ints(args, ("effect", "params..."), required=1, total=None, defaults=())
        return await fleet.scene(target, values[0], *values[1:])
    if action == "identify":
        return await fleet.identify(target)

    # Direct-frame family.
    if action == "power":
        return await fleet.power(target, bool(args) and args[0] in ("on", "1", "true"))
    if action == "hsi":
        hue, sat, bri = _ints(args, ("hue", "sat", "bri"),
                              required=1, total=3, defaults=(0, 100, 100))
        return await fleet.set_hsi(target, hue, sat, bri)
    if action == "cct":
        bri, temp, gm = _ints(args, ("bri", "temp", "gm"), required=2, total=3,
                              defaults=(100, frames.CCT_MIN, frames.GM_NEUTRAL))
        return await fleet.set_cct(target, bri, temp, gm)
    if action == "bri":
        (bri,) = _ints(args, ("bri",), required=1, total=1, defaults=(100,))
        return await fleet.set_bri(target, bri)
    if action == "raw":
        return await fleet.raw(target, " ".join(args))

    raise UnknownAction(action)


# --- string-argument coercion (a grammar concern, not a library one) ------
def _parse_gel_brand(token: str) -> int:
    """Parse a gel ``brand`` word — ``rosco``/``lee`` (case-insensitive) or ``1``/``2``.

    Returns :data:`.frames.GEL_BRAND_ROSCO` / :data:`.frames.GEL_BRAND_LEE`. Raises
    ``ValueError`` (→ http 400) on anything else.
    """
    key = str(token).strip().lower()
    if key in ("rosco", "1"):
        return frames.GEL_BRAND_ROSCO
    if key in ("lee", "2"):
        return frames.GEL_BRAND_LEE
    raise ValueError(f"gel brand must be rosco/lee or 1/2, got {token!r}")


def _ints(args, names, required: int, total: int | None, defaults) -> list[int]:
    """Coerce command args to ints with friendly, action-aware error messages.

    Args:
        args: the raw string args from the command line.
        names: argument names, used only to build error messages.
        required: minimum number of args that must be present.
        total: maximum number of args to consume, or ``None`` for unlimited.
        defaults: values to backfill optional positions up to ``total``.

    Returns the parsed ints (length ``total`` when ``total`` is set, else as many
    as were supplied).
    """
    if len(args) < required:
        expected = " ".join(f"<{n}>" for n in names[:required])
        raise ValueError(f"need at least {required} arg(s): {expected}")
    take = args if total is None else args[:total]
    try:
        values = [int(x) for x in take]
    except ValueError as exc:
        raise ValueError(f"expected integer arguments ({', '.join(names)}): {exc}") from exc
    if total is not None:
        values += list(defaults[len(values):])
    return values
