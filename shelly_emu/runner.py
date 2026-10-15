"""Wire configured devices to Home Assistant and start the transports."""

from __future__ import annotations

import asyncio
import logging
import socket
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from .config import AppConfig, DeviceConfig
from .device import ShellyDevice
from .ha import HomeAssistantClient, parse_state_number
from .http_server import ShellyHttpServer
from .mdns import MdnsAdvertiser, create_zeroconf
from .modbus_server import ModbusServer
from .rpc import RpcDispatcher

_LOGGER = logging.getLogger(__name__)

ENERGY_UNIT_SCALES = {"Wh": 1.0, "kWh": 1000.0, "MWh": 1_000_000.0}


def detect_ip(bind: str = "0.0.0.0") -> str:
    """Best guess of the LAN address this host is reachable on (no packet is sent)."""
    if bind not in ("0.0.0.0", "", "::"):
        return bind
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 80))  # TEST-NET, never routed; only picks the outbound interface
            return s.getsockname()[0]
    except OSError:
        pass
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if not ip.startswith("127."):
            return ip
    except OSError:
        pass
    return "0.0.0.0"


class EmulatedDevice:
    """One configured device with its transports and HA bindings."""

    def __init__(self, cfg: DeviceConfig, schedule: Callable[[Any], None], ha: HomeAssistantClient) -> None:
        self.cfg = cfg
        self.ha = ha
        self.device = ShellyDevice(cfg.spec, on_switch=self._on_switch)
        self.rpc = RpcDispatcher(self.device)
        self.ip = cfg.ip or detect_ip(cfg.bind)
        self.device.ip = self.ip
        self._schedule = schedule
        self.http = ShellyHttpServer(self.device, self.rpc, cfg.bind, cfg.spec.http_port)
        self.modbus = ModbusServer(self.device, cfg.bind, cfg.spec.modbus_port) if cfg.modbus else None
        self.mdns: MdnsAdvertiser | None = None
        # entity id -> list of callables(state, attrs)
        self.bindings: dict[str, list[Callable[[str | None, dict], None]]] = defaultdict(list)
        self._bind_entities()

    # ------------------------------------------------------------------ bindings
    def _bind_entities(self) -> None:
        dev = self.device
        for ch, binding in enumerate(self.cfg.channels):
            self.bindings[binding.switch].append(lambda s, a, ch=ch: dev.update_switch(ch, output=(s == "on")))
            if binding.input:
                self.bindings[binding.input].append(lambda s, a, ch=ch: dev.update_switch(ch, input=(s == "on")))
            for field_name, entity in binding.sensors.items():
                if field_name in ("energy", "energy_returned") and binding.energy_unit is None:
                    self.bindings[entity].append(self._energy_updater(ch, field_name))
                else:
                    self.bindings[entity].append(
                        lambda s, a, ch=ch, f=field_name: dev.update_switch(ch, **{f: parse_state_number(s)})
                    )
        if self.cfg.em is not None:
            em = self.cfg.em
            for phase, pb in enumerate(em.phases):
                for field_name, entity in pb.sensors.items():
                    if field_name in ("energy", "energy_returned") and em.energy_unit is None:
                        self.bindings[entity].append(self._phase_energy_updater(phase, field_name))
                    else:
                        self.bindings[entity].append(
                            lambda s, a, p=phase, f=field_name: dev.update_em(p, **{f: parse_state_number(s)})
                        )
            if em.neutral_current:
                self.bindings[em.neutral_current].append(lambda s, a: self._em_extra("neutral_current", s))
            if em.temperature:
                self.bindings[em.temperature].append(lambda s, a: self._em_extra("temperature", s))

    def _em_extra(self, key: str, state: str | None) -> None:
        self._em_extra_values = getattr(self, "_em_extra_values", {})
        self._em_extra_values[key] = parse_state_number(state)
        self.device.update_em_extra(**self._em_extra_values)

    @staticmethod
    def _unit_scale(attrs: dict, default_scale: float) -> float:
        """Scale from the entity's unit to Wh (energy_unit not configured)."""
        unit = attrs.get("unit_of_measurement")
        return ENERGY_UNIT_SCALES.get(unit, default_scale)

    def _energy_updater(self, ch: int, field_name: str) -> Callable[[str | None, dict], None]:
        spec = self.device.spec.switches[ch]

        def update(state: str | None, attrs: dict) -> None:
            spec.energy_scale = self._unit_scale(attrs, spec.energy_scale)
            self.device.update_switch(ch, **{field_name: parse_state_number(state)})

        return update

    def _phase_energy_updater(self, phase: int, field_name: str) -> Callable[[str | None, dict], None]:
        em_spec = self.device.spec.em

        def update(state: str | None, attrs: dict) -> None:
            assert em_spec is not None
            em_spec.energy_scale = self._unit_scale(attrs, em_spec.energy_scale)
            self.device.update_em(phase, **{field_name: parse_state_number(state)})

        return update

    def on_state(self, entity_id: str, state: str | None, attrs: dict) -> None:
        for cb in self.bindings.get(entity_id, ()):
            cb(state, attrs)

    def _on_switch(self, ch: int, on: bool) -> None:
        entity = self.cfg.channels[ch].switch
        self._schedule(self.ha.turn(entity, on))

    # ------------------------------------------------------------------ lifecycle
    async def start(self, zc) -> None:
        await self.http.start()
        if self.modbus is not None:
            await self.modbus.start()
        if self.cfg.mdns and zc is not None:
            self.mdns = MdnsAdvertiser(zc, self.device, self.ip, self.cfg.spec.http_port)
            await self.mdns.start()
        _LOGGER.info(
            "%s ready: model %s (%s) name=%r ip=%s channels=%d",
            self.device.device_id, self.device.model.app, self.device.model_code, self.device.device_name,
            self.ip, self.device.switch_count,
        )

    async def stop(self) -> None:
        if self.mdns is not None:
            await self.mdns.stop()
        if self.modbus is not None:
            await self.modbus.stop()
        await self.http.stop()


class Runner:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._tasks: set[asyncio.Task] = set()
        self.devices: list[EmulatedDevice] = []
        self.ha: HomeAssistantClient | None = None
        self._zc = None
        self._stop = asyncio.Event()

    def schedule(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _on_state(self, entity_id: str, state: str | None, attrs: dict) -> None:
        _LOGGER.debug("HA %s = %s", entity_id, state)
        for d in self.devices:
            d.on_state(entity_id, state, attrs)

    def _on_connection(self, connected: bool) -> None:
        for d in self.devices:
            d.device.connected = connected

    async def run(self) -> None:
        entity_ids = [e for d in self.config.devices for e in d.entity_ids]
        self.ha = HomeAssistantClient(
            self.config.ha_url, self.config.ha_token, entity_ids, self._on_state, self._on_connection
        )
        self.devices = [EmulatedDevice(d, self.schedule, self.ha) for d in self.config.devices]
        if any(d.cfg.mdns for d in self.devices):
            self._zc = await create_zeroconf(self.config.interfaces)
        ha_task = asyncio.get_running_loop().create_task(self.ha.run())
        try:
            for d in self.devices:
                await d.start(self._zc)
            await self._stop.wait()
        finally:
            _LOGGER.info("shutting down")
            for d in self.devices:
                await d.stop()
            if self._zc is not None:
                await self._zc.async_close()
            await self.ha.stop()
            ha_task.cancel()
            for t in list(self._tasks):
                t.cancel()
            await asyncio.gather(ha_task, *self._tasks, return_exceptions=True)

    def stop(self) -> None:
        self._stop.set()
