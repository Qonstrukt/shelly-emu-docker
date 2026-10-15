"""JSON-RPC dispatcher and Shelly Gen2 payload builders.

Payload shapes follow the Shelly Gen2 API docs and the emulators known to pair with
Sigenergy mySigen (Tasmota Pro 2PM, sigelly_emu Pro 3EM); they are a direct port of
the verified ESPHome component.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .device import ShellyDevice
from .models import EM_PHASES
from .state import EmState, Snapshot, SwitchState

_LOGGER = logging.getLogger(__name__)

JsonObj = dict[str, Any]


class RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    @classmethod
    def method_not_found(cls) -> RpcError:
        return cls(404, "Method not found")

    @classmethod
    def invalid_argument(cls, what: str) -> RpcError:
        return cls(-105, f"Invalid argument '{what}'")

    def to_json(self) -> JsonObj:
        return {"code": self.code, "message": self.message}


def r1(v: float) -> float:
    return round(v, 1)


def r2(v: float) -> float:
    return round(v, 2)


def r3(v: float) -> float:
    return round(v, 3)


def _time_synced(now: float) -> bool:
    return now > 1600000000


def _minute_ts(now: float) -> int:
    if not _time_synced(now):
        return 0
    n = int(now)
    return n - n % 60


def param_id(params: Any) -> int:
    if not isinstance(params, dict):
        return -1
    v = params.get("id")
    if isinstance(v, bool):
        return -1
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str) and v[:1].isdigit():
        try:
            return int(v)
        except ValueError:
            return -1
    return -1


class RpcDispatcher:
    """Dispatch Shelly RPC methods against one :class:`ShellyDevice`."""

    def __init__(self, device: ShellyDevice) -> None:
        self.dev = device

    # ------------------------------------------------------------------ Switch / Input
    def switch_status(self, index: int, s: SwitchState) -> JsonObj:
        now = self.dev.now()
        ts = _minute_ts(now)
        if s.temp is None:
            temp = {"tC": 0.0, "tF": 32.0}
        else:
            temp = {"tC": r1(s.temp), "tF": r1(s.temp * 9.0 / 5.0 + 32.0)}
        cid = self.dev.switch_component_id(index)
        if self.dev.spec.output_addon:
            # Pro Output Add-on relay: no power metering, just the output state.
            return {"id": cid, "source": "init", "output": s.output, "temperature": temp}
        return {
            "id": cid,
            "source": "init",
            "tag": None,
            "output": s.output,
            "apower": r1(s.apower),
            "voltage": 0.0 if s.voltage is None else r1(s.voltage),
            "freq": 0.0 if s.freq is None else r1(s.freq),
            "current": r3(s.current),
            "pf": r2(s.pf),
            "aenergy": {"total": r3(s.aenergy), "by_minute": [0.0, 0.0, 0.0], "minute_ts": ts},
            "ret_aenergy": {"total": r3(s.ret_aenergy), "by_minute": [0.0, 0.0, 0.0], "minute_ts": ts},
            "counts": {
                "on_time": 0,
                "on_time_rst_ts": 0,
                "switch_on": 0,
                "switch_on_rst_ts": 0,
                "on_above_thr": 0,
                "on_above_thr_rst_ts": 0,
            },
            "temperature": temp,
        }

    def switch_config(self, index: int) -> JsonObj:
        name = self.dev.channel_name(index)
        cid = self.dev.switch_component_id(index)
        if self.dev.spec.output_addon:
            return {
                "id": cid,
                "name": name or None,
                "initial_state": "restore_last",
                "auto_on": False,
                "auto_on_delay": 60.0,
                "auto_off": False,
                "auto_off_delay": 60.0,
            }
        return {
            "id": cid,
            "name": name or None,
            "in_mode": "detached",
            "in_locked": False,
            "initial_state": "restore_last",
            "auto_on": False,
            "auto_on_delay": 60.0,
            "auto_off": False,
            "auto_off_delay": 60.0,
            "power_limit": 4480,
            "voltage_limit": 280,
            "undervoltage_limit": 0,
            "autorecover_voltage_errors": False,
            "current_limit": 16.0,
            "reverse": False,
            "counts": {"enable": True, "power_thr": 100},
        }

    @staticmethod
    def input_status(cid: int, s: SwitchState) -> JsonObj:
        return {"id": cid, "state": s.input}

    @staticmethod
    def input_config(cid: int) -> JsonObj:
        return {"id": cid, "name": None, "type": "switch", "enable": True, "invert": False}

    # ------------------------------------------------------------------ EM / EMData / Temperature
    @staticmethod
    def em_status(em: EmState) -> JsonObj:
        o: JsonObj = {"id": 0}
        total_current = total_act = total_aprt = 0.0
        for p, e in zip("abc", em.phase):
            o[f"{p}_current"] = r3(e.current)
            o[f"{p}_voltage"] = r1(e.voltage)
            o[f"{p}_act_power"] = r1(e.act_power)
            o[f"{p}_aprt_power"] = r1(e.aprt_power)
            o[f"{p}_pf"] = r2(e.pf)
            o[f"{p}_freq"] = r1(e.freq)
            total_current += e.current
            total_act += e.act_power
            total_aprt += e.aprt_power
        o["n_current"] = None if em.n_current is None else r3(em.n_current)
        o["total_current"] = r3(total_current)
        o["total_act_power"] = r3(total_act)
        o["total_aprt_power"] = r3(total_aprt)
        o["user_calibrated_phase"] = []
        return o

    @staticmethod
    def em_config() -> JsonObj:
        return {
            "id": 0,
            "name": None,
            "blink_mode_selector": "active_energy",
            "phase_selector": "all",
            "monitor_phase_sequence": False,
            "ct_type": "120A",
            "reverse": {},
            "alarms": {p: {k: [None, None] for k in ("voltage", "current", "power")} for p in "abc"},
        }

    @staticmethod
    def emdata_status(em: EmState) -> JsonObj:
        o: JsonObj = {"id": 0}
        for p, e in zip("abc", em.phase):
            o[f"{p}_total_act_energy"] = r2(e.energy)
            o[f"{p}_total_act_ret_energy"] = r2(e.ret_energy)
        o["total_act"] = r2(sum(e.energy for e in em.phase))
        o["total_act_ret"] = r2(sum(e.ret_energy for e in em.phase))
        return o

    @staticmethod
    def temperature_status(em: EmState) -> JsonObj:
        t = 45.0 if em.temp is None else em.temp
        return {"id": 0, "tC": r1(t), "tF": r1(t * 9.0 / 5.0 + 32.0)}

    @staticmethod
    def temperature_config() -> JsonObj:
        return {"id": 0, "name": None, "report_thr_C": 5.0, "offset_C": 0.0}

    # ------------------------------------------------------------------ Sys / Wifi / Eth / BLE / Cloud / Mqtt / WS
    def sys_status(self) -> JsonObj:
        dev = self.dev
        now = dev.now()
        o: JsonObj = {"mac": dev.mac, "restart_required": False}
        if _time_synced(now):
            lt = time.localtime(now)
            o["time"] = time.strftime("%H:%M", lt)
            o["unixtime"] = int(now)
            o["last_sync_ts"] = int(now)
            o["utc_offset"] = lt.tm_gmtoff
        else:
            o.update({"time": None, "unixtime": None, "last_sync_ts": None, "utc_offset": 0})
        o.update(
            {
                "uptime": dev.uptime,
                "ram_size": 246580,
                "ram_free": 151284,
                "ram_min_free": 139860,
                "fs_size": 458752,
                "fs_free": 212992,
                "cfg_rev": dev.cfg_rev,
                "kvs_rev": 0,
                "schedule_rev": 0,
                "webhook_rev": 0,
                "btrelay_rev": 0,
            }
        )
        if not dev.is_meter:
            o["bthc_rev"] = 0
        o["available_updates"] = {}
        o["reset_reason"] = 3
        return o

    def sys_config(self) -> JsonObj:
        dev = self.dev
        device: JsonObj = {
            "name": dev.device_name,
            "mac": dev.mac,
            "fw_id": dev.spec.fw_id,
            "discoverable": True,
            "eco_mode": False,
        }
        if dev.model.profile is not None:
            device["profile"] = dev.model.profile
        device.update(
            {
                "addon_type": "prooutput" if dev.has_output_addon else None,
                "sys_btn_toggle": False,
                "tls_check_cert_validity_time": True,
                "enhanced_security": False,
            }
        )
        return {
            "device": device,
            "location": {"tz": dev.spec.timezone, "lat": 0.0, "lon": 0.0},
            "debug": {
                "level": 2,
                "file_level": None,
                "mqtt": {"enable": False},
                "websocket": {"enable": False},
                "file_log": {"enable": False},
                "udp": {"addr": None},
            },
            "ui_data": {"device_revision": "1-22"},
            "rpc_udp": {"dst_addr": None, "listen_port": None},
            "sntp": {"server": "time.google.com"},
            "cfg_rev": dev.cfg_rev,
        }

    def _bssid(self) -> str:
        b = bytes.fromhex(self.dev.mac)
        b = b[:5] + bytes([(b[5] + 1) & 0xFF])
        return ":".join(f"{x:02x}" for x in b)

    def wifi_status(self) -> JsonObj:
        dev = self.dev
        ssid = dev.spec.wifi_ssid
        o: JsonObj = {"sta_ip": dev.ip, "status": "got ip"}
        if ssid:
            o.update({"ssid": ssid, "channel": 6, "rssi": -55, "bssid": self._bssid()})
        else:
            o.update({"ssid": None, "rssi": 0})
        o["sta_ip6"] = []
        return o

    def wifi_config(self) -> JsonObj:
        dev = self.dev
        sta_tpl: JsonObj = {"ipv4mode": "dhcp", "ip": None, "netmask": None, "gw": None, "nameserver": None}
        return {
            "ap": {"ssid": dev.hostname, "is_open": True, "enable": False, "range_extender": {"enable": False}},
            "sta": {"ssid": dev.spec.wifi_ssid, "is_open": False, "enable": True, **sta_tpl},
            "sta1": {"ssid": None, "is_open": True, "enable": False, **sta_tpl},
            "roam": {"rssi_thr": -80, "interval": 60},
        }

    def wifi_scan(self) -> JsonObj:
        ssid = self.dev.spec.wifi_ssid
        results = []
        if ssid:
            results.append({"ssid": ssid, "bssid": self._bssid(), "auth": 3, "channel": 6, "rssi": -55})
        return {"results": results}

    @staticmethod
    def eth_status() -> JsonObj:
        return {"ip": None, "ip6": None}

    @staticmethod
    def eth_config() -> JsonObj:
        return {
            "enable": True,
            "server_mode": False,
            "ipv4mode": "dhcp",
            "ip": None,
            "netmask": None,
            "gw": None,
            "nameserver": None,
        }

    def ble_status(self) -> JsonObj:
        # Shelly Gen2 derives its public BLE address from the Wi-Fi MAC + 2.
        b = bytes.fromhex(self.dev.mac)
        b = b[:5] + bytes([(b[5] + 2) & 0xFF])
        return {"addr": ":".join(f"{x:02x}" for x in b) + ",p", "flags": []}

    @staticmethod
    def ble_config() -> JsonObj:
        return {"rpc": {"enable": False}}

    @staticmethod
    def cloud_status() -> JsonObj:
        return {"connected": False}

    @staticmethod
    def cloud_config() -> JsonObj:
        return {"enable": False, "server": "iot.shelly.cloud:6012/jrpc"}

    @staticmethod
    def mqtt_status() -> JsonObj:
        return {"connected": False}

    def mqtt_config(self) -> JsonObj:
        return {
            "enable": False,
            "server": None,
            "client_id": self.dev.device_id,
            "user": None,
            "ssl_ca": None,
            "topic_prefix": self.dev.device_id,
            "rpc_ntf": True,
            "status_ntf": False,
            "use_client_cert": False,
            "enable_rpc": True,
            "enable_control": True,
        }

    @staticmethod
    def ws_status() -> JsonObj:
        return {"connected": False}

    @staticmethod
    def ws_config() -> JsonObj:
        return {"enable": False, "server": None, "ssl_ca": "ca.pem"}

    @staticmethod
    def knx_config() -> JsonObj:
        return {"enable": False, "ia": "15.15.255", "routing": {"addr": "224.0.23.12:3671"}}

    # ------------------------------------------------------------------ device info, components
    def device_info(self) -> JsonObj:
        dev = self.dev
        o: JsonObj = {
            "name": dev.device_name,
            "id": dev.device_id,
            "mac": dev.mac,
            "slot": 0,
            "model": dev.model_code,
            "gen": 2,
            "fw_id": dev.spec.fw_id,
            "ver": dev.spec.fw_version,
            "app": dev.model.app,
            "auth_en": False,
            "auth_domain": None,
        }
        if dev.model.profile is not None:
            o["profile"] = dev.model.profile
        o["provision"] = "complete"
        o["enhanced_security"] = False
        return o

    def component_keys(self) -> list[str]:
        dev = self.dev
        m = dev.model
        keys = ["ble", "bthome", "cloud"]
        if m.is_meter:
            keys += ["em:0", "emdata:0"]
        if m.is_pro:
            keys.append("eth")
        if m.has_inputs:
            keys += [f"input:{i}" for i in range(dev.switch_count)]
        if m.is_pro and not m.is_meter:
            keys.append("knx")
        if m.is_meter:
            keys.append("modbus")
        keys.append("mqtt")
        keys += [f"switch:{dev.switch_component_id(i)}" for i in range(dev.switch_count)]
        keys.append("sys")
        if m.is_meter:
            keys.append("temperature:0")
        keys += ["wifi", "ws"]
        return keys

    def component_status(self, key: str, snap: Snapshot) -> JsonObj | None:
        if key == "ble":
            return self.ble_status()
        if key in ("bthome", "knx", "modbus"):
            return {}
        if key == "cloud":
            return self.cloud_status()
        if key == "em:0":
            return self.em_status(snap.em)
        if key == "emdata:0":
            return self.emdata_status(snap.em)
        if key == "eth":
            return self.eth_status()
        if key.startswith("input:"):
            cid = int(key[6:])
            return self.input_status(cid, snap.sw[cid])
        if key == "mqtt":
            return self.mqtt_status()
        if key.startswith("switch:"):
            index = self.dev.switch_index_for_id(int(key[7:]))
            return None if index < 0 else self.switch_status(index, snap.sw[index])
        if key == "sys":
            return self.sys_status()
        if key == "temperature:0":
            return self.temperature_status(snap.em)
        if key == "wifi":
            return self.wifi_status()
        if key == "ws":
            return self.ws_status()
        return None

    def component_config(self, key: str) -> JsonObj | None:
        if key == "ble":
            return self.ble_config()
        if key in ("bthome", "emdata:0"):
            return {}
        if key == "cloud":
            return self.cloud_config()
        if key == "em:0":
            return self.em_config()
        if key == "eth":
            return self.eth_config()
        if key.startswith("input:"):
            return self.input_config(int(key[6:]))
        if key == "knx":
            return self.knx_config()
        if key == "modbus":
            return {"enable": True}
        if key == "mqtt":
            return self.mqtt_config()
        if key.startswith("switch:"):
            index = self.dev.switch_index_for_id(int(key[7:]))
            return None if index < 0 else self.switch_config(index)
        if key == "sys":
            return self.sys_config()
        if key == "temperature:0":
            return self.temperature_config()
        if key == "wifi":
            return self.wifi_config()
        if key == "ws":
            return self.ws_config()
        return None

    def full_status(self, snap: Snapshot) -> JsonObj:
        return {k: self.component_status(k, snap) for k in self.component_keys()}

    def full_config(self) -> JsonObj:
        return {k: self.component_config(k) for k in self.component_keys()}

    def components(self, params: Any, snap: Snapshot) -> JsonObj:
        params = params if isinstance(params, dict) else {}
        want_status = want_config = True
        include = params.get("include")
        if isinstance(include, list):
            want_status = "status" in include
            want_config = "config" in include
        dynamic_only = bool(params.get("dynamic_only", False))
        offset = params.get("offset", 0)
        offset = max(int(offset), 0) if isinstance(offset, (int, float)) else 0
        keys = [] if dynamic_only else self.component_keys()
        comps = []
        for key in keys[offset:]:
            c: JsonObj = {"key": key}
            if want_status:
                c["status"] = self.component_status(key, snap)
            if want_config:
                c["config"] = self.component_config(key)
            comps.append(c)
        return {"components": comps, "cfg_rev": self.dev.cfg_rev, "offset": offset, "total": len(keys)}

    def list_methods(self) -> JsonObj:
        m = self.dev.model
        methods = [
            "Shelly.GetDeviceInfo", "Shelly.GetStatus", "Shelly.GetConfig", "Shelly.GetComponents", "Shelly.ListMethods",
            "Shelly.CheckForUpdate", "Shelly.Reboot", "RPC.Ping", "Sys.GetStatus", "Sys.GetConfig",
            "Sys.SetConfig", "Wifi.GetStatus", "Wifi.GetConfig", "Wifi.SetConfig", "Wifi.Scan",
            "BLE.GetStatus", "BLE.GetConfig", "BLE.SetConfig", "Cloud.GetStatus", "Cloud.GetConfig",
            "Cloud.SetConfig", "Mqtt.GetStatus", "Mqtt.GetConfig", "WS.GetStatus", "WS.GetConfig",
            "Schedule.List", "Webhook.List", "Script.List", "KVS.GetMany",
        ]  # fmt: skip
        if m.is_pro:
            methods += ["Eth.GetStatus", "Eth.GetConfig"]
        if m.is_meter:
            methods += [
                "EM.GetStatus", "EM.GetConfig", "EMData.GetStatus", "EMData.GetConfig",
                "Temperature.GetStatus", "Temperature.GetConfig", "Modbus.GetStatus", "Modbus.GetConfig",
            ]  # fmt: skip
            if self.dev.has_output_addon:
                methods += [
                    "Switch.GetStatus", "Switch.GetConfig", "Switch.SetConfig", "Switch.Set", "Switch.Toggle",
                    "ProOutputAddon.GetPeripherals", "ProOutputAddon.AddPeripheral", "ProOutputAddon.RemovePeripheral",
                ]  # fmt: skip
        else:
            methods += [
                "Switch.GetStatus", "Switch.GetConfig", "Switch.SetConfig", "Switch.Set", "Switch.Toggle",
                "Switch.ResetCounters", "Input.GetStatus", "Input.GetConfig", "Input.SetConfig",
            ]  # fmt: skip
            if m.app in ("Pro2PM", "Plus2PM"):
                methods.append("Shelly.ListProfiles")
        return {"methods": methods}

    @staticmethod
    def list_profiles() -> JsonObj:
        return {
            "profiles": {
                "cover": {"components": [{"type": "input", "count": 2}, {"type": "cover", "count": 1}]},
                "switch": {"components": [{"type": "input", "count": 2}, {"type": "switch", "count": 2}]},
            }
        }

    # ------------------------------------------------------------------ dispatcher
    def call(self, method: str, params: Any = None) -> JsonObj | None:
        """Execute one RPC method. Returns the result (``None`` for void methods) or raises :class:`RpcError`."""
        dev = self.dev
        m = method.lower()
        snap = dev.snapshot()
        p = params if isinstance(params, dict) else {}
        ack = {"restart_required": False}
        _LOGGER.debug("%s rpc %s %s", dev.device_id, method, params if params else "")

        def is_(name: str) -> bool:
            return m == name.lower()

        # ---- Shelly.* ----
        if is_("Shelly.GetDeviceInfo"):
            return self.device_info()
        if is_("Shelly.GetStatus"):
            return self.full_status(snap)
        if is_("Shelly.GetConfig"):
            return self.full_config()
        if is_("Shelly.GetComponents"):
            return self.components(p, snap)
        if is_("Shelly.ListMethods"):
            return self.list_methods()
        if is_("Shelly.ListProfiles"):
            if dev.model.app not in ("Pro2PM", "Plus2PM"):
                raise RpcError.method_not_found()
            return self.list_profiles()
        if is_("Shelly.CheckForUpdate"):
            return {}
        if is_("Shelly.Reboot") or is_("Shelly.Update") or is_("Shelly.SetAuth") or is_("Shelly.DetectLocation"):
            return None
        if is_("RPC.Ping"):
            return {}
        # ---- Sys / Wifi / Eth / BLE / Cloud / Mqtt / WS ----
        if is_("Sys.GetStatus"):
            return self.sys_status()
        if is_("Sys.GetConfig"):
            return self.sys_config()
        if is_("Sys.SetConfig"):
            name = p.get("config", {}).get("device", {}).get("name") if isinstance(p.get("config"), dict) else None
            if isinstance(name, str):
                dev.set_device_name(name)
            return ack
        if is_("Wifi.GetStatus"):
            return self.wifi_status()
        if is_("Wifi.GetConfig"):
            return self.wifi_config()
        if is_("Wifi.SetConfig"):
            _LOGGER.info("%s Wifi.SetConfig received (ignored)", dev.device_id)
            return ack
        if is_("Wifi.Scan"):
            return self.wifi_scan()
        if is_("Wifi.ListAPClients"):
            return {"ap_clients": []}
        if is_("Eth.GetStatus"):
            if not dev.model.is_pro:
                raise RpcError.method_not_found()
            return self.eth_status()
        if is_("Eth.GetConfig"):
            if not dev.model.is_pro:
                raise RpcError.method_not_found()
            return self.eth_config()
        if is_("BLE.GetStatus"):
            return self.ble_status()
        if is_("BLE.GetConfig"):
            return self.ble_config()
        if m in (
            "ble.setconfig", "cloud.setconfig", "mqtt.setconfig", "ws.setconfig", "eth.setconfig",
            "modbus.setconfig", "em.setconfig", "emdata.setconfig", "temperature.setconfig",
        ):  # fmt: skip
            return ack
        if is_("Cloud.GetStatus"):
            return self.cloud_status()
        if is_("Cloud.GetConfig"):
            return self.cloud_config()
        if is_("Mqtt.GetStatus"):
            return self.mqtt_status()
        if is_("Mqtt.GetConfig"):
            return self.mqtt_config()
        if is_("WS.GetStatus"):
            return self.ws_status()
        if is_("WS.GetConfig"):
            return self.ws_config()
        # ---- harmless extras some apps call ----
        if is_("Schedule.List"):
            return {"jobs": [], "rev": 0}
        if is_("Webhook.List"):
            return {"hooks": [], "rev": 0}
        if is_("Script.List"):
            return {"scripts": []}
        if is_("KVS.GetMany") or is_("KVS.List"):
            return {"items": {}, "offset": 0, "total": 0, "rev": 0}
        # ---- Pro Output Add-on (pro3em with output_addon) ----
        if is_("ProOutputAddon.GetPeripherals"):
            if not dev.has_output_addon:
                raise RpcError.method_not_found()
            return {"digital_out": {f"switch:{dev.switch_component_id(i)}": {} for i in range(dev.switch_count)}}
        if is_("ProOutputAddon.AddPeripheral") or is_("ProOutputAddon.RemovePeripheral"):
            if not dev.has_output_addon:
                raise RpcError.method_not_found()
            raise RpcError(-108, "Peripheral configuration is fixed on this device")
        # ---- Switch / Input ----
        if m.startswith("switch.") or m.startswith("input."):
            if dev.is_meter and (not dev.has_output_addon or m.startswith("input.")):
                raise RpcError.method_not_found()
            cid = dev.switch_index_for_id(param_id(p))
            if cid < 0:
                raise RpcError.invalid_argument("id")
            if is_("Switch.GetStatus"):
                return self.switch_status(cid, snap.sw[cid])
            if is_("Switch.GetConfig"):
                return self.switch_config(cid)
            if is_("Switch.SetConfig") or is_("Input.SetConfig"):
                return ack
            if is_("Switch.ResetCounters"):
                return {"aenergy": {"total": r3(snap.sw[cid].aenergy)}}
            if is_("Switch.Set"):
                on = p.get("on")
                if isinstance(on, bool):
                    value = on
                elif isinstance(on, int):
                    value = on != 0
                else:
                    raise RpcError.invalid_argument("on")
                return {"was_on": dev.request_switch(cid, value)}
            if is_("Switch.Toggle"):
                return {"was_on": dev.request_switch(cid, not snap.sw[cid].output)}
            if is_("Input.GetStatus"):
                if not dev.model.has_inputs:
                    raise RpcError.invalid_argument("id")
                return self.input_status(cid, snap.sw[cid])
            if is_("Input.GetConfig"):
                if not dev.model.has_inputs:
                    raise RpcError.invalid_argument("id")
                return self.input_config(cid)
            raise RpcError.method_not_found()
        # ---- EM / EMData / Temperature / Modbus (Pro 3EM) ----
        if m.startswith(("em.", "emdata.", "temperature.", "modbus.")):
            if not dev.is_meter:
                raise RpcError.method_not_found()
            if param_id(p) > 0:
                raise RpcError.invalid_argument("id")
            if is_("EM.GetStatus"):
                return self.em_status(snap.em)
            if is_("EM.GetConfig"):
                return self.em_config()
            if is_("EM.GetCTTypes"):
                return {"supported": ["120A", "400A"]}
            if is_("EMData.GetStatus"):
                return self.emdata_status(snap.em)
            if is_("EMData.GetConfig"):
                return {}
            if is_("Temperature.GetStatus"):
                return self.temperature_status(snap.em)
            if is_("Temperature.GetConfig"):
                return self.temperature_config()
            if is_("Modbus.GetStatus"):
                return {}
            if is_("Modbus.GetConfig"):
                return {"enable": True}
            raise RpcError.method_not_found()
        _LOGGER.warning("%s unknown RPC method: %s", dev.device_id, method)
        raise RpcError.method_not_found()

    def envelope(self, request: Any) -> JsonObj:
        """Handle a framed request ``{"id","src","method","params"}`` and return the response envelope."""
        req = request if isinstance(request, dict) else {}
        method = req.get("method")
        src = req.get("src")
        env: JsonObj = {
            "id": req.get("id"),
            "src": self.dev.device_id,
            "dst": src if isinstance(src, str) else "user_1",
        }
        if not isinstance(method, str) or not method:
            env["error"] = {"code": -32600, "message": "Invalid Request"}
            return env
        try:
            env["result"] = self.call(method, req.get("params"))
        except RpcError as err:
            env["error"] = err.to_json()
        return env
