import asyncio

import pytest

from brickremote import config
from brickremote.protocol import PORTS, Status


class FakeModule:
    """Stands in for JMCModule: records what the apps ask the motors to do."""

    def __init__(self):
        self._speeds = dict.fromkeys(PORTS, 0)
        self.inverted, self.links = set(), {}
        self.status = Status(80)
        self.is_connected = True
        self.calls = []

    @property
    def battery(self):
        return self.status.battery

    @property
    def speeds(self):
        return dict(self._speeds)

    async def drive(self, **speeds):
        self.calls.append(("drive", speeds))
        for port, speed in speeds.items():
            self._speeds[port] = speed
            if port in self.links:
                self._speeds[self.links[port]] = speed

    async def run(self, seconds, **speeds):
        self.calls.append(("run", seconds, speeds))
        await self.drive(**speeds)
        await asyncio.sleep(0)
        await self.drive(**dict.fromkeys(speeds, 0))

    async def stop(self):
        self.calls.append(("stop",))
        self._speeds = dict.fromkeys(PORTS, 0)

    async def close(self, fast=True):
        self.calls.append(("close",))


@pytest.fixture
def fake_module():
    return FakeModule()


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    """Never touch the real ~/.config/brickremote.json from tests."""
    path = tmp_path / "brickremote.json"
    monkeypatch.setattr(config, "CONFIG", path)
    return path
