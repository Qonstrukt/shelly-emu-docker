#!/usr/bin/env python3
"""Tiny fake Home Assistant WebSocket server for testing shelly-emu without a real HA.

Speaks the subset shelly-emu uses: auth, subscribe_entities (compressed state events),
call_service (switch/homeassistant turn_on/turn_off). Entities are invented on demand:
``switch.*`` start off, ``sensor.*power*`` get a wandering wattage, ``sensor.*energy*`` /
``*import*`` / ``*export*`` count up in kWh, ``sensor.*voltage*`` hover around 230 V.

Usage: python3 tools/ha_stub.py [--port 8123] [--token test]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import time

from aiohttp import WSMsgType, web

_LOGGER = logging.getLogger("ha_stub")


class Stub:
    def __init__(self, token: str) -> None:
        self.token = token
        self.states: dict[str, dict] = {}
        self.subscribers: list[tuple[web.WebSocketResponse, int, set[str]]] = []

    def entity(self, entity_id: str) -> dict:
        if entity_id not in self.states:
            domain = entity_id.split(".", 1)[0]
            name = entity_id.replace(".", " ").replace("_", " ").title()
            attrs = {"friendly_name": name}
            if domain == "sensor":
                if "voltage" in entity_id:
                    state, attrs["unit_of_measurement"] = "230.0", "V"
                elif "energy" in entity_id or "import" in entity_id or "export" in entity_id:
                    state, attrs["unit_of_measurement"] = f"{random.uniform(1, 500):.3f}", "kWh"
                elif "current" in entity_id:
                    state, attrs["unit_of_measurement"] = "0.5", "A"
                else:
                    state, attrs["unit_of_measurement"] = f"{random.uniform(0, 800):.1f}", "W"
            elif domain in ("switch", "light", "input_boolean", "binary_sensor"):
                state = "off"
            else:
                state = "unknown"
            self.states[entity_id] = {"s": state, "a": attrs, "lc": time.time(), "lu": time.time(), "c": "ctx"}
        return self.states[entity_id]

    async def set_state(self, entity_id: str, state: str) -> None:
        ent = self.entity(entity_id)
        if ent["s"] == state:
            return
        ent["s"] = state
        ent["lu"] = ent["lc"] = time.time()
        _LOGGER.info("%s = %s", entity_id, state)
        for ws, sub_id, ids in list(self.subscribers):
            if entity_id in ids and not ws.closed:
                await ws.send_json({"id": sub_id, "type": "event", "event": {"c": {entity_id: {"+": {"s": state, "lu": ent["lu"]}}}}})

    async def wander(self) -> None:
        while True:
            await asyncio.sleep(5)
            for entity_id, ent in list(self.states.items()):
                unit = ent["a"].get("unit_of_measurement")
                if unit == "W":
                    await self.set_state(entity_id, f"{max(0.0, float(ent['s']) + random.uniform(-50, 50)):.1f}")
                elif unit == "kWh":
                    await self.set_state(entity_id, f"{float(ent['s']) + 0.001:.3f}")
                elif unit == "V":
                    await self.set_state(entity_id, f"{random.uniform(228, 233):.1f}")

    async def handle(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required", "ha_version": "2026.10.0"})
        msg = await ws.receive_json()
        if msg.get("type") != "auth" or msg.get("access_token") != self.token:
            await ws.send_json({"type": "auth_invalid", "message": "Invalid access token"})
            await ws.close()
            return ws
        await ws.send_json({"type": "auth_ok", "ha_version": "2026.10.0"})
        _LOGGER.info("client authenticated")
        try:
            async for raw in ws:
                if raw.type != WSMsgType.TEXT:
                    break
                msg = json.loads(raw.data)
                mid, mtype = msg.get("id"), msg.get("type")
                if mtype == "subscribe_entities":
                    ids = set(msg.get("entity_ids") or [])
                    self.subscribers.append((ws, mid, ids))
                    await ws.send_json({"id": mid, "type": "result", "success": True, "result": None})
                    await ws.send_json({"id": mid, "type": "event", "event": {"a": {e: self.entity(e) for e in sorted(ids)}}})
                elif mtype == "call_service":
                    target = msg.get("target") or {}
                    entity_id = target.get("entity_id")
                    service = msg.get("service")
                    _LOGGER.info("call_service %s.%s %s", msg.get("domain"), service, entity_id)
                    if service in ("turn_on", "turn_off") and entity_id:
                        await ws.send_json({"id": mid, "type": "result", "success": True, "result": {"context": {"id": "ctx"}}})
                        # a real switch takes a moment to report back
                        new_state = "on" if service == "turn_on" else "off"
                        asyncio.get_running_loop().call_later(
                            0.5, lambda e=entity_id, st=new_state: asyncio.ensure_future(self.set_state(e, st))
                        )
                    else:
                        await ws.send_json({"id": mid, "type": "result", "success": False, "error": {"code": "not_found", "message": "unsupported"}})
                elif mtype == "ping":
                    await ws.send_json({"id": mid, "type": "pong"})
                else:
                    await ws.send_json({"id": mid, "type": "result", "success": False, "error": {"code": "unknown_command", "message": mtype}})
        finally:
            self.subscribers = [s for s in self.subscribers if s[0] is not ws]
            _LOGGER.info("client disconnected")
        return ws


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--token", default="test")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    stub = Stub(args.token)
    app = web.Application()
    app.router.add_get("/api/websocket", stub.handle)

    async def start_wander(app):
        app["wander"] = asyncio.get_running_loop().create_task(stub.wander())

    app.on_startup.append(start_wander)
    _LOGGER.info("fake Home Assistant on ws://%s:%d/api/websocket (token %r)", args.host, args.port, args.token)
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
