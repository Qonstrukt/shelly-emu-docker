"""Minimal asyncio Modbus TCP server for the Pro 3EM (SigenStor polls it after enrollment)."""

from __future__ import annotations

import asyncio
import logging

from .device import ShellyDevice
from .modbus_registers import build_register_map, handle_frame, split_frames

_LOGGER = logging.getLogger(__name__)

CLIENT_TIMEOUT = 120.0


class ModbusServer:
    def __init__(self, device: ShellyDevice, host: str, port: int) -> None:
        self.device = device
        self.host = host
        self.port = port
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._client, self.host, self.port, reuse_address=True)
        _LOGGER.info("%s Modbus TCP listening on %s:%d", self.device.device_id, self.host, self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def _addon_output(self, snap) -> bool | None:
        return snap.sw[0].output if self.device.has_output_addon else None

    def registers(self):
        snap = self.device.snapshot()
        return build_register_map(
            snap.em, self.device.mac, self.device.model_code, self.device.now(), self._addon_output(snap)
        )

    def _write_coil(self, on: bool) -> None:
        _LOGGER.info("%s Modbus coil 100 <- %s", self.device.device_id, "on" if on else "off")
        self.device.request_switch(0, on)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        _LOGGER.debug("%s modbus client connected: %s", self.device.device_id, peer)
        buf = bytearray()
        try:
            while True:
                try:
                    data = await asyncio.wait_for(reader.read(512), CLIENT_TIMEOUT)
                except TimeoutError:
                    _LOGGER.debug("%s modbus client idle, closing: %s", self.device.device_id, peer)
                    break
                if not data:
                    break
                buf += data
                if len(buf) > 1024:
                    _LOGGER.warning("%s garbage from modbus client %s, closing", self.device.device_id, peer)
                    break
                try:
                    frames = split_frames(buf)
                except ValueError:
                    break
                for frame in frames:
                    snap = self.device.snapshot()
                    regs = build_register_map(
                        snap.em, self.device.mac, self.device.model_code, self.device.now(), self._addon_output(snap)
                    )
                    writer.write(handle_frame(frame, regs, self._addon_output(snap), self._write_coil))
                if frames:
                    await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
