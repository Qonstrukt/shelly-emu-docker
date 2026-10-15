"""mDNS advertisement: ``_shelly._tcp`` (TXT gen/app/ver) and ``_http._tcp`` (TXT gen) under
the Shelly hostname (``ShellyPro4PM-AABBCCDDEEFF.local``) with the device id as instance name."""

from __future__ import annotations

import logging
import socket

from zeroconf import IPVersion, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

from .device import ShellyDevice

_LOGGER = logging.getLogger(__name__)


def _force_ttl_255(zc) -> None:
    """RFC 6762 requires IP TTL 255 on every mDNS packet. python-zeroconf sets the multicast TTL,
    but unicast replies (to QU questions, which Shelly controllers send) use the socket's
    unicast TTL. Shelly controllers drop replies with a lower TTL, so raise it on every socket."""
    engine = getattr(zc, "engine", None)
    sockets: dict[int, object] = {}
    for wrapped in list(getattr(engine, "senders", [])) + list(getattr(engine, "readers", [])):
        s = getattr(wrapped, "sock", None)
        if s is not None:
            sockets[s.fileno()] = s
    patched = 0
    for s in sockets.values():
        try:
            if s.family == socket.AF_INET:
                s.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, 255)
            elif s.family == socket.AF_INET6:
                s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_UNICAST_HOPS, 255)
            patched += 1
        except OSError as err:
            _LOGGER.debug("could not set TTL 255 on mDNS socket: %s", err)
    if patched:
        _LOGGER.debug("set unicast TTL 255 on %d mDNS socket(s)", patched)
    else:
        _LOGGER.warning("could not find the mDNS sockets to set TTL 255; unicast replies keep the default TTL")


async def create_zeroconf(interfaces: list[str] | None = None) -> AsyncZeroconf:
    """One responder per process; every emulated device registers its services on it."""
    kwargs: dict = {"ip_version": IPVersion.V4Only}
    if interfaces:
        kwargs["interfaces"] = interfaces
    zc = AsyncZeroconf(**kwargs)
    await zc.zeroconf.async_wait_for_start()
    _force_ttl_255(zc.zeroconf)
    return zc


def service_infos(device: ShellyDevice, ip: str, port: int) -> list[ServiceInfo]:
    addresses = [socket.inet_aton(ip)]
    server = f"{device.hostname}.local."
    return [
        ServiceInfo(
            "_shelly._tcp.local.",
            f"{device.device_id}._shelly._tcp.local.",
            addresses=addresses,
            port=port,
            properties={"gen": "2", "app": device.model.app, "ver": device.spec.fw_version},
            server=server,
        ),
        ServiceInfo(
            "_http._tcp.local.",
            f"{device.device_id}._http._tcp.local.",
            addresses=addresses,
            port=port,
            properties={"gen": "2"},
            server=server,
        ),
    ]


class MdnsAdvertiser:
    def __init__(self, zc: AsyncZeroconf, device: ShellyDevice, ip: str, port: int) -> None:
        self.zc = zc
        self.device = device
        self.ip = ip
        self.port = port
        self._infos: list[ServiceInfo] = []

    async def start(self) -> None:
        self._infos = service_infos(self.device, self.ip, self.port)
        for info in self._infos:
            await self.zc.async_register_service(info, allow_name_change=False)
        _LOGGER.info(
            "%s mDNS: %s.local (%s) advertising _shelly._tcp as %s",
            self.device.device_id, self.device.hostname, self.ip, self.device.device_id,
        )

    async def stop(self) -> None:
        for info in self._infos:
            try:
                await self.zc.async_unregister_service(info)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("mDNS unregister failed: %s", err)
        self._infos = []
