"""Shelly Gen2 HTTP transport: ``GET /shelly``, ``GET|POST /rpc``, ``GET|POST /rpc/<Method>``."""

from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import web

from .device import ShellyDevice
from .rpc import RpcDispatcher, RpcError

_LOGGER = logging.getLogger(__name__)

SERVER_HEADER = "ShellyHTTP/1.0.0"
MAX_BODY = 8192


def coerce_params(query: Any, skip: str | None = None) -> dict[str, Any]:
    """Shelly GET semantics: every query parameter is an RPC parameter; values that parse as
    JSON (numbers, booleans, objects) are used as such, everything else stays a string."""
    out: dict[str, Any] = {}
    for key, value in query.items():
        if key == skip:
            continue
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            parsed = value
        out[key] = value if isinstance(parsed, str) else parsed
    return out


def json_response(payload: Any, status: int = 200) -> web.Response:
    resp = web.Response(
        text=json.dumps(payload, separators=(",", ":")),
        status=status,
        content_type="application/json",
    )
    resp.headers["Server"] = SERVER_HEADER
    return resp


class ShellyHttpServer:
    def __init__(self, device: ShellyDevice, rpc: RpcDispatcher, host: str, port: int) -> None:
        self.device = device
        self.rpc = rpc
        self.host = host
        self.port = port
        self.app = web.Application(client_max_size=MAX_BODY)
        self.app.router.add_route("GET", "/shelly", self.handle_shelly)
        self.app.router.add_route("GET", "/rpc", self.handle_rpc)
        self.app.router.add_route("POST", "/rpc", self.handle_rpc)
        self.app.router.add_route("GET", "/rpc/{method}", self.handle_rpc_method)
        self.app.router.add_route("POST", "/rpc/{method}", self.handle_rpc_method)
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port, reuse_address=True)
        await site.start()
        _LOGGER.info("%s HTTP listening on %s:%d", self.device.device_id, self.host, self.port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _read_json_body(self, request: web.Request) -> Any:
        body = await request.read()
        if not body:
            return None
        try:
            return json.loads(body)
        except ValueError:
            raise web.HTTPBadRequest(
                text=json.dumps({"code": -32700, "message": "Invalid JSON"}),
                content_type="application/json",
                headers={"Server": SERVER_HEADER},
            ) from None

    def _bare(self, method: str, params: Any) -> web.Response:
        try:
            result = self.rpc.call(method, params)
        except RpcError as err:
            return json_response(err.to_json(), 404 if err.code == 404 else 500)
        return json_response(result)

    async def handle_shelly(self, request: web.Request) -> web.Response:
        return json_response(self.rpc.device_info())

    async def handle_rpc(self, request: web.Request) -> web.Response:
        if request.method == "POST":
            body = await self._read_json_body(request)
            if body is not None:
                if isinstance(body, list):
                    return json_response([self.rpc.envelope(item) for item in body])
                return json_response(self.rpc.envelope(body))
        # GET /rpc?method=Switch.Set&id=0&on=true (bare result)
        method = request.query.get("method")
        if not method:
            return json_response({"code": -32600, "message": "Missing 'method'"}, 400)
        params_json = request.query.get("params")
        if params_json is not None:
            try:
                params = json.loads(params_json)
            except ValueError:
                params = {}
        else:
            params = coerce_params(request.query, skip="method")
        return self._bare(method, params)

    async def handle_rpc_method(self, request: web.Request) -> web.Response:
        method = request.match_info["method"]
        params: Any = None
        if request.method == "POST":
            params = await self._read_json_body(request)
        if params is None:
            params = coerce_params(request.query)
        return self._bare(method, params)
