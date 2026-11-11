"""Web remote control: `brickremote web` serves a touch-friendly page to any browser on the network.

The browser talks to the server over a WebSocket. While a control is held, the page repeats
its state several times a second; if those messages stop (closed tab, lost Wi-Fi), the motors
are stopped after a moment.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket
import time
from importlib import resources

from aiohttp import WSMsgType, web

from .car import Car
from .module import JMCModule
from .protocol import PORTS
from .script import Calibration, ScriptError, parse_path, parse_program, run_path, run_program
from .tui import EXAMPLE_PATH, EXAMPLE_PROGRAM, load_config, save_config

log = logging.getLogger(__name__)

DEADMAN = 0.7  # seconds without a drive message before moving motors are stopped


class Remote:
    """Shared state between all connected browsers."""

    def __init__(self, module: JMCModule, car: Car):
        self.module, self.car = module, car
        cfg = load_config()
        self.cfg = cfg
        self.module.inverted = set(cfg.get("inverted", []))
        self.settings = {
            "gear": cfg.get("gear", 3),
            "program": cfg.get("program", EXAMPLE_PROGRAM),
            "path": cfg.get("path", EXAMPLE_PATH),
            "calibration": cfg.get("calibration", vars(Calibration())),
            "inverted": sorted(self.module.inverted),
            "links": {},
            "drive_port": car.drive_port,
            "steer_port": car.steer_port,
        }
        self.clients: set[web.WebSocketResponse] = set()
        self.task: asyncio.Task | None = None
        self.log: list[str] = []
        self.last_drive = 0.0

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def say(self, msg: str) -> None:
        log.debug("say %s", msg)
        self.log = (self.log + [msg])[-5:]

    def persist(self) -> None:
        self.cfg.update(gear=self.settings["gear"], inverted=self.settings["inverted"],
                        calibration=self.settings["calibration"], program=self.settings["program"],
                        path=self.settings["path"])
        save_config(self.cfg)

    def state(self) -> dict:
        return {"type": "state", "connected": self.module.is_connected, "battery": self.module.battery,
                "speeds": self.module.speeds, "running": self.running, "log": self.log,
                "settings": self.settings}

    async def broadcast(self) -> None:
        msg = json.dumps(self.state())
        for ws in list(self.clients):
            with contextlib.suppress(Exception):
                await ws.send_str(msg)

    def start(self, coro) -> None:
        self.cancel()
        self.task = asyncio.create_task(coro)
        self.task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task) -> None:
        if task.cancelled():
            self.say("Stopped.")
        elif task.exception():
            self.say(f"Oops: {task.exception()}")
        else:
            self.say("All done!")

    def cancel(self) -> None:
        if self.running:
            self.task.cancel()

    async def handle(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "drive":
            if self.running or not self.module.is_connected:
                return
            speeds = {p: max(-255, min(255, int(v))) for p, v in msg.get("speeds", {}).items() if p in PORTS}
            self.last_drive = time.monotonic()
            if any(self.module.speeds[p] != v for p, v in speeds.items()):
                await self.module.drive(**speeds)
        elif kind == "stop":
            self.cancel()
            if self.module.is_connected:
                await self.module.stop()
            self.say("STOP!")
        elif kind == "settings":
            new = msg.get("settings", {})
            for key in ("gear", "program", "path", "calibration"):
                if key in new:
                    self.settings[key] = new[key]
            if "inverted" in new:
                self.settings["inverted"] = sorted(p for p in new["inverted"] if p in PORTS)
                self.module.inverted = set(self.settings["inverted"])
            if "links" in new:
                self.settings["links"] = {a: b for a, b in new["links"].items() if a in PORTS and b in PORTS}
                self.module.links = dict(self.settings["links"])
            self.persist()
        elif kind == "run_program":
            try:
                steps = parse_program(self.settings["program"])
            except ScriptError as exc:
                self.say(f"Oops: {exc}")
            else:
                self.say("Running your program!")
                self.start(run_program(self.module, steps, int(self.settings["gear"]), log=self.say))
        elif kind == "run_path":
            try:
                moves = parse_path(self.settings["path"])
            except ScriptError as exc:
                self.say(f"Oops: {exc}")
            else:
                self.say("Off we go!")
                cal = Calibration(**self.settings["calibration"])
                self.start(run_path(self.car, moves, cal, log=self.say))

    async def deadman(self) -> None:
        """Stop motors when browsers stop sending while something is moving."""
        while True:
            await asyncio.sleep(0.1)
            moving = any(self.module.speeds.values())
            if moving and not self.running and time.monotonic() - self.last_drive > DEADMAN:
                with contextlib.suppress(Exception):
                    await self.module.stop()

    async def keep_connected(self) -> None:
        while True:
            if not self.module.is_connected:
                self.cancel()
                try:
                    await self.module.disconnect()
                    await self.module.connect()
                    self.say("Connected! Let's go!")
                except Exception as exc:
                    log.debug("connect failed: %s", exc)
                    await asyncio.sleep(1)
                    continue
            await asyncio.sleep(0.5)

    async def ticker(self) -> None:
        while True:
            await self.broadcast()
            await asyncio.sleep(0.2)


async def index(request: web.Request) -> web.Response:
    html = resources.files("brickremote").joinpath("web/index.html").read_text(encoding="utf-8")
    return web.Response(text=html, content_type="text/html")


async def websocket(request: web.Request) -> web.WebSocketResponse:
    remote: Remote = request.app["remote"]
    ws = web.WebSocketResponse(heartbeat=5)
    await ws.prepare(request)
    remote.clients.add(ws)
    await ws.send_str(json.dumps(remote.state()))
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    await remote.handle(json.loads(msg.data))
                except Exception as exc:
                    remote.say(f"Oops: {exc}")
                await remote.broadcast()
    finally:
        remote.clients.discard(ws)
    return ws


def local_addresses() -> list[str]:
    addresses = set()
    with contextlib.suppress(OSError):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 1))  # no packet is sent; this just picks the outgoing interface
        addresses.add(s.getsockname()[0])
        s.close()
    with contextlib.suppress(OSError):
        addresses.add(socket.gethostname() + ".local")
    return sorted(addresses)


async def serve(module: JMCModule, car: Car, host: str, port: int) -> None:
    remote = Remote(module, car)
    app = web.Application()
    app["remote"] = remote
    app.router.add_get("/", index)
    app.router.add_get("/ws", websocket)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    print("Brick Remote is running! Open one of these in a browser:", flush=True)
    print(f"  http://localhost:{port}")
    if host in ("0.0.0.0", "::", ""):
        for addr in local_addresses():
            print(f"  http://{addr}:{port}  (from a tablet or another computer)")
    print("Press Ctrl-C to stop.", flush=True)
    tasks = [asyncio.create_task(t) for t in (remote.keep_connected(), remote.deadman(), remote.ticker())]
    try:
        await asyncio.Event().wait()
    finally:
        for t in tasks:
            t.cancel()
        remote.cancel()
        if module.is_connected:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(module.stop(), 0.5)
            command = module.detach()
            if command:
                import subprocess
                subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        await runner.cleanup()


def run(address: str | None = None, drive_port: str = "a", steer_port: str = "d",
        host: str = "0.0.0.0", port: int = 8080) -> None:
    module = JMCModule(address, timeout=5.0)
    car = Car(module, drive_port=drive_port, steer_port=steer_port)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve(module, car, host, port))
