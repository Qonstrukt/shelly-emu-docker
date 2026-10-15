#!/usr/bin/env python3
"""Read a few Shelly Pro 3EM registers over Modbus TCP, the way Sigenergy does.

Usage: python3 tools/modbus_smoke.py <ip> [port] [unit]
No third-party dependencies.
"""
import socket
import struct
import sys

host = sys.argv[1]
port = int(sys.argv[2]) if len(sys.argv) > 2 else 502
unit = int(sys.argv[3]) if len(sys.argv) > 3 else 7


def read_input(sock, address, count, tid=1):
    pdu = struct.pack(">BHH", 0x04, address, count)
    sock.sendall(struct.pack(">HHHB", tid, 0, len(pdu) + 1, unit) + pdu)
    hdr = sock.recv(7)
    _, _, length, _ = struct.unpack(">HHHB", hdr)
    body = b""
    while len(body) < length - 1:
        body += sock.recv(length - 1 - len(body))
    if body[0] & 0x80:
        raise RuntimeError(f"Modbus exception {body[1]}")
    return struct.unpack(f">{body[1] // 2}H", body[2:])


def f32(regs, i):
    """Shelly word order: low word first (CDAB)."""
    return struct.unpack(">f", struct.pack(">HH", regs[i + 1], regs[i]))[0]


with socket.create_connection((host, port), timeout=5) as s:
    regs = read_input(s, 1011, 6)
    print(f"total current {f32(regs, 0):.3f} A, active power {f32(regs, 2):.1f} W, apparent {f32(regs, 4):.1f} VA")
    for phase, base in (("A", 1020), ("B", 1040), ("C", 1060)):
        regs = read_input(s, base, 15)
        print(f"phase {phase}: {f32(regs, 0):.1f} V, {f32(regs, 2):.3f} A, {f32(regs, 4):.1f} W, pf {f32(regs, 8):.2f}, {f32(regs, 13):.1f} Hz")
    regs = read_input(s, 1162, 4)
    print(f"energy total {f32(regs, 0):.1f} Wh, returned {f32(regs, 2):.1f} Wh")
