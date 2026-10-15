import struct

import pytest

from shelly_emu.modbus_registers import (
    EX_ILLEGAL_ADDRESS,
    EX_ILLEGAL_FUNCTION,
    build_register_map,
    get_float,
    handle_frame,
    put_float,
    split_frames,
)
from shelly_emu.state import EmPhaseState, EmState


def test_put_float_cdab_word_order():
    regs = {}
    put_float(regs, 100, 230.0)
    # 230.0f = 0x43660000 -> low word first
    assert regs[100] == 0x0000 and regs[101] == 0x4366
    assert get_float(regs, 100) == 230.0
    put_float(regs, 200, -1.5)
    assert get_float(regs, 200) == -1.5


def test_register_map_layout():
    em = EmState(
        phase=[
            EmPhaseState(voltage=230.0, current=2.0, act_power=460.0, aprt_power=460.0, pf=1.0, freq=50.0, energy=1000.0, ret_energy=10.0),
            EmPhaseState(voltage=231.0, current=1.0, act_power=-100.0, aprt_power=120.0, pf=0.83, freq=50.0, energy=500.0, ret_energy=20.0),
            EmPhaseState(voltage=229.0, current=0.0, act_power=0.0, aprt_power=0.0, pf=1.0, freq=50.0, energy=0.0, ret_energy=0.0),
        ],
        n_current=0.5,
    )
    regs = build_register_map(em, "AABBCCDDEEFF", "SPEM-003CEBEU", 1700000000.0)
    # identity: MAC in regs 0..5, model in 6..13, little-endian char pairs
    assert bytes([regs[0] & 0xFF, regs[0] >> 8]) == b"AA"
    assert "".join(chr(regs[i] & 0xFF) + chr(regs[i] >> 8) for i in range(6)) == "AABBCCDDEEFF"
    assert "".join(chr(regs[i] & 0xFF) + chr(regs[i] >> 8) for i in range(6, 14)).rstrip("\0") == "SPEM-003CEBEU"
    # EM block
    assert (regs[1001] << 16 | regs[1000]) == 1700000000
    assert get_float(regs, 1007) == 0.5
    assert get_float(regs, 1011) == pytest.approx(3.0)
    assert get_float(regs, 1013) == pytest.approx(360.0)
    assert get_float(regs, 1015) == pytest.approx(580.0)
    assert get_float(regs, 1020) == 230.0 and get_float(regs, 1040) == 231.0 and get_float(regs, 1060) == 229.0
    assert get_float(regs, 1044) == -100.0
    assert get_float(regs, 1048) == pytest.approx(0.83)
    assert get_float(regs, 1033) == 50.0
    # EMData block
    assert get_float(regs, 1162) == 1500.0 and get_float(regs, 1164) == 30.0
    assert get_float(regs, 1170) == 1000.0 and get_float(regs, 1174) == 10.0 and get_float(regs, 1184) == 10.0
    assert get_float(regs, 1190) == 500.0


def test_register_map_unsynced_time_and_no_neutral():
    regs = build_register_map(EmState(), "AABBCCDDEEFF", "SPEM-003CEBEU", 12.0)
    assert regs[1000] == 0 and regs[1001] == 0
    assert get_float(regs, 1007) == 0.0


def _adu(tid, unit, fc, addr, count):
    pdu = struct.pack(">BHH", fc, addr, count)
    return struct.pack(">HHHB", tid, 0, len(pdu) + 1, unit) + pdu


def test_handle_frame_fc04():
    regs = {}
    put_float(regs, 1013, 360.0)
    resp = handle_frame(_adu(0x1234, 7, 4, 1013, 2), regs)
    tid, proto, length, unit, fc, nbytes = struct.unpack(">HHHBBB", resp[:9])
    assert (tid, proto, length, unit, fc, nbytes) == (0x1234, 0, 7, 7, 4, 4)
    words = struct.unpack(">HH", resp[9:])
    assert struct.unpack(">f", struct.pack(">HH", words[1], words[0]))[0] == 360.0
    # unmapped registers read as 0
    resp = handle_frame(_adu(1, 1, 4, 5000, 3), regs)
    assert resp[9:] == b"\x00" * 6


def test_handle_frame_errors():
    regs = {}
    resp = handle_frame(_adu(1, 7, 3, 1000, 2), regs)  # holding: illegal address like a real 3EM
    assert resp[7] == 0x83 and resp[8] == EX_ILLEGAL_ADDRESS
    resp = handle_frame(_adu(1, 7, 6, 1000, 2), regs)
    assert resp[7] == 0x86 and resp[8] == EX_ILLEGAL_FUNCTION
    resp = handle_frame(_adu(1, 7, 4, 1000, 200), regs)
    assert resp[7] == 0x84 and resp[8] == EX_ILLEGAL_ADDRESS
    assert struct.unpack(">H", resp[4:6])[0] == 3


def test_split_frames():
    buf = bytearray(_adu(1, 1, 4, 0, 1) + _adu(2, 1, 4, 0, 1)[:5])
    frames = split_frames(buf)
    assert len(frames) == 1 and frames[0][:2] == b"\x00\x01"
    assert len(buf) == 5
    buf += _adu(2, 1, 4, 0, 1)[5:]
    assert len(split_frames(buf)) == 1 and not buf
    with pytest.raises(ValueError):
        split_frames(bytearray(b"\x00\x01\x00\x00\x00\x01\x01"))
