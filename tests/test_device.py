import pytest

from shelly_emu.device import OPTIMISTIC_SECONDS, DeviceSpec, EmSpec, ShellyDevice, SwitchChannelSpec


def test_identity(pro4pm, pro3em):
    assert pro4pm.mac == "AABBCCDDEEFF"
    assert pro4pm.device_id == "shellypro4pm-aabbccddeeff"
    assert pro4pm.hostname == "ShellyPro4PM-AABBCCDDEEFF"
    assert pro4pm.model_code == "SPSW-004PE16EU"
    assert pro3em.device_id == "shellypro3em-112233445566"
    assert pro3em.hostname == "ShellyPro3EM-112233445566"


def test_spec_validation():
    with pytest.raises(ValueError):
        DeviceSpec(model="nope", mac="AABBCCDDEEFF", switches=[SwitchChannelSpec()])
    with pytest.raises(ValueError):
        DeviceSpec(model="pro1pm", mac="AABBCCDDEEFF", switches=[SwitchChannelSpec(), SwitchChannelSpec()])
    with pytest.raises(ValueError):
        DeviceSpec(model="pro1pm", mac="AABBCCDDEEFF", switches=[])
    with pytest.raises(ValueError):
        DeviceSpec(model="pro1pm", mac="xyz", switches=[SwitchChannelSpec()])
    with pytest.raises(ValueError):
        DeviceSpec(model="pro3em", mac="AABBCCDDEEFF", switches=[SwitchChannelSpec()])
    with pytest.raises(ValueError):
        DeviceSpec(model="pro1pm", mac="AABBCCDDEEFF", switches=[SwitchChannelSpec()], em=EmSpec())
    spec = DeviceSpec(model="pro3em", mac="AABBCCDDEEFF")
    assert spec.em is not None


def test_derived_values(pro4pm):
    pro4pm.update_switch(0, output=True, power=230.0, voltage=230.0, energy=1.5)
    s = pro4pm.snapshot().sw[0]
    assert s.output is True
    assert s.apower == 230.0
    assert s.current == pytest.approx(1.0)
    assert s.pf == 1.0
    assert s.aenergy == 1500.0  # kWh -> Wh
    pro4pm.update_switch(0, power=0.0, current=None)
    s = pro4pm.snapshot().sw[0]
    assert s.pf == 0.0 and s.current == 0.0


def test_unavailable_sensor_falls_back(pro4pm):
    pro4pm.update_switch(1, power=100.0)
    pro4pm.update_switch(1, power=None)
    assert pro4pm.snapshot().sw[1].apower == 0.0


def test_energy_integration_without_energy_entity(pro4pm, clock):
    pro4pm.update_switch(1, output=True, power=1000.0)
    clock.advance(1800)  # half an hour at 1 kW
    s = pro4pm.snapshot().sw[1]
    assert s.aenergy == pytest.approx(500.0)
    pro4pm.update_switch(1, power=-500.0)  # export is not integrated into aenergy
    clock.advance(3600)
    assert pro4pm.snapshot().sw[1].aenergy == pytest.approx(500.0)
    # channel 0 has an energy entity: never integrated
    pro4pm.update_switch(0, power=1000.0)
    clock.advance(3600)
    assert pro4pm.snapshot().sw[0].aenergy == 0.0


def test_optimistic_state(pro4pm, clock):
    pro4pm.update_switch(0, output=False)
    assert pro4pm.request_switch(0, True) is False
    assert pro4pm.commands == [(0, True)]
    assert pro4pm.snapshot().sw[0].output is True  # optimistic, HA has not confirmed yet
    clock.advance(OPTIMISTIC_SECONDS + 1)
    assert pro4pm.snapshot().sw[0].output is False  # expired, back to HA state
    assert pro4pm.request_switch(0, True) is False
    pro4pm.update_switch(0, output=True)  # HA confirms
    clock.advance(OPTIMISTIC_SECONDS + 1)
    assert pro4pm.snapshot().sw[0].output is True


def test_em_defaults_and_derivation(pro3em, clock):
    pro3em.update_em(0, power=-1000.0, energy=10.0, energy_returned=2.0)
    pro3em.update_em(1, power=460.0, power_factor=0.5)
    snap = pro3em.snapshot()
    a, b, c = snap.em.phase
    assert a.voltage == 230.0 and a.freq == 50.0 and a.pf == 1.0
    assert a.aprt_power == 1000.0 and a.current == pytest.approx(1000 / 230)
    assert a.energy == 10000.0 and a.ret_energy == 2000.0
    assert b.aprt_power == pytest.approx(920.0)
    assert c.act_power == 0.0
    # phases b/c integrate; phase a has entities
    clock.advance(3600)
    snap = pro3em.snapshot()
    assert snap.em.phase[1].energy == pytest.approx(460.0)
    assert snap.em.phase[0].ret_energy == 2000.0
    # exported power on an integrating phase goes to ret_energy
    pro3em.update_em(2, power=-100.0)
    clock.advance(3600)
    assert pro3em.snapshot().em.phase[2].ret_energy == pytest.approx(100.0)


def test_switch_spec_count():
    dev = ShellyDevice(DeviceSpec(model="plusplugs", mac="AABBCCDDEE01", switches=[SwitchChannelSpec()]))
    assert dev.switch_count == 1
    assert dev.model.has_inputs is False
