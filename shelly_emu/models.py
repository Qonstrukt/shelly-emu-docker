"""Shelly Gen2 model identities (same table as the ESPHome component)."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_FW_VERSION = "2.0.0"
DEFAULT_FW_ID = "20260710-101218/2.0.0-g87fbfa4"

MAX_SWITCHES = 4
EM_PHASES = 3


@dataclass(frozen=True)
class ModelInfo:
    key: str
    code: str
    app: str
    host_prefix: str
    switches: int
    is_pro: bool
    is_meter: bool

    @property
    def has_profile(self) -> bool:
        """Pro/Plus 2PM report a ``switch`` profile, the 3EM a ``triphase`` one."""
        return self.is_meter or self.app in ("Pro2PM", "Plus2PM")

    @property
    def profile(self) -> str | None:
        if self.is_meter:
            return "triphase"
        if self.app in ("Pro2PM", "Plus2PM"):
            return "switch"
        return None

    @property
    def has_inputs(self) -> bool:
        return not self.is_meter and self.app != "PlusPlugS"


MODELS: dict[str, ModelInfo] = {
    m.key: m
    for m in (
        ModelInfo("pro1pm", "SPSW-201PE16EU", "Pro1PM", "ShellyPro1PM", 1, True, False),
        ModelInfo("pro2pm", "SPSW-202PE16EU", "Pro2PM", "ShellyPro2PM", 2, True, False),
        ModelInfo("pro4pm", "SPSW-004PE16EU", "Pro4PM", "ShellyPro4PM", 4, True, False),
        ModelInfo("plus1pm", "SNSW-001P16EU", "Plus1PM", "ShellyPlus1PM", 1, False, False),
        ModelInfo("plus2pm", "SNSW-102P16EU", "Plus2PM", "ShellyPlus2PM", 2, False, False),
        ModelInfo("plusplugs", "SNPL-00112EU", "PlusPlugS", "ShellyPlusPlugS", 1, False, False),
        ModelInfo("pro3em", "SPEM-003CEBEU", "Pro3EM", "ShellyPro3EM", 0, True, True),
    )
}


def normalize_mac(value: str) -> str:
    """Return the MAC as 12 upper-case hex digits or raise ``ValueError``."""
    mac = value.replace(":", "").replace("-", "").strip().upper()
    if len(mac) != 12 or any(c not in "0123456789ABCDEF" for c in mac):
        raise ValueError(f"mac must be 12 hex digits, e.g. AABBCCDDEEFF (got {value!r})")
    return mac
