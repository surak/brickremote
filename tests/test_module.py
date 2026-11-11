import asyncio

import pytest

from brickremote import module as module_mod
from brickremote.module import JMCModule
from brickremote.protocol import NOTIFY_UUID, WRITE_UUID, motor_frame, stop_frame


class FakeClient:
    """Stands in for bleak's BleakClient."""

    instances = []

    def __init__(self, device, timeout=None):
        self.device, self.address = device, "2A:88:2E:AF:36:31"
        self.connected = False
        self.writes, self.notify = [], None
        self._backend = type("Backend", (), {"_device_path": "/org/bluez/hci0/dev_2A_88_2E_AF_36_31"})()
        FakeClient.instances.append(self)

    @property
    def is_connected(self):
        return self.connected

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def start_notify(self, uuid, callback):
        assert uuid == NOTIFY_UUID
        self.notify = callback

    async def write_gatt_char(self, uuid, data, response=False):
        assert uuid == WRITE_UUID
        self.writes.append((bytes(data), response))


@pytest.fixture
def client(monkeypatch):
    FakeClient.instances = []
    monkeypatch.setattr(module_mod, "BleakClient", FakeClient)

    async def fake_find(address=None, timeout=10.0):
        return "device"

    monkeypatch.setattr(module_mod, "find", fake_find)
    return FakeClient


def run(coro):
    return asyncio.run(coro)


def test_drive_writes_frames_with_response(client):
    async def go():
        async with JMCModule(resend_interval=None) as m:
            await m.drive(a=255)
            await m.drive(d=-120)
            return client.instances[0].writes
    writes = run(go())
    assert writes[0] == (motor_frame(a=255), True)  # the module ignores writes without response
    assert writes[1] == (motor_frame(a=255, d=-120), True)
    assert writes[-1] == (stop_frame(), True)       # leaving the context stops the motors


def test_inverted_and_linked_ports(client):
    async def go():
        async with JMCModule(resend_interval=None, inverted="d", links={"a": "c"}) as m:
            await m.drive(a=100, d=50)
            return m.speeds, client.instances[0].writes[0][0]
    speeds, frame = run(go())
    assert speeds["c"] == 100                        # C follows A
    assert frame == motor_frame(a=100, c=100, d=-50)  # D is sent reversed


def test_unknown_port_is_rejected(client):
    async def go():
        async with JMCModule(resend_interval=None) as m:
            await m.drive(z=1)
    with pytest.raises(ValueError):
        run(go())


def test_run_stops_the_ports_afterwards(client):
    async def go():
        async with JMCModule(resend_interval=None) as m:
            await m.run(0.01, a=255)
            return m.speeds["a"]
    assert run(go()) == 0


def test_state_is_resent(client):
    async def go():
        async with JMCModule(resend_interval=0.01) as m:
            await m.drive(b=60)
            await asyncio.sleep(0.06)
            return [w for w, _ in client.instances[0].writes]
    frames = run(go())
    assert frames.count(motor_frame(b=60)) >= 3


def test_status_notifications(client):
    seen = []

    async def go():
        async with JMCModule(resend_interval=None) as m:
            m.on_status = seen.append
            client.instances[0].notify(None, bytearray.fromhex("5a6b010001642b"))
            client.instances[0].notify(None, bytearray(b"junk"))
            return m.battery
    assert run(go()) == 100
    assert [s.battery for s in seen] == [100]


def test_connect_fails_when_no_module_is_found(client, monkeypatch):
    async def not_found(address=None, timeout=10.0):
        return None
    monkeypatch.setattr(module_mod, "find", not_found)
    with pytest.raises(ConnectionError):
        run(JMCModule().connect())


def test_keep_connected_reconnects_after_a_drop(client):
    events = []

    async def go():
        m = JMCModule(resend_interval=None)
        task = asyncio.create_task(m.keep_connected(
            on_connected=lambda: events.append("up"), on_lost=lambda: events.append("lost"), poll=0.01))
        await asyncio.sleep(0.05)
        client.instances[-1].connected = False  # e.g. the module switched itself off
        await asyncio.sleep(0.05)
        task.cancel()
        return m.is_connected
    assert run(go())
    assert events == ["lost", "up", "lost", "up"]
    assert len(client.instances) == 2


def test_close_hands_the_disconnect_to_busctl(client, monkeypatch):
    started = []
    monkeypatch.setattr(module_mod.shutil, "which", lambda name: "/usr/bin/busctl")
    monkeypatch.setattr(module_mod.subprocess, "Popen", lambda cmd, **kw: started.append(cmd))

    async def go():
        m = JMCModule(resend_interval=None)
        await m.connect()
        await m.drive(a=255)
        fake = client.instances[0]
        await m.close()
        return m.is_connected, fake
    connected, fake = run(go())
    assert not connected
    assert fake.writes[-1] == (stop_frame(), True)  # motors stopped before leaving
    assert fake.connected                           # ...and the link is left to busctl
    assert started == [["busctl", "call", "org.bluez", "/org/bluez/hci0/dev_2A_88_2E_AF_36_31",
                        "org.bluez.Device1", "Disconnect"]]


def test_close_disconnects_itself_without_busctl(client, monkeypatch):
    monkeypatch.setattr(module_mod.shutil, "which", lambda name: None)

    async def go():
        m = JMCModule(resend_interval=None)
        await m.connect()
        fake = client.instances[0]
        await m.close()
        return fake
    assert not run(go()).connected
