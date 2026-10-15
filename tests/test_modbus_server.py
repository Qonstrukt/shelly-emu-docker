"""Connection handling of the asyncio Modbus TCP server (one connection, many requests)."""

import asyncio
import struct

import pytest

from shelly_emu import modbus_server
from shelly_emu.device import DeviceSpec, EmSpec, ShellyDevice, SwitchChannelSpec
from shelly_emu.modbus_server import ModbusServer


def _adu(tid, fc, *words, tail=b""):
    pdu = struct.pack(">B", fc) + b"".join(struct.pack(">H", w) for w in words) + tail
    return struct.pack(">HHHB", tid, 0, len(pdu) + 1, 7) + pdu


async def _request(reader, writer, adu):
    writer.write(adu)
    await writer.drain()
    hdr = await asyncio.wait_for(reader.readexactly(7), 2)
    tid, _, length, unit = struct.unpack(">HHHB", hdr)
    body = await asyncio.wait_for(reader.readexactly(length - 1), 2)
    return tid, body


@pytest.fixture
def device():
    commands = []
    spec = DeviceSpec(model="pro3em", mac="112233445588", em=EmSpec(), switches=[SwitchChannelSpec()], output_addon=True)
    dev = ShellyDevice(spec, on_switch=lambda ch, on: commands.append((ch, on)))
    dev.commands = commands
    dev.update_em(0, power=230.0, voltage=230.0)
    return dev


def run(coro):
    return asyncio.run(coro)


def test_many_requests_on_one_connection(device):
    async def scenario():
        server = ModbusServer(device, "127.0.0.1", 0)
        await server.start()
        port = server._server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            tid, body = await _request(reader, writer, _adu(1, 4, 1013, 2))
            assert tid == 1 and body[0] == 4 and body[1] == 4
            assert struct.unpack(">f", body[4:6] + body[2:4])[0] == pytest.approx(230.0)
            tid, body = await _request(reader, writer, _adu(2, 1, 1100, 1))
            assert tid == 2 and body == bytes([1, 1, 0])
            tid, body = await _request(reader, writer, _adu(3, 5, 1100, 0xFF00))
            assert tid == 3 and body == bytes([5]) + struct.pack(">HH", 1100, 0xFF00)
            assert device.commands == [(0, True)]
            tid, body = await _request(reader, writer, _adu(4, 1, 1100, 1))
            assert tid == 4 and body == bytes([1, 1, 1])  # optimistic state visible on the same connection
            tid, body = await _request(reader, writer, _adu(5, 4, 3000, 1))
            assert tid == 5 and body == bytes([4, 2, 0, 1])
            # two requests pipelined in one TCP segment
            writer.write(_adu(6, 4, 1013, 1) + _adu(7, 1, 1100, 1))
            await writer.drain()
            tids = []
            for _ in range(2):
                hdr = await asyncio.wait_for(reader.readexactly(7), 2)
                tid, _, length, _ = struct.unpack(">HHHB", hdr)
                await reader.readexactly(length - 1)
                tids.append(tid)
            assert tids == [6, 7]
            # still open: a short wait yields no EOF
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(reader.read(1), 0.3)
            assert not reader.at_eof()
        finally:
            writer.close()
            await server.stop()

    run(scenario())


def test_idle_timeout_only_after_real_idle_time(device, monkeypatch):
    monkeypatch.setattr(modbus_server, "CLIENT_TIMEOUT", 0.6)

    async def scenario():
        server = ModbusServer(device, "127.0.0.1", 0)
        await server.start()
        port = server._server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            # activity every 0.3 s for 1.5 s keeps the connection alive past the 0.6 s timeout
            for tid in range(1, 6):
                await asyncio.sleep(0.3)
                got, _ = await _request(reader, writer, _adu(tid, 4, 1013, 1))
                assert got == tid
            # now go idle: closed after the timeout, not before
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(reader.read(1), 0.3)
            data = await asyncio.wait_for(reader.read(1), 1.0)
            assert data == b"" and reader.at_eof()
        finally:
            writer.close()
            await server.stop()

    run(scenario())


def test_concurrent_connections_are_not_evicted(device):
    async def scenario():
        server = ModbusServer(device, "127.0.0.1", 0)
        await server.start()
        port = server._server.sockets[0].getsockname()[1]
        conns = [await asyncio.open_connection("127.0.0.1", port) for _ in range(6)]
        try:
            for i, (reader, writer) in enumerate(conns):
                tid, body = await _request(reader, writer, _adu(10 + i, 4, 1013, 1))
                assert tid == 10 + i and body[0] == 4
            # the first connection still works after five more were opened
            reader, writer = conns[0]
            tid, body = await _request(reader, writer, _adu(99, 1, 1100, 1))
            assert tid == 99 and body[0] == 1
            for reader, _ in conns:
                assert not reader.at_eof()
        finally:
            for _, writer in conns:
                writer.close()
            await server.stop()

    run(scenario())
