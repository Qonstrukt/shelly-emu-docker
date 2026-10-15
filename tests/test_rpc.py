import json

import pytest

from shelly_emu.device import DeviceSpec, ShellyDevice, SwitchChannelSpec
from shelly_emu.rpc import RpcDispatcher, RpcError, param_id


def test_device_info(rpc4):
    info = rpc4.call("Shelly.GetDeviceInfo")
    assert info == {
        "name": "Plugs",
        "id": "shellypro4pm-aabbccddeeff",
        "mac": "AABBCCDDEEFF",
        "slot": 0,
        "model": "SPSW-004PE16EU",
        "gen": 2,
        "fw_id": "20260710-101218/2.0.0-g87fbfa4",
        "ver": "2.0.0",
        "app": "Pro4PM",
        "auth_en": False,
        "auth_domain": None,
        "provision": "complete",
        "enhanced_security": False,
    }
    json.dumps(info)


def test_device_info_profiles(rpc3em):
    assert rpc3em.call("Shelly.GetDeviceInfo")["profile"] == "triphase"
    dev = ShellyDevice(DeviceSpec(model="pro2pm", mac="AABBCCDDEE02", switches=[SwitchChannelSpec()]))
    assert RpcDispatcher(dev).call("Shelly.GetDeviceInfo")["profile"] == "switch"
    assert RpcDispatcher(dev).call("Sys.GetConfig")["device"]["profile"] == "switch"
    assert "profiles" in RpcDispatcher(dev).call("Shelly.ListProfiles")


def test_method_names_are_case_insensitive(rpc4):
    assert rpc4.call("WiFi.GetStatus")["status"] == "got ip"
    assert rpc4.call("shelly.getdeviceinfo")["gen"] == 2


def test_unknown_method(rpc4):
    with pytest.raises(RpcError) as e:
        rpc4.call("Nope.Nope")
    assert e.value.code == 404
    assert e.value.to_json() == {"code": 404, "message": "Method not found"}


def test_switch_status_shape(rpc4, pro4pm):
    pro4pm.update_switch(0, output=True, power=123.456, voltage=231.2, energy=12.3456, temperature=40.0)
    st = rpc4.call("Switch.GetStatus", {"id": 0})
    assert st["id"] == 0 and st["output"] is True
    assert st["apower"] == 123.5 and st["voltage"] == 231.2
    assert st["current"] == round(123.456 / 231.2, 3)
    assert st["aenergy"]["total"] == 12345.6
    assert st["aenergy"]["by_minute"] == [0.0, 0.0, 0.0]
    assert st["aenergy"]["minute_ts"] % 60 == 0 and st["aenergy"]["minute_ts"] > 1600000000
    assert st["temperature"] == {"tC": 40.0, "tF": 104.0}
    assert set(st) == {
        "id", "source", "tag", "output", "apower", "voltage", "freq", "current", "pf",
        "aenergy", "ret_aenergy", "counts", "temperature",
    }  # fmt: skip
    # string ids (GET query) work too
    assert rpc4.call("Switch.GetStatus", {"id": "3"})["id"] == 3


def test_switch_config(rpc4):
    cfg = rpc4.call("Switch.GetConfig", {"id": 0})
    assert cfg["name"] == "Boiler" and cfg["in_mode"] == "detached"
    assert rpc4.call("Switch.GetConfig", {"id": 2})["name"] is None


def test_switch_set_and_toggle(rpc4, pro4pm):
    assert rpc4.call("Switch.Set", {"id": 1, "on": True}) == {"was_on": False}
    assert pro4pm.commands == [(1, True)]
    assert rpc4.call("Switch.GetStatus", {"id": 1})["output"] is True
    assert rpc4.call("Switch.Toggle", {"id": 1}) == {"was_on": True}
    assert pro4pm.commands == [(1, True), (1, False)]
    assert rpc4.call("Switch.Set", {"id": 1, "on": 1}) == {"was_on": False}
    with pytest.raises(RpcError) as e:
        rpc4.call("Switch.Set", {"id": 1, "on": "yes"})
    assert e.value.code == -105
    with pytest.raises(RpcError) as e:
        rpc4.call("Switch.Set", {"id": 4, "on": True})
    assert e.value.message == "Invalid argument 'id'"
    with pytest.raises(RpcError):
        rpc4.call("Switch.Set", {"on": True})


def test_switch_methods_rejected_on_meter(rpc3em):
    with pytest.raises(RpcError) as e:
        rpc3em.call("Switch.GetStatus", {"id": 0})
    assert e.value.code == 404


def test_components_and_status(rpc4):
    st = rpc4.call("Shelly.GetStatus")
    assert list(st) == [
        "ble", "bthome", "cloud", "eth", "input:0", "input:1", "input:2", "input:3", "knx", "mqtt",
        "switch:0", "switch:1", "switch:2", "switch:3", "sys", "wifi", "ws",
    ]  # fmt: skip
    cfg = rpc4.call("Shelly.GetConfig")
    assert list(cfg) == list(st)
    assert cfg["sys"]["device"]["name"] == "Plugs"
    assert "profile" not in cfg["sys"]["device"]
    comps = rpc4.call("Shelly.GetComponents")
    assert comps["total"] == 17 and comps["offset"] == 0
    assert comps["components"][0] == {"key": "ble", "status": {"addr": "aa:bb:cc:dd:ee:01,p", "flags": []}, "config": {"rpc": {"enable": False}}}
    partial = rpc4.call("Shelly.GetComponents", {"offset": 15, "include": ["status"]})
    assert [c["key"] for c in partial["components"]] == ["wifi", "ws"]
    assert "config" not in partial["components"][0]
    assert rpc4.call("Shelly.GetComponents", {"dynamic_only": True})["components"] == []


