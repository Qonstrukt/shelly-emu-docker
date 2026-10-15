import pytest

from shelly_emu.config import ConfigError, load_config

ENV = {"HA_URL": "http://ha:8123", "HA_TOKEN": "t"}


def write(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return p


def test_load_switch_device(tmp_path):
    p = write(
        tmp_path,
        """
devices:
  - model: pro2pm
    name: Plugs
    mac: "AA:BB:CC:DD:EE:FF"
    channels:
      - switch: switch.boiler
        name: Boiler
        power: sensor.boiler_power
        energy: sensor.boiler_energy
        energy_unit: Wh
      - switch: light.dryer
        input: binary_sensor.door
""",
    )
    cfg = load_config(p, ENV)
    assert cfg.ha_url == "http://ha:8123" and cfg.ha_token == "t"
    d = cfg.devices[0]
    assert d.spec.mac == "AABBCCDDEEFF" and d.spec.model == "pro2pm"
    assert d.modbus is False and d.mdns is True and d.bind == "0.0.0.0"
    assert d.spec.switches[0].has_energy and d.spec.switches[0].energy_scale == 1.0
    assert not d.spec.switches[1].has_energy
    assert sorted(d.entity_ids) == ["binary_sensor.door", "light.dryer", "sensor.boiler_energy", "sensor.boiler_power", "switch.boiler"]


def test_load_meter_device(tmp_path):
    p = write(
        tmp_path,
        """
homeassistant:
  url: http://other:8123
  token: yaml-token
devices:
  - model: pro3em
    mac: 112233445566
    em:
      energy_unit: kWh
      phases:
        - power: sensor.l1
          energy: sensor.import
        - power: sensor.l2
""",
    )
    cfg = load_config(p, {})
    assert cfg.ha_url == "http://other:8123" and cfg.ha_token == "yaml-token"
    d = cfg.devices[0]
    assert d.modbus is True
    assert d.spec.em.phases[0].has_energy and not d.spec.em.phases[1].has_energy
    assert len(d.spec.em.phases) == 3


def test_token_file(tmp_path):
    tf = tmp_path / "token"
    tf.write_text("secret\n")
    p = write(tmp_path, "devices:\n  - {model: pro1pm, mac: AABBCCDDEEFF, channels: [{switch: switch.a}]}\n")
    cfg = load_config(p, {"HA_URL": "http://ha", "HA_TOKEN_FILE": str(tf)})
    assert cfg.ha_token == "secret"


@pytest.mark.parametrize(
    "body, match",
    [
        ("devices: []", "non-empty"),
        ("devices:\n  - {model: pro9, mac: AABBCCDDEEFF}", "model"),
        ("devices:\n  - {model: pro1pm, channels: [{switch: switch.a}]}", "mac"),
        ("devices:\n  - {model: pro1pm, mac: AABBCCDDEEFF, channels: []}", "at least one"),
        ("devices:\n  - {model: pro1pm, mac: AABBCCDDEEFF, channels: [{switch: switch.a}, {switch: switch.b}]}", "at most 1"),
        ("devices:\n  - {model: pro3em, mac: AABBCCDDEEFF}", "em"),
        ("devices:\n  - {model: pro1pm, mac: AABBCCDDEEFF, channels: [{switch: switch.a, bogus: 1}]}", "unknown keys"),
        ("devices:\n  - {model: pro1pm, mac: AABBCCDDEEFF, channels: [{switch: switch.a, energy_unit: J}]}", "energy_unit"),
        (
            "devices:\n  - {model: pro1pm, mac: AABBCCDDEEFF, channels: [{switch: switch.a}]}\n"
            "  - {model: pro1pm, mac: AABBCCDDEE00, channels: [{switch: switch.b}]}",
            "own IP",
        ),
        (
            "devices:\n  - {model: pro1pm, mac: AABBCCDDEEFF, bind: 10.0.0.1, channels: [{switch: switch.a}]}\n"
            "  - {model: pro1pm, mac: AABBCCDDEEFF, bind: 10.0.0.2, channels: [{switch: switch.b}]}",
            "unique 'mac'",
        ),
    ],
)
def test_errors(tmp_path, body, match):
    p = write(tmp_path, body)
    with pytest.raises(ConfigError, match=match):
        load_config(p, ENV)


def test_two_devices_with_own_ips(tmp_path):
    p = write(
        tmp_path,
        "devices:\n  - {model: pro1pm, mac: AABBCCDDEEFF, bind: 10.0.0.1, channels: [{switch: switch.a}]}\n"
        "  - {model: pro1pm, mac: AABBCCDDEE00, bind: 10.0.0.2, channels: [{switch: switch.b}]}",
    )
    cfg = load_config(p, ENV)
    assert [d.ip for d in cfg.devices] == [None, None]


def test_missing_ha(tmp_path):
    p = write(tmp_path, "devices:\n  - {model: pro1pm, mac: AABBCCDDEEFF, channels: [{switch: switch.a}]}")
    with pytest.raises(ConfigError, match="HA_URL"):
        load_config(p, {})
    with pytest.raises(ConfigError, match="HA_TOKEN"):
        load_config(p, {"HA_URL": "http://ha"})
