"""Shelly Gen2 device emulator backed by Home Assistant.

Package layout:

* ``models``, ``state``, ``device``, ``rpc``, ``modbus_registers``: the emulator core.
  No network code, no Home Assistant code; usable from a HACS integration later.
* ``http_server``, ``mdns``, ``modbus_server``: asyncio transports.
* ``ha``: Home Assistant WebSocket client.
* ``config``, ``runner``, ``cli``: YAML configuration and the container entry point.
"""

__version__ = "0.1.0"
