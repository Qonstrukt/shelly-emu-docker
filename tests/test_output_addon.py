"""Shelly Pro 3EM with the Pro Output Add-on relay (switch:100)."""

import struct

import pytest

from shelly_emu.config import ConfigError, load_config
from shelly_emu.device import DeviceSpec, EmSpec, ShellyDevice, SwitchChannelSpec
from shelly_emu.modbus_registers import (
    ADDON_COIL,
    ADDON_SWITCH_REG,
    EX_ILLEGAL_ADDRESS,
    EX_ILLEGAL_FUNCTION,
    EX_ILLEGAL_VALUE,
    build_register_map,
    handle_frame,
)
from shelly_emu.rpc import RpcDispatcher, RpcError
from shelly_emu.state import EmState

ENV = {"HA_URL": "http://ha:8123", "HA_TOKEN": "t"}


@pytest.fixture
def addon(clock):
    commands = []
    spec = DeviceSpec(
        model="pro3em", mac="112233445577", name="Grid", em=EmSpec(),
        switches=[SwitchChannelSpec(name="Boiler")], output_addon=True,
    )  # fmt: skip
    dev = ShellyDevice(spec, on_switch=lambda ch, on: commands.append((ch, on)), clock=clock, wallclock=clock.wallclock)
    dev.commands = commands
    return dev


@pytest.fixture
def rpc(addon):
    return RpcDispatcher(addon)


def test_spec_validation():
    with pytest.raises(ValueError, match="output_addon"):
        DeviceSpec(model="pro3em", mac="112233445577", switches=[SwitchChannelSpec()])
    with pytest.raises(ValueError, match="exactly one"):
        DeviceSpec(model="pro3em", mac="112233445577", output_addon=True)
    with pytest.raises(ValueError, match="only available"):
        DeviceSpec(model="pro1pm", mac="112233445577", switches=[SwitchChannelSpec()], output_addon=True)


def test_ids(addon, pro3em, pro4pm):
    assert addon.has_output_addon and not pro3em.has_output_addon and not pro4pm.has_output_addon
    assert addon.switch_component_id(0) == 100
    assert addon.switch_index_for_id(100) == 0
    assert addon.switch_index_for_id(0) == -1 and addon.switch_index_for_id(101) == -1
    assert pro4pm.switch_component_id(3) == 3 and pro4pm.switch_index_for_id(100) == -1


def test_switch_status_and_config(rpc, addon):
    addon.update_switch(0, output=True, temperature=38.0)
    assert rpc.call("Switch.GetStatus", {"id": 100}) == {
        "id": 100, "source": "init", "output": True, "temperature": {"tC": 38.0, "tF": 100.4},
    }  # fmt: skip
    assert rpc.call("Switch.GetConfig", {"id": "100"}) == {
        "id": 100, "name": "Boiler", "initial_state": "restore_last",
        "auto_on": False, "auto_on_delay": 60.0, "auto_off": False, "auto_off_delay": 60.0,
    }  # fmt: skip
    for bad in (0, 1, 101, -1):
        with pytest.raises(RpcError) as e:
            rpc.call("Switch.GetStatus", {"id": bad})
        assert e.value.code == -105 and e.value.message == "Invalid argument 'id'"
    with pytest.raises(RpcError) as e:
        rpc.call("Input.GetStatus", {"id": 100})
    assert e.value.code == 404


def test_switch_set_toggle_setconfig(rpc, addon):
    assert rpc.call("Switch.Set", {"id": 100, "on": True}) == {"was_on": False}
    assert addon.commands == [(0, True)]
    assert rpc.call("Switch.GetStatus", {"id": 100})["output"] is True
    assert rpc.call("Switch.Toggle", {"id": 100}) == {"was_on": True}
    assert addon.commands == [(0, True), (0, False)]
    assert rpc.call("Switch.SetConfig", {"id": 100, "config": {"name": "x"}}) == {"restart_required": False}


