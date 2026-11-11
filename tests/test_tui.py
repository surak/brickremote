import curses
import json

import pytest

from brickremote import tui
from brickremote.car import Car


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(tui.time, "monotonic", c)
    return c


@pytest.fixture
def app(fake_module):
    return tui.App(None, fake_module, Car(fake_module))  # no screen needed for the controls


def press(app, *keys):
    for k in keys:
        app.key(ord(k) if isinstance(k, str) else k)


async def tick(app):
    await app.apply_held()


def test_arrows_drive_and_steer(app, fake_module, clock):
    import asyncio
    press(app, curses.KEY_UP, curses.KEY_LEFT)
    asyncio.run(tick(app))
    assert fake_module.speeds["a"] == 255 and fake_module.speeds["d"] == 255


def test_released_keys_stop_and_steering_springs_back(app, fake_module, clock):
    import asyncio
    press(app, curses.KEY_UP, curses.KEY_LEFT)
    clock.now += 0.05
    press(app, curses.KEY_UP, curses.KEY_LEFT)  # auto-repeat
    clock.now += tui.REPEAT_HOLD + 0.01        # then let go
    asyncio.run(tick(app))
    assert fake_module.speeds["a"] == 0 and fake_module.speeds["d"] == 0


def test_first_press_waits_for_auto_repeat(app, fake_module, clock):
    import asyncio
    press(app, curses.KEY_UP)
    clock.now += tui.REPEAT_HOLD + 0.01  # repeats have not started yet: still held
    asyncio.run(tick(app))
    assert fake_module.speeds["a"] == 255


def test_hold_latches_driving_but_not_steering(app, fake_module, clock):
    import asyncio
    press(app, "h", curses.KEY_UP, curses.KEY_RIGHT)
    clock.now += 5
    asyncio.run(tick(app))
    assert fake_module.speeds["a"] == 255 and fake_module.speeds["d"] == 0


def test_gears_set_the_speed(app, fake_module, clock):
    import asyncio
    press(app, "1", curses.KEY_DOWN)
    asyncio.run(tick(app))
    assert fake_module.speeds["a"] == -60


def test_ports_tab_keys(app, fake_module, clock):
    import asyncio
    press(app, 9, "w", "j")  # Tab to Ports, A forward, D left (reverse)
    asyncio.run(tick(app))
    assert fake_module.speeds["a"] == 255 and fake_module.speeds["d"] == -255


def test_pair_reverse_and_quit(app, fake_module, tmp_config):
    press(app, "p")
    assert fake_module.links == {"a": "b"}
    press(app, "p")
    assert fake_module.links == {"a": "c"}
    press(app, "r", "d")
    assert fake_module.inverted == {"d"}
    assert json.loads(tmp_config.read_text())["inverted"] == ["d"]
    assert not app.quit
    press(app, "q")
    assert app.quit


def test_keys_ignored_while_choosing_a_port_to_reverse(app, fake_module):
    press(app, "r", "q")  # q is not a port, so nothing is reversed and we don't quit
    assert fake_module.inverted == set() and not app.quit
