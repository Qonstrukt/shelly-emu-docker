"""The emulated Shelly device: identity, live values, snapshot, switch commands.

This module has no network or Home Assistant code. A data source (the HA client,
a test, a future HACS integration) pushes values in with :meth:`ShellyDevice.update_switch`
and :meth:`ShellyDevice.update_em`; transports read :meth:`ShellyDevice.snapshot`.
``Switch.Set`` / ``Switch.Toggle`` end up in the ``on_switch`` callback, which the
integration layer turns into a Home Assistant service call.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .models import DEFAULT_FW_ID, DEFAULT_FW_VERSION, EM_PHASES, MAX_SWITCHES, MODELS, ModelInfo, normalize_mac
from .state import EmPhaseState, EmState, Snapshot, SwitchState

_LOGGER = logging.getLogger(__name__)

OPTIMISTIC_SECONDS = 5.0
ADDON_SWITCH_ID = 100  # Shelly add-on peripheral ids are 100..199

SWITCH_FIELDS = ("power", "voltage", "current", "energy", "energy_returned", "frequency", "power_factor", "temperature")
PHASE_FIELDS = ("voltage", "current", "power", "apparent_power", "power_factor", "frequency", "energy", "energy_returned")

ENERGY_UNITS = {"kWh": 1000.0, "Wh": 1.0}


@dataclass
class SwitchChannelSpec:
    """Which values a switch channel has a source for (entity ids are opaque here)."""

    name: str = ""
    has_power: bool = False  # a power source exists (else P is derived from U * I * pf)
    has_energy: bool = False
    has_energy_returned: bool = False
    energy_scale: float = 1000.0  # multiply source energy by this to get Wh


@dataclass
class EmPhaseSpec:
    has_energy: bool = False
    has_energy_returned: bool = False
    has_power: bool = False


@dataclass
class EmSpec:
    phases: list[EmPhaseSpec] = field(default_factory=lambda: [EmPhaseSpec() for _ in range(EM_PHASES)])
    energy_scale: float = 1000.0


@dataclass
class DeviceSpec:
    """Everything the core needs to know about one emulated device."""

    model: str
    mac: str
    name: str = ""
    model_code: str | None = None
    fw_version: str = DEFAULT_FW_VERSION
    fw_id: str = DEFAULT_FW_ID
    timezone: str = "UTC"
    wifi_ssid: str | None = "HomeAssistant"
    http_port: int = 80
    modbus_port: int = 502
    switches: list[SwitchChannelSpec] = field(default_factory=list)
    em: EmSpec | None = None
    # Pro 3EM with the Shelly Pro Output Add-on: ``switches[0]`` is exposed as switch:100.
    output_addon: bool = False

    def __post_init__(self) -> None:
        if self.model not in MODELS:
            raise ValueError(f"unknown model {self.model!r}, choose one of {', '.join(MODELS)}")
        self.mac = normalize_mac(self.mac)
        info = MODELS[self.model]
        if info.is_meter:
            if self.output_addon:
                if len(self.switches) != 1:
                    raise ValueError("output_addon needs exactly one switch channel")
            elif self.switches:
                raise ValueError(
                    f"model {self.model!r} has no switch channels; use 'output_addon' for the Pro Output Add-on relay"
                )
            if self.em is None:
                self.em = EmSpec()
        else:
            if self.em is not None:
                raise ValueError(f"model {self.model!r} is not an energy meter")
            if self.output_addon:
                raise ValueError("'output_addon' is only available for model 'pro3em'")
            if not self.switches:
                raise ValueError("at least one switch channel is required")
            if len(self.switches) > info.switches:
                raise ValueError(f"model {self.model!r} supports at most {info.switches} switch channel(s)")


@dataclass
class _SwitchValues:
    output: bool = False
    input: bool = False
    values: dict[str, float | None] = field(default_factory=dict)
    integrated_wh: float = 0.0
    optimistic: bool = False
    optimistic_state: bool = False
    optimistic_until: float = 0.0


@dataclass
class _PhaseValues:
    values: dict[str, float | None] = field(default_factory=dict)
    integrated_wh: float = 0.0
    integrated_ret_wh: float = 0.0


def _num(value: float | None, fallback: float | None) -> float | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return fallback
    return float(value)


class ShellyDevice:
    """One emulated Shelly Gen2 device."""

    def __init__(
        self,
        spec: DeviceSpec,
        *,
        on_switch: Callable[[int, bool], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        wallclock: Callable[[], float] = time.time,
    ) -> None:
        self.spec = spec
        self.model: ModelInfo = MODELS[spec.model]
        self.model_code = spec.model_code or self.model.code
        self.on_switch = on_switch
        self._clock = clock
        self._wallclock = wallclock
        self._started = clock()

        self.mac = spec.mac  # AABBCCDDEEFF
        self.mac_lower = self.mac.lower()
        self.device_id = f"{self.model.host_prefix.lower()}-{self.mac_lower}"
        self.hostname = f"{self.model.host_prefix}-{self.mac}"
        self.device_name: str | None = spec.name or None
        self.cfg_rev = 1
        self.ip = "0.0.0.0"
        self.connected = False  # data source (HA) connected

        self._switches = [_SwitchValues() for _ in self.spec.switches]
        self._phases = [_PhaseValues() for _ in range(EM_PHASES)]
        self._em_extra: dict[str, float | None] = {}
        self._last_integration = clock()

    # ------------------------------------------------------------------ identity
    @property
    def is_meter(self) -> bool:
        return self.model.is_meter

    @property
    def switch_count(self) -> int:
        return len(self._switches)

    @property
    def has_output_addon(self) -> bool:
        return self.spec.output_addon and self.switch_count > 0

    def switch_component_id(self, index: int) -> int:
        """Shelly component id of channel ``index`` (0..n for switch devices, 100 for the add-on relay)."""
        return ADDON_SWITCH_ID + index if self.spec.output_addon else index

    def switch_index_for_id(self, cid: int) -> int:
        """Channel index for a Shelly switch id, or -1 if the id does not exist."""
        index = cid - ADDON_SWITCH_ID if self.spec.output_addon else cid
        return index if 0 <= index < self.switch_count else -1

    @property
    def uptime(self) -> int:
        return int(self._clock() - self._started)

    def now(self) -> float:
        return self._wallclock()

    def channel_name(self, ch: int) -> str:
        if 0 <= ch < len(self.spec.switches):
            return self.spec.switches[ch].name
        return ""

    # ------------------------------------------------------------------ inputs
    def update_switch(self, ch: int, *, output: bool | None = None, input: bool | None = None, **values: float | None) -> None:
        """Store new values for a switch channel. Keys of ``values`` are :data:`SWITCH_FIELDS`."""
        if not 0 <= ch < self.switch_count:
            raise IndexError(f"switch channel {ch} out of range")
        self._integrate()
        s = self._switches[ch]
        if output is not None:
            s.output = bool(output)
        if input is not None:
            s.input = bool(input)
        for key, value in values.items():
            if key not in SWITCH_FIELDS:
                raise KeyError(key)
            s.values[key] = _num(value, None)

    def update_em(self, phase: int, **values: float | None) -> None:
        """Store new values for a meter phase (0..2). Keys are :data:`PHASE_FIELDS`."""
        if not 0 <= phase < EM_PHASES:
            raise IndexError(f"phase {phase} out of range")
        self._integrate()
        for key, value in values.items():
            if key not in PHASE_FIELDS:
                raise KeyError(key)
            self._phases[phase].values[key] = _num(value, None)

    def update_em_extra(self, *, neutral_current: float | None = None, temperature: float | None = None) -> None:
        self._em_extra["neutral_current"] = _num(neutral_current, None)
        self._em_extra["temperature"] = _num(temperature, None)

    # ------------------------------------------------------------------ energy integration
    def _integrate(self) -> None:
        """Integrate power into Wh for channels without an energy source."""
        now = self._clock()
        dt_h = (now - self._last_integration) / 3600.0
        self._last_integration = now
        if dt_h <= 0:
            return
        for spec, s in zip(self.spec.switches, self._switches):
            if spec.has_energy:
                continue
            p = _num(s.values.get("power"), 0.0) or 0.0
            if p > 0:
                s.integrated_wh += p * dt_h
        if self.is_meter and self.spec.em is not None:
            for spec, ph in zip(self.spec.em.phases, self._phases):
                p = _num(ph.values.get("power"), 0.0) or 0.0
                if not spec.has_energy and p > 0:
                    ph.integrated_wh += p * dt_h
                if not spec.has_energy_returned and p < 0:
                    ph.integrated_ret_wh += -p * dt_h

    # ------------------------------------------------------------------ snapshot
    def snapshot(self) -> Snapshot:
        self._integrate()
        now = self._clock()
        snap = Snapshot()
        for i, (spec, ch) in enumerate(zip(self.spec.switches, self._switches)):
            s: SwitchState = snap.sw[i]
            state = ch.output
            if ch.optimistic:
                if now >= ch.optimistic_until or state == ch.optimistic_state:
                    ch.optimistic = False
                else:
                    state = ch.optimistic_state
            v = ch.values
            s.output = state
            s.input = ch.input
            s.voltage = _num(v.get("voltage"), None)
            s.freq = _num(v.get("frequency"), None)
            s.temp = _num(v.get("temperature"), None)
            current = _num(v.get("current"), None)
            pf = _num(v.get("power_factor"), None)
            power = _num(v.get("power"), None)
            if power is not None or spec.has_power or current is None or s.voltage is None:
                s.apower = power or 0.0
            else:
                # No power source but voltage and current are known: P = U * I * pf
                s.apower = s.voltage * current * (1.0 if pf is None else pf)
            if current is None:
                current = abs(s.apower) / s.voltage if s.voltage else 0.0
            s.current = current
            if pf is None:
                pf = 1.0 if s.apower != 0 else 0.0
            s.pf = pf
            if spec.has_energy:
                s.aenergy = (_num(v.get("energy"), 0.0) or 0.0) * spec.energy_scale
            else:
                s.aenergy = ch.integrated_wh
            if spec.has_energy_returned:
                s.ret_aenergy = (_num(v.get("energy_returned"), 0.0) or 0.0) * spec.energy_scale
            else:
                s.ret_aenergy = 0.0

        if self.is_meter and self.spec.em is not None:
            em: EmState = snap.em
            scale = self.spec.em.energy_scale
            for p, (spec, ph) in enumerate(zip(self.spec.em.phases, self._phases)):
                e: EmPhaseState = em.phase[p]
                v = ph.values
                e.voltage = _num(v.get("voltage"), 230.0) or 0.0
                e.freq = _num(v.get("frequency"), 50.0) or 0.0
                e.pf = _num(v.get("power_factor"), 1.0) or 0.0
                current = _num(v.get("current"), None)
                power = _num(v.get("power"), None)
                if power is not None or spec.has_power or current is None:
                    e.act_power = power or 0.0
                else:
                    # No power source but current (and voltage) are known: P = U * I * pf, I < 0 = export
                    e.act_power = e.voltage * current * e.pf
                aprt = _num(v.get("apparent_power"), None)
                if aprt is None:
                    if current is not None:
                        aprt = e.voltage * abs(current)
                    else:
                        aprt = abs(e.act_power) / abs(e.pf) if abs(e.pf) > 0.01 else abs(e.act_power)
                e.aprt_power = aprt
                if current is None:
                    current = e.aprt_power / e.voltage if e.voltage > 0 else 0.0
                e.current = current
                e.energy = (_num(v.get("energy"), 0.0) or 0.0) * scale if spec.has_energy else ph.integrated_wh
                e.ret_energy = (
                    (_num(v.get("energy_returned"), 0.0) or 0.0) * scale if spec.has_energy_returned else ph.integrated_ret_wh
                )
            em.n_current = _num(self._em_extra.get("neutral_current"), None)
            em.temp = _num(self._em_extra.get("temperature"), None)
        return snap

    # ------------------------------------------------------------------ commands
    def request_switch(self, ch: int, on: bool) -> bool:
        """Switch a channel. Returns the previous output state (``was_on``)."""
        if not 0 <= ch < self.switch_count:
            raise IndexError(f"switch channel {ch} out of range")
        s = self._switches[ch]
        was_on = s.optimistic_state if s.optimistic and self._clock() < s.optimistic_until else s.output
        s.optimistic = True
        s.optimistic_state = on
        s.optimistic_until = self._clock() + OPTIMISTIC_SECONDS
        _LOGGER.info("%s switch:%d -> %s", self.device_id, self.switch_component_id(ch), "on" if on else "off")
        if self.on_switch is not None:
            self.on_switch(ch, on)
        return was_on

    def set_device_name(self, name: str) -> None:
        self.device_name = name or None
        self.cfg_rev += 1
