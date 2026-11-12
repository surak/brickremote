"""Control JG_JMC motor modules: `brickremote` (remote control), `scan`, `run`, `program`, `path`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from . import protocol, script
from .car import Car
from .module import JMCModule, discover


async def _scan(args) -> None:
    devices = await discover(args.timeout, settle=None if args.full else 2.0,
                             on_found=lambda dev: print(f"{dev.address}  {(dev.name or '').strip()}", flush=True))
    if not devices:
        print("no modules found - is it switched on, and not connected to anything else?")


async def _wait_battery(module: JMCModule) -> None:
    for _ in range(20):  # the first status report takes a moment
        if module.battery is not None:
            print(f"battery: {module.battery}%")
            return
        await asyncio.sleep(0.1)


def _connect(args) -> JMCModule:
    return JMCModule(args.address, slot=args.slot, six_ports=args.six_ports, inverted=args.invert)


async def _run(args) -> None:
    speeds = {}
    for item in args.speeds:
        port, _, value = item.partition("=")
        if port.lower() not in protocol.PORTS or not value:
            raise SystemExit(f"bad argument {item!r}; expected e.g. a=255 or d=-120")
        speeds[port.lower()] = int(value)
    async with _connect(args) as module:
        await _wait_battery(module)
        await module.run(args.time, **speeds)


async def _program(args) -> None:
    try:
        steps = script.parse_program(args.file.read())
    except script.ScriptError as exc:
        raise SystemExit(f"error: {exc}") from None
    async with _connect(args) as module:
        await _wait_battery(module)
        await script.run_program(module, steps, gear=args.gear)


async def _path(args) -> None:
    try:
        moves = script.parse_path(args.file.read())
    except script.ScriptError as exc:
        raise SystemExit(f"error: {exc}") from None
    cal = script.Calibration(args.mps, args.dps)
    async with _connect(args) as module:
        await _wait_battery(module)
        await script.run_path(Car(module, drive_port=args.drive, steer_port=args.steer), moves, cal)


def main() -> None:
    parser = argparse.ArgumentParser(prog="brickremote", description=__doc__)
    parser.add_argument("--address", help="module address (default: first one found)")
    parser.add_argument("--slot", type=int, default=1, help="module slot 1-3 (default 1)")
    parser.add_argument("--six-ports", action="store_true", help="module has ports E and F")
    parser.add_argument("--invert", default="", metavar="PORTS", help="ports to reverse, e.g. --invert ad")
    parser.add_argument("--drive", default="a", help="car drive port (default a)")
    parser.add_argument("--steer", default="d", help="car steering port (default d)")
    parser.add_argument("--log", metavar="FILE", help="write a debug log (keys and commands sent) to FILE")
    sub = parser.add_subparsers(dest="command")

    scan = sub.add_parser("scan", help="list nearby modules")
    scan.add_argument("--timeout", type=float, default=10.0, help="give up after this many seconds (default 10)")
    scan.add_argument("--full", action="store_true", help="always scan the whole timeout, to find every module")
    scan.set_defaults(func=_scan)

    run = sub.add_parser("run", help="run ports for a while, e.g. `run a=255 d=-255 --time 2`")
    run.add_argument("speeds", nargs="+", metavar="PORT=SPEED", help="speed -255..255 per port (a-f)")
    run.add_argument("--time", type=float, default=2.0, help="seconds to run (default 2)")
    run.set_defaults(func=_run)

    prog = sub.add_parser("program", help="run a program script ('-' for stdin)",
                          description=script.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    prog.add_argument("file", type=argparse.FileType())
    prog.add_argument("--gear", type=int, default=3, choices=(1, 2, 3), help="initial gear (default 3)")
    prog.set_defaults(func=_program)

    path = sub.add_parser("path", help="drive a path script ('-' for stdin)",
                          description=script.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    path.add_argument("file", type=argparse.FileType())
    path.add_argument("--mps", type=float, default=0.5, help="metres per second at full speed (default 0.5)")
    path.add_argument("--dps", type=float, default=90.0, help="degrees per second when turning (default 90)")
    path.set_defaults(func=_path)

    web = sub.add_parser("web", help="start the web remote control (open it in any browser on the network)")
    web.add_argument("--port", type=int, default=8080, help="port to listen on (default 8080)")
    web.add_argument("--host", default="0.0.0.0", help="address to listen on (default: all)")
    web.set_defaults(func=None)

    args = parser.parse_args()
    if args.log:
        logging.basicConfig(filename=args.log, level=logging.DEBUG,
                            format="%(asctime)s %(name)s %(message)s")
        logging.getLogger("bleak").setLevel(logging.INFO)
    if args.command is None:
        from . import tui
        tui.run(args.address, args.drive, args.steer)
        return
    if args.command == "web":
        try:
            from . import web as webapp
        except ImportError:
            sys.exit("The web remote needs aiohttp: pip install 'brickremote[web]'")
        webapp.run(args.address, args.drive, args.steer, args.host, args.port)
        return
    try:
        asyncio.run(args.func(args))
    except ConnectionError as exc:
        sys.exit(f"error: {exc}")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
