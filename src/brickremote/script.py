"""Text versions of the official app's "Program" (blocks) and "Path" modes.

Program scripts, one command per line (`#` starts a comment):

    gear 2              set speed for `+` / `-` (1 = 60, 2 = 120, 3 = 255)
    a + 2               port A forward for 2 s at the current gear
    ac - 1.5            ports A and C reverse for 1.5 s
    d 200 1             port D at speed 200 for 1 s (-255..255)
    b +                 port B forward, keep running
    wait 1              pause 1 s (motors keep their state)
    stop                stop every port
    repeat 3            repeat the block up to `end` three times
    forever             repeat the block up to `end` until stopped
    end

Path scripts drive a car (drive port + steering port), distances in metres,
turns in degrees; `Calibration` turns them into times:

    forward 1.5
    left 90
    back 0.5
    right 45
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .car import Car
from .module import JMCModule
from .protocol import GEARS, PORTS


class ScriptError(ValueError):
    pass


@dataclass
class Step:
    op: str               # "drive", "wait", "stop", "gear", "repeat"
    ports: str = ""
    speed: str | int = 0  # "+", "-" or an int
    seconds: float | None = None
    count: int | None = None  # repeat count, None = forever
    body: list[Step] | None = None


def parse_program(text: str) -> list[Step]:
    stack: list[list[Step]] = [[]]
    for lineno, raw in enumerate(text.splitlines(), 1):
        words = raw.split("#", 1)[0].split()
        if not words:
            continue
        cmd, args = words[0].lower(), words[1:]
        try:
            if cmd == "end":
                if len(stack) == 1:
                    raise ScriptError("`end` without `repeat`/`forever`")
                stack.pop()
            elif cmd in ("repeat", "forever"):
                step = Step("repeat", count=int(args[0]) if cmd == "repeat" else None, body=[])
                stack[-1].append(step)
                stack.append(step.body)
            elif cmd == "wait":
                stack[-1].append(Step("wait", seconds=float(args[0])))
            elif cmd == "stop":
                stack[-1].append(Step("stop"))
            elif cmd == "gear":
                gear = int(args[0])
                if not 1 <= gear <= len(GEARS):
                    raise ScriptError("gear must be 1, 2 or 3")
                stack[-1].append(Step("gear", count=gear))
            elif cmd and all(c in PORTS for c in cmd):
                speed = args[0] if args[0] in "+-" else int(args[0])
                seconds = float(args[1]) if len(args) > 1 else None
                stack[-1].append(Step("drive", ports=cmd, speed=speed, seconds=seconds))
            else:
                raise ScriptError(f"unknown command {cmd!r}")
        except (IndexError, ValueError) as exc:
            raise ScriptError(f"line {lineno}: {raw.strip()!r}: {exc or 'missing argument'}") from None
    if len(stack) != 1:
        raise ScriptError("missing `end`")
    return stack[0]


async def run_program(module: JMCModule, steps: list[Step], gear: int = 3, log=print) -> None:
    """Run a parsed program. Cancel the task to abort; motors are stopped either way."""
    state = {"gear": gear}

    async def run(block: list[Step]) -> None:
        for step in block:
            if step.op == "gear":
                state["gear"] = step.count
            elif step.op == "wait":
                await asyncio.sleep(step.seconds)
            elif step.op == "stop":
                await module.stop()
            elif step.op == "repeat":
                n = 0
                while step.count is None or n < step.count:
                    await run(step.body)
                    n += 1
            elif step.op == "drive":
                speed = step.speed
                if speed in ("+", "-"):
                    speed = GEARS[state["gear"] - 1] * (1 if speed == "+" else -1)
                speeds = dict.fromkeys(step.ports, speed)
                log(f"{step.ports.upper()} {speed:+d}" + (f" for {step.seconds}s" if step.seconds else ""))
                if step.seconds is None:
                    await module.drive(**speeds)
                else:
                    await module.run(step.seconds, **speeds)

    try:
        await run(steps)
    finally:
        await module.stop()


@dataclass
class Calibration:
    metres_per_second: float = 0.5   # at full speed, straight
    degrees_per_second: float = 90.0  # at full speed, full steering lock


def parse_path(text: str) -> list[tuple[str, float]]:
    moves = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        words = raw.split("#", 1)[0].split()
        if not words:
            continue
        cmd = {"fwd": "forward", "backward": "back", "bwd": "back"}.get(words[0].lower(), words[0].lower())
        if cmd not in ("forward", "back", "left", "right") or len(words) != 2:
            raise ScriptError(f"line {lineno}: expected `forward|back|left|right <amount>`, got {raw.strip()!r}")
        try:
            moves.append((cmd, float(words[1])))
        except ValueError:
            raise ScriptError(f"line {lineno}: {words[1]!r} is not a number") from None
    return moves


async def run_path(car: Car, moves: list[tuple[str, float]], cal: Calibration, log=print) -> None:
    """Drive a path at full speed. Turns are made driving forward with full steering."""
    try:
        for cmd, amount in moves:
            if cmd in ("forward", "back"):
                seconds = amount / cal.metres_per_second
                log(f"{cmd} {amount} m ({seconds:.1f}s)")
                await car.drive(255 if cmd == "forward" else -255)
            else:
                seconds = amount / cal.degrees_per_second
                log(f"{cmd} {amount}° ({seconds:.1f}s)")
                await car.drive(255, 255 if cmd == "left" else -255)
            await asyncio.sleep(seconds)
            await car.stop()
            await asyncio.sleep(0.3)  # let the steering re-centre
    finally:
        await car.stop()
