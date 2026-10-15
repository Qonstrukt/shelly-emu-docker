"""Shelly Pro 3EM Modbus register map and Modbus TCP frame handling (pure functions).

Register addresses follow the Shelly docs (EM at 31000+, EMData at 31160+); on the wire
they are input registers at ``address - 30000``. 32-bit values use the CDAB word order
(low word first) like a real Pro 3EM. Only FC04 is served; FC03 answers *illegal data
address* as the real device does.
"""

from __future__ import annotations

import logging
import struct
from collections.abc import Callable

from .state import EmState

_LOGGER = logging.getLogger(__name__)

FC_READ_COILS = 0x01
FC_READ_HOLDING = 0x03
FC_READ_INPUT = 0x04
FC_WRITE_COIL = 0x05
FC_WRITE_COILS = 0x0F
EX_ILLEGAL_FUNCTION = 0x01
EX_ILLEGAL_ADDRESS = 0x02
EX_ILLEGAL_VALUE = 0x03

# Pro Output Add-on relay (switch:100). Shelly maps switch coils as 100 + switch_id * 10
# (switch:0 -> 100, switch:100 -> 1100); the input-register block of the first switch
# component is at docs address 33000, wire address 3000.
ADDON_SWITCH_ID = 100
ADDON_COIL = 100 + ADDON_SWITCH_ID * 10  # 1100
ADDON_SWITCH_REG = 3000
# EM / EMData input registers live here; reads elsewhere are worth a log line.
EM_BLOCK = range(1000, 1241)

RegisterMap = dict[int, int]


def put_float(regs: RegisterMap, addr: int, value: float) -> None:
    bits = struct.unpack("<I", struct.pack("<f", float(value)))[0]
    regs[addr] = bits & 0xFFFF
    regs[addr + 1] = bits >> 16


def put_u32(regs: RegisterMap, addr: int, value: int) -> None:
    value &= 0xFFFFFFFF
    regs[addr] = value & 0xFFFF
    regs[addr + 1] = value >> 16


def put_ascii(regs: RegisterMap, addr: int, text: str, n_regs: int) -> None:
    data = text.encode("ascii", "replace")
    for i in range(n_regs):
        lo = data[2 * i] if 2 * i < len(data) else 0
        hi = data[2 * i + 1] if 2 * i + 1 < len(data) else 0
        regs[addr + i] = (hi << 8) | lo


def get_float(regs: RegisterMap, addr: int) -> float:
    """Inverse of :func:`put_float` (for tests and tools)."""
    bits = (regs.get(addr + 1, 0) << 16) | regs.get(addr, 0)
    return struct.unpack("<f", struct.pack("<I", bits))[0]


def build_register_map(
    em: EmState, mac: str, model_code: str, now: float, addon_output: bool | None = None
) -> RegisterMap:
    """``addon_output`` is the Pro Output Add-on relay state, or ``None`` without the add-on."""
    regs: RegisterMap = {}
    # Identity block at 0: MAC (6 regs) + model (8 regs), as captured from a real Pro 3EM.
    put_ascii(regs, 0, mac, 6)
    put_ascii(regs, 6, model_code, 8)

    ts = int(now) if now > 1600000000 else 0
    total_current = sum(p.current for p in em.phase)
    total_act = sum(p.act_power for p in em.phase)
    total_aprt = sum(p.aprt_power for p in em.phase)

    # EM (docs: 31000+, on the wire 1000+)
    put_u32(regs, 1000, ts)
    for a in range(1002, 1007):
        regs[a] = 0
    put_float(regs, 1007, 0.0 if em.n_current is None else em.n_current)
    regs[1009] = 0
    regs[1010] = 0
    put_float(regs, 1011, total_current)
    put_float(regs, 1013, total_act)
    put_float(regs, 1015, total_aprt)
    for i, p in enumerate(em.phase):
        base = 1020 + 20 * i
        put_float(regs, base + 0, p.voltage)
        put_float(regs, base + 2, p.current)
        put_float(regs, base + 4, p.act_power)
        put_float(regs, base + 6, p.aprt_power)
        put_float(regs, base + 8, p.pf)
        regs[base + 10] = 0
        regs[base + 11] = 0
        regs[base + 12] = 0
        put_float(regs, base + 13, p.freq)

    if addon_output is not None:
        regs[ADDON_SWITCH_REG] = 1 if addon_output else 0

    # EMData (docs: 31160+, on the wire 1160+)
    put_u32(regs, 1160, ts)
    put_float(regs, 1162, sum(p.energy for p in em.phase))
    put_float(regs, 1164, sum(p.ret_energy for p in em.phase))
    for i, p in enumerate(em.phase):
        base = 1170 + 20 * i
        put_float(regs, base + 0, p.energy)
        put_float(regs, base + 2, p.energy)
        put_float(regs, base + 4, p.ret_energy)
        put_float(regs, base + 6, p.ret_energy)
        put_float(regs, base + 8, 0.0)
        put_float(regs, base + 10, 0.0)
        put_float(regs, base + 12, p.energy)
        put_float(regs, base + 14, p.ret_energy)
    return regs