def test_components_and_sys_config(rpc, rpc3em):
    st = rpc.call("Shelly.GetStatus")
    assert list(st) == [
        "ble", "bthome", "cloud", "em:0", "emdata:0", "eth", "modbus", "mqtt", "switch:100", "sys", "temperature:0", "wifi", "ws",
    ]  # fmt: skip
    assert st["switch:100"]["id"] == 100 and "apower" not in st["switch:100"]
    assert rpc.call("Shelly.GetConfig")["sys"]["device"]["addon_type"] == "prooutput"
    assert rpc.call("Sys.GetConfig")["device"]["addon_type"] == "prooutput"
    assert rpc3em.call("Sys.GetConfig")["device"]["addon_type"] is None
    comps = rpc.call("Shelly.GetComponents")
    assert comps["total"] == 13
    keys = [c["key"] for c in comps["components"]]
    assert keys.index("mqtt") < keys.index("switch:100") < keys.index("sys")
    assert "switch:100" not in rpc3em.call("Shelly.GetStatus")


def test_pro_output_addon_methods(rpc, rpc3em):
    assert rpc.call("ProOutputAddon.GetPeripherals") == {"digital_out": {"switch:100": {}}}
    for method in ("ProOutputAddon.AddPeripheral", "ProOutputAddon.RemovePeripheral"):
        with pytest.raises(RpcError) as e:
            rpc.call(method, {"type": "digital_out", "attrs": {"cid": 100}})
        assert e.value.code == -108 and e.value.message == "Peripheral configuration is fixed on this device"
    for method in ("ProOutputAddon.GetPeripherals", "ProOutputAddon.AddPeripheral", "Switch.GetStatus"):
        with pytest.raises(RpcError) as e:
            rpc3em.call(method, {"id": 100})
        assert e.value.code == 404


def test_list_methods(rpc, rpc3em):
    m = rpc.call("Shelly.ListMethods")["methods"]
    for name in ("Switch.Set", "Switch.Toggle", "Switch.GetStatus", "ProOutputAddon.GetPeripherals", "ProOutputAddon.RemovePeripheral"):
        assert name in m
    assert "Input.GetStatus" not in m and "Switch.ResetCounters" not in m
    assert "Switch.Set" not in rpc3em.call("Shelly.ListMethods")["methods"]


def _adu(fc, *words, tid=1, unit=7):
    pdu = struct.pack(">B", fc) + b"".join(struct.pack(">H", w) for w in words)
    return struct.pack(">HHHB", tid, 0, len(pdu) + 1, unit) + pdu


def test_register_3000():
    regs = build_register_map(EmState(), "112233445577", "SPEM-003CEBEU", 1700000000.0, addon_output=True)
    assert regs[ADDON_SWITCH_REG] == 1
    regs = build_register_map(EmState(), "112233445577", "SPEM-003CEBEU", 1700000000.0, addon_output=False)
    assert regs[ADDON_SWITCH_REG] == 0
    regs = build_register_map(EmState(), "112233445577", "SPEM-003CEBEU", 1700000000.0)
    assert ADDON_SWITCH_REG not in regs


def test_read_coils():
    on = handle_frame(_adu(1, 100, 1), {}, addon_output=True)
    assert on[6:] == bytes([7, 1, 1, 0x01])
    off = handle_frame(_adu(1, 100, 1), {}, addon_output=False)
    assert off[6:] == bytes([7, 1, 1, 0x00])
    # range covering 100 at a bit offset: coils 96..111 -> bit 4 of byte 0
    wide = handle_frame(_adu(1, 96, 16), {}, addon_output=True)
    assert wide[6:] == bytes([7, 1, 2, 0x10, 0x00])
    for addr, count in ((0, 100), (101, 5), (100, 0), (0, 2001)):
        resp = handle_frame(_adu(1, addr, count), {}, addon_output=True)
        assert resp[7:9] == bytes([0x81, EX_ILLEGAL_ADDRESS]), (addr, count)
    # without the add-on the coil functions stay unsupported
    for frame in (_adu(1, 100, 1), _adu(5, 100, 0xFF00), _adu(15, 100, 1) + b"\x01\x01"):
        resp = handle_frame(frame, {})
        assert resp[7] & 0x80 and resp[8] == EX_ILLEGAL_FUNCTION


