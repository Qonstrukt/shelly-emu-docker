"""Shelly Pro 3EM Modbus register map and Modbus TCP frame handling (pure functions).

Register addresses follow the Shelly docs (EM at 31000+, EMData at 31160+); on the wire
they are input registers at ``address - 30000``. 32-bit values use the CDAB word order
(low word first) like a real Pro 3EM. Only FC04 is served; FC03 answers *illegal data
address* as the real device does.
"""

from __future__ import annotations

import struct

from .state import EmState

FC_READ_HOLDING = 0x03
FC_READ_INPUT = 0x04
EX_ILLEGAL_FUNCTION = 0x01
EX_ILLEGAL_ADDRESS = 0x02

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


def build_register_map(em: EmState, mac: str, model_code: str, now: float) -> RegisterMap:
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


def handle_frame(frame: bytes, regs: RegisterMap) -> bytes:
    """Answer one Modbus TCP ADU (MBAP header + PDU). ``frame`` is a complete frame."""
    unit = frame[6]
    fc = frame[7]
    if fc not in (FC_READ_HOLDING, FC_READ_INPUT) or len(frame) < 12:
        pdu = bytes([fc | 0x80, EX_ILLEGAL_FUNCTION])
    else:
        address, count = struct.unpack(">HH", frame[8:12])
        if count < 1 or count > 125 or fc == FC_READ_HOLDING:
            pdu = bytes([fc | 0x80, EX_ILLEGAL_ADDRESS])
        else:
            body = bytearray([fc, count * 2])
            for i in range(count):
                body += struct.pack(">H", regs.get(address + i, 0))
            pdu = bytes(body)
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