def test_meter_status(rpc3em, pro3em):
    pro3em.update_em(0, power=100.0, voltage=230.0)
    pro3em.update_em(1, power=-50.0, voltage=231.0)
    st = rpc3em.call("Shelly.GetStatus")
    assert list(st) == ["ble", "bthome", "cloud", "em:0", "emdata:0", "eth", "modbus", "mqtt", "sys", "temperature:0", "wifi", "ws"]
    em = st["em:0"]
    assert em["a_act_power"] == 100.0 and em["b_act_power"] == -50.0 and em["c_act_power"] == 0.0
    assert em["total_act_power"] == 50.0
    assert em["n_current"] is None
    assert em["user_calibrated_phase"] == []
    assert rpc3em.call("EM.GetStatus", {"id": 0})["b_voltage"] == 231.0
    assert rpc3em.call("EMData.GetStatus")["total_act"] == 0.0
    assert rpc3em.call("Temperature.GetStatus") == {"id": 0, "tC": 45.0, "tF": 113.0}
    assert rpc3em.call("Modbus.GetConfig") == {"enable": True}
    assert rpc3em.call("EM.GetCTTypes") == {"supported": ["120A", "400A"]}
    with pytest.raises(RpcError):
        rpc3em.call("EM.GetStatus", {"id": 1})
    assert "bthc_rev" not in st["sys"]
    assert rpc3em.call("Sys.GetConfig")["device"]["profile"] == "triphase"


def test_list_methods(rpc4, rpc3em):
    m4 = rpc4.call("Shelly.ListMethods")["methods"]
    assert "Switch.Set" in m4 and "Eth.GetStatus" in m4 and "EM.GetStatus" not in m4 and "Shelly.ListProfiles" not in m4
    m3 = rpc3em.call("Shelly.ListMethods")["methods"]
    assert "EM.GetStatus" in m3 and "Switch.Set" not in m3


def test_sys_and_wifi(rpc4, pro4pm):
    pro4pm.ip = "192.168.1.50"
    sysst = rpc4.call("Sys.GetStatus")
    assert sysst["mac"] == "AABBCCDDEEFF" and sysst["uptime"] == 0 and sysst["unixtime"] == 1700000000
    assert sysst["cfg_rev"] == 1
    wifi = rpc4.call("Wifi.GetStatus")
    assert wifi["sta_ip"] == "192.168.1.50" and wifi["status"] == "got ip" and wifi["ssid"] == "HomeAssistant"
    assert rpc4.call("Wifi.GetConfig")["ap"]["ssid"] == "ShellyPro4PM-AABBCCDDEEFF"
    assert rpc4.call("Sys.SetConfig", {"config": {"device": {"name": "New"}}}) == {"restart_required": False}
    assert rpc4.call("Shelly.GetDeviceInfo")["name"] == "New"
    assert rpc4.call("Sys.GetStatus")["cfg_rev"] == 2


def test_void_and_misc_methods(rpc4):
    assert rpc4.call("Shelly.Reboot") is None
    assert rpc4.call("RPC.Ping") == {}
    assert rpc4.call("Shelly.CheckForUpdate") == {}
    assert rpc4.call("Schedule.List") == {"jobs": [], "rev": 0}
    assert rpc4.call("KVS.GetMany") == {"items": {}, "offset": 0, "total": 0, "rev": 0}
    assert rpc4.call("Wifi.SetConfig", {"config": {}}) == {"restart_required": False}
    assert rpc4.call("Cloud.GetConfig")["enable"] is False
    assert rpc4.call("Mqtt.GetConfig")["client_id"] == "shellypro4pm-aabbccddeeff"
    assert rpc4.call("Input.GetStatus", {"id": 0}) == {"id": 0, "state": False}


def test_plus_models_have_no_eth(rpc4):
    dev = ShellyDevice(DeviceSpec(model="plus1pm", mac="AABBCCDDEE03", switches=[SwitchChannelSpec()]))
    rpc = RpcDispatcher(dev)
    with pytest.raises(RpcError):
        rpc.call("Eth.GetStatus")
    assert "eth" not in rpc.call("Shelly.GetStatus")
    assert "knx" not in rpc.call("Shelly.GetStatus")
    plug = RpcDispatcher(ShellyDevice(DeviceSpec(model="plusplugs", mac="AABBCCDDEE04", switches=[SwitchChannelSpec()])))
    assert "input:0" not in plug.call("Shelly.GetStatus")
    with pytest.raises(RpcError):
        plug.call("Input.GetStatus", {"id": 0})


def test_envelope(rpc4):
    env = rpc4.envelope({"id": 7, "src": "validate", "method": "Shelly.GetDeviceInfo"})
    assert env["id"] == 7 and env["src"] == "shellypro4pm-aabbccddeeff" and env["dst"] == "validate"
    assert env["result"]["app"] == "Pro4PM"
    env = rpc4.envelope({"id": 8, "method": "Nope"})
    assert env["dst"] == "user_1" and env["error"] == {"code": 404, "message": "Method not found"}
    env = rpc4.envelope({"id": 9})
    assert env["error"]["code"] == -32600
    env = rpc4.envelope({"id": 10, "src": "x", "method": "Switch.Set", "params": {"id": 0, "on": True}})
    assert env["result"] == {"was_on": False}


def test_param_id():
    assert param_id({"id": 2}) == 2
    assert param_id({"id": "2"}) == 2
    assert param_id({"id": 2.0}) == 2
    assert param_id({"id": True}) == -1
    assert param_id({"id": "abc"}) == -1
    assert param_id({}) == -1
    assert param_id(None) == -1
