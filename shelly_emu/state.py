"""Plain value snapshots shared by the RPC and Modbus builders."""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import EM_PHASES, MAX_SWITCHES


@dataclass
class SwitchState:
    output: bool = False
    input: bool = False
    apower: float = 0.0
    voltage: float | None = None
    current: float = 0.0
    freq: float | None = None
    pf: float = 0.0
    temp: float | None = None
    aenergy: float = 0.0  # Wh
    ret_aenergy: float = 0.0  # Wh


@dataclass
class EmPhaseState:
    voltage: float = 0.0
    current: float = 0.0
    act_power: float = 0.0
    aprt_power: float = 0.0
    pf: float = 1.0
    freq: float = 50.0
    energy: float = 0.0  # Wh
    ret_energy: float = 0.0  # Wh


@dataclass
class EmState:
    phase: list[EmPhaseState] = field(default_factory=lambda: [EmPhaseState() for _ in range(EM_PHASES)])
    n_current: float | None = None
    temp: float | None = None


@dataclass
class Snapshot:
    """Copy of all live values at one instant."""

    sw: list[SwitchState] = field(default_factory=lambda: [SwitchState() for _ in range(MAX_SWITCHES)])
    em: EmState = field(default_factory=EmState)
