import asyncio
import json

import pytest

pytest.importorskip("aiohttp")
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from brickremote import web  # noqa: E402
from brickremote.car import Car  # noqa: E402


async def receive_state(ws):
    while True:
        msg = json.loads((await ws.receive()).data)
        if msg["type"] == "state":
            return msg


def with_server(fake_module, test):
    """Run `test(client, ws, remote)` against a real server backed by the fake module.

    The state message the server sends on connecting is read first and kept in `remote.initial`.
    """
    async def go():
        remote = web.Remote(fake_module, Car(fake_module))
        async with TestClient(TestServer(web.make_app(remote))) as client:
            ws = await client.ws_connect("/ws")
            remote.initial = await receive_state(ws)
            try:
                return await test(client, ws, remote)
            finally:
                await ws.close()
                remote.cancel()
    return asyncio.run(go())


def test_page_is_served(fake_module):
    async def test(client, ws, remote):
        resp = await client.get("/")
        return resp.status, await resp.text()
    status, html = with_server(fake_module, test)
    assert status == 200 and "BRICK REMOTE" in html


def test_first_message_is_the_state(fake_module):
    async def test(client, ws, remote):
        return remote.initial
    state = with_server(fake_module, test)
    assert state["connected"] and state["battery"] == 80
    assert state["settings"]["drive_port"] == "a" and state["settings"]["steer_port"] == "d"


def test_drive_and_stop(fake_module):
    async def test(client, ws, remote):
        await ws.send_json({"type": "drive", "speeds": {"a": 300, "d": -255, "x": 5}})
        await receive_state(ws)
        await asyncio.sleep(0.05)
        driving = fake_module.speeds
        await ws.send_json({"type": "stop"})
        await asyncio.sleep(0.05)
        return driving, fake_module.speeds
    driving, stopped = with_server(fake_module, test)
    assert driving["a"] == 255 and driving["d"] == -255  # clamped, unknown port ignored
    assert not any(stopped.values())


def test_motors_stop_when_the_browser_goes_quiet(fake_module, monkeypatch):
    monkeypatch.setattr(web, "DEADMAN", 0.1)

    async def test(client, ws, remote):
        watchdog = asyncio.create_task(remote.deadman())
        await ws.send_json({"type": "drive", "speeds": {"a": 120}})
        await asyncio.sleep(0.05)
        moving = fake_module.speeds["a"]
        await asyncio.sleep(0.4)  # no more drive messages
        watchdog.cancel()
        return moving, fake_module.speeds["a"]
    assert with_server(fake_module, test) == (120, 0)


def test_settings_apply_and_persist(fake_module, tmp_config):
    async def test(client, ws, remote):
        await ws.send_json({"type": "settings", "settings": {
            "gear": 1, "inverted": ["d", "z"], "links": {"a": "b"}, "program": "a + 1\n"}})
        return await receive_state(ws)
    state = with_server(fake_module, test)
    assert fake_module.inverted == {"d"} and fake_module.links == {"a": "b"}
    assert state["settings"]["gear"] == 1
    saved = json.loads(tmp_config.read_text())
    assert saved["gear"] == 1 and saved["inverted"] == ["d"] and saved["program"] == "a + 1\n"


def test_run_program(fake_module):
    async def test(client, ws, remote):
        await ws.send_json({"type": "settings", "settings": {"program": "gear 1\na + 1\n"}})
        await ws.send_json({"type": "run_program"})
        for _ in range(50):
            await asyncio.sleep(0.02)
            if not remote.running:
                break
        return remote.log
    log = with_server(fake_module, test)
    assert ("run", 1.0, {"a": 60}) in fake_module.calls
    assert log[-1] == "All done!"


def test_bad_program_is_reported(fake_module):
    async def test(client, ws, remote):
        await ws.send_json({"type": "settings", "settings": {"program": "fly 3\n"}})
        await ws.send_json({"type": "run_program"})
        while not (state := await receive_state(ws))["log"]:
            pass
        return state["log"]
    assert with_server(fake_module, test)[-1].startswith("Oops")
