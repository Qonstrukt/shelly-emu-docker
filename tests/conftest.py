import pytest

from shelly_emu.device import DeviceSpec, EmPhaseSpec, EmSpec, ShellyDevice, SwitchChannelSpec
from shelly_emu.rpc import RpcDispatcher


class FakeClock:
    def __init__(self, start: float = 1000.0, wall: float = 1700000000.0) -> None:
        self.t = start
        self.wall = wall

    def __call__(self) -> float:
        return self.t

    def wallclock(self) -> float:
        return self.wall + (self.t - 1000.0)

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def pro4pm(clock):
    commands = []
    spec = DeviceSpec(
        model="pro4pm",
        mac="aa:bb:cc:dd:ee:ff",
        name="Plugs",
        switches=[
            SwitchChannelSpec(name="Boiler", has_energy=True, energy_scale=1000.0),
            SwitchChannelSpec(name="Dryer"),
            SwitchChannelSpec(),
            SwitchChannelSpec(name="Pool", has_energy=True, has_energy_returned=True, energy_scale=1.0),
        ],
    )
    dev = ShellyDevice(spec, on_switch=lambda ch, on: commands.append((ch, on)), clock=clock, wallclock=clock.wallclock)
    dev.commands = commands
    return dev


@pytest.fixture
def pro3em(clock):
    spec = DeviceSpec(
        model="pro3em",
        mac="112233445566",
        name="Grid",
        em=EmSpec(phases=[EmPhaseSpec(True, True), EmPhaseSpec(), EmPhaseSpec()], energy_scale=1000.0),
    )
    return ShellyDevice(spec, clock=clock, wallclock=clock.wallclock)


@pytest.fixture
def rpc4(pro4pm):
    return RpcDispatcher(pro4pm)


@pytest.fixture
def rpc3em(pro3em):
    return RpcDispatcher(pro3em)
