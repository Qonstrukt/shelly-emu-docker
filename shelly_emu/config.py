"""YAML configuration: devices with their Home Assistant entity bindings, plus env overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .device import ENERGY_UNITS, PHASE_FIELDS, SWITCH_FIELDS, DeviceSpec, EmPhaseSpec, EmSpec, SwitchChannelSpec
from .models import DEFAULT_FW_ID, DEFAULT_FW_VERSION, EM_PHASES, MODELS


class ConfigError(Exception):
    pass


@dataclass
class ChannelBinding:
    switch: str
    name: str = ""
    input: str | None = None
    sensors: dict[str, str] = field(default_factory=dict)  # field -> entity id
    energy_unit: str | None = None  # kWh | Wh | None = from the entity's unit attribute

    @property
    def entity_ids(self) -> list[str]:
        ids = [self.switch, *self.sensors.values()]
        if self.input:
            ids.append(self.input)
        return ids


@dataclass
class PhaseBinding:
    sensors: dict[str, str] = field(default_factory=dict)


@dataclass
class EmBinding:
    phases: list[PhaseBinding] = field(default_factory=list)
    neutral_current: str | None = None
    temperature: str | None = None
    energy_unit: str | None = None

    @property
    def entity_ids(self) -> list[str]:
        ids = [e for p in self.phases for e in p.sensors.values()]
        ids += [e for e in (self.neutral_current, self.temperature) if e]
        return ids


@dataclass
class DeviceConfig:
    spec: DeviceSpec
    channels: list[ChannelBinding] = field(default_factory=list)
    em: EmBinding | None = None
    bind: str = "0.0.0.0"
    ip: str | None = None  # advertised address; auto-detected when None
    mdns: bool = True
    modbus: bool = False

    @property
    def entity_ids(self) -> list[str]:
        ids = [e for ch in self.channels for e in ch.entity_ids]
        if self.em is not None:
            ids += self.em.entity_ids
        return ids


@dataclass
class AppConfig:
    ha_url: str
    ha_token: str
    devices: list[DeviceConfig]
    interfaces: list[str] | None = None
    log_level: str = "INFO"


def _str(d: dict, key: str, default: Any = None, *, where: str) -> Any:
    v = d.get(key, default)
    if v is None:
        return None
    if not isinstance(v, str):
        raise ConfigError(f"{where}: '{key}' must be a string")
    return v


def _energy_unit(value: Any, where: str) -> str | None:
    if value is None:
        return None
    if value not in ENERGY_UNITS:
        raise ConfigError(f"{where}: energy_unit must be one of {', '.join(ENERGY_UNITS)}")
    return value


def _parse_channel(raw: Any, where: str) -> ChannelBinding:
    if not isinstance(raw, dict) or "switch" not in raw:
        raise ConfigError(f"{where}: each channel needs a 'switch' entity id")
    sensors = {}
    for key in SWITCH_FIELDS:
        ent = _str(raw, key, where=where)
        if ent:
            sensors[key] = ent
    known = {"switch", "name", "input", "energy_unit", *SWITCH_FIELDS}
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"{where}: unknown keys {sorted(unknown)}")
    return ChannelBinding(
        switch=_str(raw, "switch", where=where),
        name=_str(raw, "name", "", where=where) or "",
        input=_str(raw, "input", where=where),
        sensors=sensors,
        energy_unit=_energy_unit(raw.get("energy_unit"), where),
    )


def _parse_em(raw: Any, where: str) -> EmBinding:
    if not isinstance(raw, dict) or not isinstance(raw.get("phases"), list) or not raw["phases"]:
        raise ConfigError(f"{where}: 'em.phases' must be a list of 1 to {EM_PHASES} phases")
    if len(raw["phases"]) > EM_PHASES:
        raise ConfigError(f"{where}: at most {EM_PHASES} phases")
    phases = []
    for i, p in enumerate(raw["phases"]):
        pw = f"{where}.phases[{i}]"
        if not isinstance(p, dict):
            raise ConfigError(f"{pw}: must be a mapping")
        unknown = set(p) - set(PHASE_FIELDS)
        if unknown:
            raise ConfigError(f"{pw}: unknown keys {sorted(unknown)}")
        phases.append(PhaseBinding({k: v for k in PHASE_FIELDS if (v := _str(p, k, where=pw))}))
    unknown = set(raw) - {"phases", "neutral_current", "temperature", "energy_unit"}
    if unknown:
        raise ConfigError(f"{where}: unknown keys {sorted(unknown)}")
    return EmBinding(
        phases=phases,
        neutral_current=_str(raw, "neutral_current", where=where),
        temperature=_str(raw, "temperature", where=where),
        energy_unit=_energy_unit(raw.get("energy_unit"), where),
    )


DEVICE_KEYS = {
    "model", "name", "mac", "model_code", "firmware_version", "fw_id", "timezone", "wifi_ssid",
    "bind", "ip", "http_port", "mdns", "modbus", "modbus_port", "channels", "em", "output_addon",
}  # fmt: skip


def _parse_output_addon(raw: Any, where: str) -> ChannelBinding:
    if not isinstance(raw, dict) or not isinstance(raw.get("switch"), str):
        raise ConfigError(f"{where}: 'output_addon' needs a 'switch' entity id")
    unknown = set(raw) - {"switch", "name"}
    if unknown:
        raise ConfigError(f"{where}: unknown keys {sorted(unknown)}")
    return ChannelBinding(switch=raw["switch"], name=_str(raw, "name", "", where=where) or "")


def parse_device(raw: Any, index: int) -> DeviceConfig:
    where = f"devices[{index}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: must be a mapping")
    unknown = set(raw) - DEVICE_KEYS
    if unknown:
        raise ConfigError(f"{where}: unknown keys {sorted(unknown)}")
    model = _str(raw, "model", where=where)
    if not model or model.lower() not in MODELS:
        raise ConfigError(f"{where}: 'model' must be one of {', '.join(MODELS)}")
    model = model.lower()
    mac = raw.get("mac")
    if mac is None:
        raise ConfigError(f"{where}: 'mac' is required (12 hex digits, it fixes the Shelly id)")
    info = MODELS[model]

    channels = [_parse_channel(c, f"{where}.channels[{i}]") for i, c in enumerate(raw.get("channels") or [])]
    em = _parse_em(raw["em"], f"{where}.em") if "em" in raw else None
    if info.is_meter and em is None:
        raise ConfigError(f"{where}: model '{model}' needs an 'em' section")
    output_addon = "output_addon" in raw
    if output_addon:
        if not info.is_meter:
            raise ConfigError(f"{where}: 'output_addon' is only available for model 'pro3em'")
        if channels:
            raise ConfigError(f"{where}: model '{model}' has no 'channels'; the add-on relay is 'output_addon'")
        # The add-on relay is channel 0 of the device, exposed as Shelly component switch:100.
        channels = [_parse_output_addon(raw["output_addon"], f"{where}.output_addon")]
    elif info.is_meter and channels:
        raise ConfigError(f"{where}: model '{model}' has no switch channels; use 'output_addon' for the Pro Output Add-on relay")

    switch_specs = [
        SwitchChannelSpec(
            name=c.name,
            has_power="power" in c.sensors,
            has_energy="energy" in c.sensors,
            has_energy_returned="energy_returned" in c.sensors,
            energy_scale=ENERGY_UNITS[c.energy_unit or "kWh"],
        )
        for c in channels
    ]
    em_spec = None
    if em is not None:
        phase_specs = [
            EmPhaseSpec("energy" in p.sensors, "energy_returned" in p.sensors, has_power="power" in p.sensors)
            for p in em.phases
        ]
        phase_specs += [EmPhaseSpec() for _ in range(EM_PHASES - len(phase_specs))]
        em_spec = EmSpec(phases=phase_specs, energy_scale=ENERGY_UNITS[em.energy_unit or "kWh"])

    try:
        spec = DeviceSpec(
            model=model,
            mac=str(mac),
            name=_str(raw, "name", "", where=where) or "",
            model_code=_str(raw, "model_code", where=where),
            fw_version=str(raw.get("firmware_version", DEFAULT_FW_VERSION)),
            fw_id=str(raw.get("fw_id", DEFAULT_FW_ID)),
            timezone=_str(raw, "timezone", "UTC", where=where) or "UTC",
            wifi_ssid=_str(raw, "wifi_ssid", "HomeAssistant", where=where),
            http_port=int(raw.get("http_port", 80)),
            modbus_port=int(raw.get("modbus_port", 502)),
            switches=switch_specs,
            em=em_spec,
            output_addon=output_addon,
        )
    except ValueError as err:
        raise ConfigError(f"{where}: {err}") from None

    return DeviceConfig(
        spec=spec,
        channels=channels,
        em=em,
        bind=_str(raw, "bind", "0.0.0.0", where=where) or "0.0.0.0",
        ip=_str(raw, "ip", where=where),
        mdns=bool(raw.get("mdns", True)),
        modbus=bool(raw.get("modbus", info.is_meter)),
    )


def _check_listeners(devices: list[DeviceConfig]) -> None:
    """Sigen identifies a device by IP and assumes port 80/502: two devices in one process need
    distinct bind addresses (or distinct ports, which Sigen cannot use)."""
    seen: dict[tuple[str, int], str] = {}
    for d in devices:
        ports = [d.spec.http_port] + ([d.spec.modbus_port] if d.modbus else [])
        for port in ports:
            for other_bind, other_port in list(seen):
                if other_port == port and ("0.0.0.0" in (other_bind, d.bind) or other_bind == d.bind):
                    raise ConfigError(
                        f"devices {seen[(other_bind, other_port)]} and {d.spec.mac} both listen on "
                        f"{d.bind}:{port}. Give each device its own IP ('bind:' on a host with several "
                        "addresses) or run one container per device (macvlan), see README."
                    )
            seen[(d.bind, port)] = d.spec.mac
    macs = [d.spec.mac for d in devices]
    if len(set(macs)) != len(macs):
        raise ConfigError("every device needs a unique 'mac'")


def load_config(path: str | Path, env: dict[str, str] | None = None) -> AppConfig:
    env = os.environ if env is None else env
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except yaml.YAMLError as err:
        raise ConfigError(f"{path}: invalid YAML: {err}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping")

    ha = raw.get("homeassistant") or {}
    if not isinstance(ha, dict):
        raise ConfigError("'homeassistant' must be a mapping")
    url = env.get("HA_URL") or ha.get("url")
    token = env.get("HA_TOKEN") or ha.get("token")
    token_file = env.get("HA_TOKEN_FILE") or ha.get("token_file")
    if not token and token_file:
        try:
            token = Path(token_file).read_text().strip()
        except OSError as err:
            raise ConfigError(f"cannot read HA token file {token_file}: {err}") from None
    if not url:
        raise ConfigError("Home Assistant URL missing: set HA_URL or homeassistant.url")
    if not token:
        raise ConfigError("Home Assistant token missing: set HA_TOKEN, HA_TOKEN_FILE or homeassistant.token")

    devices_raw = raw.get("devices")
    if not isinstance(devices_raw, list) or not devices_raw:
        raise ConfigError("'devices' must be a non-empty list")
    devices = [parse_device(d, i) for i, d in enumerate(devices_raw)]
    _check_listeners(devices)

    interfaces = raw.get("mdns_interfaces")
    if interfaces is not None and not (isinstance(interfaces, list) and all(isinstance(i, str) for i in interfaces)):
        raise ConfigError("'mdns_interfaces' must be a list of IP addresses")
    advertise = env.get("ADVERTISE_IP")
    if advertise:
        for d in devices:
            if d.ip is None:
                d.ip = advertise
    return AppConfig(
        ha_url=str(url),
        ha_token=str(token),
        devices=devices,
        interfaces=interfaces,
        log_level=env.get("LOG_LEVEL") or str(raw.get("log_level", "INFO")),
    )