def handle_frame(
    frame: bytes,
    regs: RegisterMap,
    addon_output: bool | None = None,
    write_coil: Callable[[bool], None] | None = None,
) -> bytes:
    """Answer one Modbus TCP ADU (MBAP header + PDU). ``frame`` is a complete frame.

    With the Pro Output Add-on (``addon_output`` not ``None``) coil 1100 reflects the relay:
    FC01 reads it, FC05 / FC15 write it through ``write_coil``. Without the add-on the coil
    functions answer *illegal function* like a bare Pro 3EM.

    Every rejected request is logged at WARNING, coil traffic at INFO and FC04 reads outside
    the EM/EMData block at INFO, so unexpected controller behaviour shows up in the log.
    """
    unit = frame[6]
    fc = frame[7]
    addon = addon_output is not None
    address = count = None
    if len(frame) >= 12:
        address, count = struct.unpack(">HH", frame[8:12])

    def exception(code: int, why: str) -> bytes:
        _LOGGER.warning(
            "modbus rejected: unit %d FC%02X addr %s count/value %s -> exception %02X (%s)",
            unit, fc, address, count if count is None else f"0x{count:04X}" if fc == FC_WRITE_COIL else count, code, why,
        )
        return bytes([fc | 0x80, code])

    if address is None or count is None:
        pdu = exception(EX_ILLEGAL_FUNCTION, "short frame")
    elif fc in (FC_READ_INPUT, FC_READ_HOLDING):
        if fc == FC_READ_HOLDING:
            # A real Pro 3EM answers holding-register reads with "illegal data address".
            pdu = exception(EX_ILLEGAL_ADDRESS, "holding registers are not served")
        elif count < 1 or count > 125:
            pdu = exception(EX_ILLEGAL_ADDRESS, "bad count")
        else:
            if address not in EM_BLOCK or address + count - 1 not in EM_BLOCK:
                _LOGGER.info("modbus FC04 read outside the EM block: unit %d addr %d count %d", unit, address, count)
            body = bytearray([fc, count * 2])
            for i in range(count):
                body += struct.pack(">H", regs.get(address + i, 0))
            pdu = bytes(body)
    elif fc == FC_READ_COILS and addon:
        if count < 1 or count > 2000 or address > ADDON_COIL or address + count <= ADDON_COIL:
            pdu = exception(EX_ILLEGAL_ADDRESS, f"range does not cover coil {ADDON_COIL}")
        else:
            n_bytes = (count + 7) // 8
            body = bytearray(2 + n_bytes)
            body[0] = fc
            body[1] = n_bytes
            if addon_output:
                bit = ADDON_COIL - address
                body[2 + bit // 8] |= 1 << (bit % 8)
            pdu = bytes(body)
            _LOGGER.info("modbus FC01 read coils: unit %d addr %d count %d -> coil %d %s",
                         unit, address, count, ADDON_COIL, "on" if addon_output else "off")
    elif fc == FC_WRITE_COIL and addon:
        value = count
        if address != ADDON_COIL:
            pdu = exception(EX_ILLEGAL_ADDRESS, f"only coil {ADDON_COIL} exists")
        elif value not in (0xFF00, 0x0000):
            pdu = exception(EX_ILLEGAL_VALUE, "coil value must be 0xFF00 or 0x0000")
        else:
            on = value == 0xFF00
            _LOGGER.info("modbus FC05 write coil: unit %d coil %d <- %s", unit, address, "on" if on else "off")
            if write_coil is not None:
                write_coil(on)
            pdu = bytes(frame[7:12])  # echo the request
    elif fc == FC_WRITE_COILS and addon:
        if address != ADDON_COIL or count != 1 or len(frame) < 14:
            pdu = exception(EX_ILLEGAL_ADDRESS, f"only coil {ADDON_COIL}, count 1, is writable")
        else:
            on = (frame[13] & 0x01) != 0
            _LOGGER.info("modbus FC15 write coils: unit %d coil %d <- %s", unit, address, "on" if on else "off")
            if write_coil is not None:
                write_coil(on)
            pdu = bytes(frame[7:12])
    else:
        pdu = exception(EX_ILLEGAL_FUNCTION, "unsupported function" + ("" if addon else " (no output add-on configured)"))
    return frame[0:2] + b"\x00\x00" + struct.pack(">HB", len(pdu) + 1, unit) + pdu


def split_frames(buf: bytearray) -> list[bytes]:
    """Pop complete ADUs off ``buf``. Raises ``ValueError`` on a bad length field."""
    frames: list[bytes] = []
    while len(buf) >= 7:
        length = (buf[4] << 8) | buf[5]
        if length < 2 or length > 260:
            raise ValueError("bad MBAP length")
        if len(buf) < 6 + length:
            break
        frames.append(bytes(buf[: 6 + length]))
        del buf[: 6 + length]
    return frames
