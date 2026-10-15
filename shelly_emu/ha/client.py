"""Home Assistant WebSocket API client.

Uses ``subscribe_entities`` (compressed state updates, HA 2022.4+) for the entities
the emulator needs and ``call_service`` for switching. Reconnects with backoff.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

StateCallback = Callable[[str, str | None, dict[str, Any]], None]
ConnectionCallback = Callable[[bool], None]

UNAVAILABLE_STATES = ("unavailable", "unknown", "", None)


class HomeAssistantError(Exception):
    pass


def websocket_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if url.startswith("http://"):
        url = "ws://" + url[7:]
    elif url.startswith("https://"):
        url = "wss://" + url[8:]
    elif not url.startswith(("ws://", "wss://")):
        url = "ws://" + url
    if not url.endswith("/api/websocket"):
        url += "/api/websocket"
    return url


def parse_state_number(state: str | None) -> float | None:
    """Sensor state -> float, ``None`` for unavailable/unknown/non-numeric."""
    if state in UNAVAILABLE_STATES:
        return None
    try:
        return float(state)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


class HomeAssistantClient:
    def __init__(
        self,
        url: str,
        token: str,
        entity_ids: Iterable[str],
        on_state: StateCallback,
        on_connection: ConnectionCallback | None = None,
    ) -> None:
        self.url = websocket_url(url)
        self.token = token
        self.entity_ids = sorted(set(entity_ids))
        self.on_state = on_state
        self.on_connection = on_connection
        self.states: dict[str, dict[str, Any]] = {}  # entity_id -> {"s": state, "a": attrs}
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._stopping = False
        self.connected = False

    # ------------------------------------------------------------------ public
    async def run(self) -> None:
        """Connect and keep connected until :meth:`stop`."""
        backoff = 1.0
        while not self._stopping:
            try:
                await self._session()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Home Assistant connection failed: %s", err)
            if self._stopping:
                break
            self._set_connected(False)
            _LOGGER.info("reconnecting to Home Assistant in %.0f s", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    async def stop(self) -> None:
        self._stopping = True
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()

    async def call_service(self, domain: str, service: str, entity_id: str, data: dict | None = None) -> Any:
        msg: dict[str, Any] = {
            "type": "call_service",
            "domain": domain,
            "service": service,
            "target": {"entity_id": entity_id},
        }
        if data:
            msg["service_data"] = data
        return await self._request(msg)

    async def turn(self, entity_id: str, on: bool) -> None:
        """switch.turn_on/off for switches, homeassistant.turn_on/off for everything else."""
        domain = entity_id.split(".", 1)[0]
        service_domain = "switch" if domain == "switch" else "homeassistant"
        try:
            await self.call_service(service_domain, "turn_on" if on else "turn_off", entity_id)
            _LOGGER.info("HA %s -> %s", entity_id, "on" if on else "off")
        except HomeAssistantError as err:
            _LOGGER.error("HA %s -> %s failed: %s", entity_id, "on" if on else "off", err)

    def state(self, entity_id: str) -> str | None:
        s = self.states.get(entity_id)
        return None if s is None else s.get("s")

    def attributes(self, entity_id: str) -> dict[str, Any]:
        s = self.states.get(entity_id)
        return {} if s is None else s.get("a", {})

    # ------------------------------------------------------------------ internals
    def _set_connected(self, value: bool) -> None:
        if self.connected != value:
            self.connected = value
            if self.on_connection is not None:
                self.on_connection(value)

    async def _request(self, msg: dict[str, Any], timeout: float = 15.0) -> Any:
        ws = self._ws
        if ws is None or ws.closed or not self.connected:
            raise HomeAssistantError("not connected")
        msg_id = self._next_id
        self._next_id += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        try:
            await ws.send_json({"id": msg_id, **msg})
            return await asyncio.wait_for(fut, timeout)
        except TimeoutError:
            raise HomeAssistantError("timeout") from None
        finally:
            self._pending.pop(msg_id, None)

    async def _session(self) -> None:
        timeout = aiohttp.ClientTimeout(total=None, connect=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            _LOGGER.info("connecting to Home Assistant at %s", self.url)
            async with session.ws_connect(self.url, heartbeat=30) as ws:
                self._ws = ws
                try:
                    await self._authenticate(ws)
                    await self._subscribe(ws)
                    self._set_connected(True)
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            self._handle(msg.json())
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
                finally:
                    self._ws = None
                    for fut in self._pending.values():
                        if not fut.done():
                            fut.set_exception(HomeAssistantError("disconnected"))
                    self._pending.clear()
        if not self._stopping:
            raise HomeAssistantError("connection closed")

    async def _authenticate(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        msg = await ws.receive_json(timeout=15)
        if msg.get("type") != "auth_required":
            raise HomeAssistantError(f"unexpected greeting: {msg}")
        await ws.send_json({"type": "auth", "access_token": self.token})
        msg = await ws.receive_json(timeout=15)
        if msg.get("type") != "auth_ok":
            raise HomeAssistantError(f"authentication failed: {msg.get('message', msg)}")
        _LOGGER.info("authenticated with Home Assistant %s", msg.get("ha_version", ""))

    async def _subscribe(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self._next_id = 1
        self._sub_id = self._next_id
        self._next_id += 1
        await ws.send_json({"id": self._sub_id, "type": "subscribe_entities", "entity_ids": self.entity_ids})
        # The result for the subscription comes before the first event.
        while True:
            msg = await ws.receive_json(timeout=15)
            if msg.get("id") == self._sub_id and msg.get("type") == "result":
                if not msg.get("success"):
                    raise HomeAssistantError(f"subscribe_entities failed: {msg.get('error')}")
                return
            self._handle(msg)

    def _handle(self, msg: dict[str, Any]) -> None:
        mtype = msg.get("type")
        if mtype == "event" and msg.get("id") == getattr(self, "_sub_id", None):
            self._handle_entities_event(msg.get("event") or {})
        elif mtype == "result":
            fut = self._pending.get(msg.get("id"))
            if fut is not None and not fut.done():
                if msg.get("success"):
                    fut.set_result(msg.get("result"))
                else:
                    err = msg.get("error") or {}
                    fut.set_exception(HomeAssistantError(f"{err.get('code')}: {err.get('message')}"))
        elif mtype == "pong":
            pass

    def _handle_entities_event(self, event: dict[str, Any]) -> None:
        for entity_id, data in (event.get("a") or {}).items():
            self.states[entity_id] = {"s": data.get("s"), "a": dict(data.get("a") or {})}
            self._emit(entity_id)
        for entity_id, change in (event.get("c") or {}).items():
            cur = self.states.setdefault(entity_id, {"s": None, "a": {}})
            plus = change.get("+") or {}
            if "s" in plus:
                cur["s"] = plus["s"]
            if "a" in plus:
                cur["a"].update(plus["a"])
            minus = change.get("-") or {}
            for key in minus.get("a") or []:
                cur["a"].pop(key, None)
            self._emit(entity_id)
        for entity_id in event.get("r") or []:
            self.states[entity_id] = {"s": None, "a": {}}
            self._emit(entity_id)

    def _emit(self, entity_id: str) -> None:
        cur = self.states[entity_id]
        try:
            self.on_state(entity_id, cur["s"], cur["a"])
        except Exception:  # noqa: BLE001
            _LOGGER.exception("state callback failed for %s", entity_id)
