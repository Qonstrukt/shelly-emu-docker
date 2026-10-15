"""Home Assistant WebSocket client (the only module that knows about HA)."""

from .client import HomeAssistantClient, HomeAssistantError, parse_state_number

__all__ = ["HomeAssistantClient", "HomeAssistantError", "parse_state_number"]