def test_write_single_coil():
    writes = []
    req = _adu(5, ADDON_COIL, 0xFF00)
    resp = handle_frame(req, {}, addon_output=False, write_coil=writes.append)
    assert resp[7:] == req[7:] and resp[:2] == req[:2] and writes == [True]
    resp = handle_frame(_adu(5, ADDON_COIL, 0x0000), {}, addon_output=True, write_coil=writes.append)
    assert writes == [True, False] and resp[7] == 5
    resp = handle_frame(_adu(5, ADDON_COIL, 0x1234), {}, addon_output=True, write_coil=writes.append)
    assert resp[7:9] == bytes([0x85, EX_ILLEGAL_VALUE]) and writes == [True, False]
    resp = handle_frame(_adu(5, 0, 0xFF00), {}, addon_output=True, write_coil=writes.append)
    assert resp[7:9] == bytes([0x85, EX_ILLEGAL_ADDRESS])


def test_write_multiple_coils():
    writes = []
    req = _adu(15, ADDON_COIL, 1) + b"\x01\x01"
    resp = handle_frame(req, {}, addon_output=False, write_coil=writes.append)
    assert resp[7:] == req[7:12] and writes == [True]
    assert struct.unpack(">H", resp[4:6])[0] == 6
    resp = handle_frame(_adu(15, ADDON_COIL, 1) + b"\x01\x00", {}, addon_output=True, write_coil=writes.append)
    assert writes == [True, False] and resp[7] == 15
    resp = handle_frame(_adu(15, ADDON_COIL, 2) + b"\x01\x03", {}, addon_output=True, write_coil=writes.append)
    assert resp[7:9] == bytes([0x8F, EX_ILLEGAL_ADDRESS])
    resp = handle_frame(_adu(15, 0, 1) + b"\x01\x01", {}, addon_output=True, write_coil=writes.append)
    assert resp[7:9] == bytes([0x8F, EX_ILLEGAL_ADDRESS])
    assert writes == [True, False]


def test_config(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(
        """
devices:
  - model: pro3em
    mac: 112233445577
    output_addon:
      switch: switch.boiler
      name: Boiler
    em:
      phases:
        - power: sensor.l1
"""
    )
    cfg = load_config(p, ENV)
    d = cfg.devices[0]
    assert d.spec.output_addon and d.spec.switches[0].name == "Boiler"
    assert d.channels[0].switch == "switch.boiler"
    assert sorted(d.entity_ids) == ["sensor.l1", "switch.boiler"]
    dev = ShellyDevice(d.spec)
    assert dev.has_output_addon and dev.switch_component_id(0) == 100

    for body, match in (
        ("devices:\n  - {model: pro1pm, mac: 112233445577, channels: [{switch: switch.a}], output_addon: {switch: switch.b}}", "only available"),
        ("devices:\n  - {model: pro3em, mac: 112233445577, em: {phases: [{power: sensor.l1}]}, output_addon: {name: x}}", "'switch'"),
        ("devices:\n  - {model: pro3em, mac: 112233445577, em: {phases: [{power: sensor.l1}]}, output_addon: {switch: switch.b, power: sensor.p}}", "unknown keys"),
        ("devices:\n  - {model: pro3em, mac: 112233445577, em: {phases: [{power: sensor.l1}]}, channels: [{switch: switch.b}]}", "output_addon"),
    ):
        p.write_text(body)
        with pytest.raises(ConfigError, match=match):
            load_config(p, ENV)
