"""Decode status notifications the lights push on their notify characteristic.

The lights reply to queries (and sometimes volunteer state, e.g. a battery push on
connect) with frames in the same ``[0x78, opcode, len, payload…, checksum]`` shape
as commands — but the opcode is a *reply* code. This module maps the reply codes we
understand into a flat dict of state fields; anything unknown is preserved as raw hex
so nothing is silently dropped.

Reply codes and field offsets follow the response table in the companion
protocol documentation (https://github.com/verygeeky/neewer-hardware), verified
against real hardware where noted.
"""
from __future__ import annotations

PREFIX = 0x78

# Reply opcodes (data[1]).
R_VERSION_DIRECT = 0x00     # version, direct query
R_POWER_DIRECT = 0x02       # power status, direct query
R_STATE_MAC = 0x04          # device power/state, by-MAC query
R_BATTERY = 0x05            # battery %, by-MAC query (or pushed on connect)
R_VERSION_MAC = 0x08        # version, by-MAC query
R_TEMP_FAN = 0x12           # temperature / fan-mode
R_STREAMER_SUPPORT = 0x17   # streamer-support reply (TL60): value at [9], 1 = supported
R_ACK = 0x7F                # generic ACK to a provisioning/grouping command


def _mac(data: bytes, start: int = 3) -> str:
    return ":".join(f"{b:02x}" for b in data[start:start + 6])


def parse(data: bytes) -> dict:
    """Decode one notification frame into a state dict.

    Returns ``{'raw': '<hex>'}`` for frames that aren't ours, are too short, or use
    a reply code we don't decode yet — callers can still surface the bytes.
    """
    if len(data) < 3 or data[0] != PREFIX:
        return {"raw": data.hex(" ")}

    op = data[1]
    out: dict = {}
    try:
        if op == R_VERSION_DIRECT:
            out["version"] = f"{data[5]}.{data[6]}.{data[7]}"
        elif op == R_POWER_DIRECT:
            out["power"] = "on" if data[3] == 1 else "off"
        elif op == R_STATE_MAC:
            out["mac"] = _mac(data)
            out["mode"] = data[9]
            out["power"] = "on" if data[10] == 1 else "off"
        elif op == R_BATTERY:
            out["mac"] = _mac(data)
            pct = data[9] & 0xFF
            out["battery_raw"] = pct
            if pct <= 100:
                out["battery"] = pct           # normal 0–100 percentage
            else:
                # e.g. the TL120C reports 0xF0 here — a mains/external-power flag,
                # not a percentage.
                out["power_source"] = "external"
        elif op == R_VERSION_MAC:
            out["version"] = f"{data[11]}.{data[12]}.{data[13]}"
        elif op == R_TEMP_FAN:
            out["temp_c"] = (data[9] & 0xFF) - 50
        elif op == R_STREAMER_SUPPORT:
            # 78 17 07 <MAC6> <supported> ck — the TL60 answering the 0xC4 query;
            # 1 = the realtime streamer (0xC0/0xBF) is available.
            out["mac"] = _mac(data)
            out["streamer"] = bool(data[9])
        elif op == R_ACK:
            # 78 7f 08 <MAC6> <acked-op> <status> ck — a tube acknowledging a
            # provisioning/grouping command (0x9F/0x8C). data[2]==8 for this form.
            out["mac"] = _mac(data)
            out["ack_op"] = data[9]
            out["ack_status"] = data[10]
        else:
            out["raw"] = data.hex(" ")
    except IndexError:
        # Frame shorter than its reply code implies (often MTU truncation).
        return {"raw": data.hex(" ")}
    return out
