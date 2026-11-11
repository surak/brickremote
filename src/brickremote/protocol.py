"""Wire format of JG_JMC-xxxx motor modules.

Commands are written (with response) to characteristic FFF2:

    5A 6B 02 00 LEN  dirAB speedAB  dirCD speedCD  module  [dirEF speedE speedF]  SUM

    LEN      payload length: 5 for 4-port modules, 8 for 6-port modules
    dirAB    A: 1 = forward, 2 = reverse; B: +4 = forward, +8 = reverse
    dirCD    same for C and D
    speed    0..255, shared by both ports of a pair
    module   1..3, the module slot (the official app can drive three at once)
    SUM      sum of all previous bytes, modulo 256

The module reports on FFF1:

    5A 6B 01 00 01  battery%  SUM
"""

from __future__ import annotations

from dataclasses import dataclass

SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000fff2-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"
NAME_PREFIX = "JG_JMC-"

HEADER = 0x5A
FRAME_TYPE = 0x6B
CMD_MOTORS = 0x02
CMD_STATUS = 0x01

MAX_SPEED = 255
GEARS = (60, 120, 255)  # the official app's gears 1, 2 and 3
PORTS = ("a", "b", "c", "d", "e", "f")


def checksum(data: bytes | list[int]) -> int:
    return sum(data) & 0xFF


def _clamp(speed: int) -> int:
    return max(-MAX_SPEED, min(MAX_SPEED, int(speed)))


def _pair(first: int, second: int) -> tuple[int, int]:
    """Direction bits and shared speed for two ports."""
    first, second = _clamp(first), _clamp(second)
    dirs = (1 if first > 0 else 2 if first < 0 else 0) | (4 if second > 0 else 8 if second < 0 else 0)
    return dirs, max(abs(first), abs(second))


def motor_frame(a: int = 0, b: int = 0, c: int = 0, d: int = 0, e: int = 0, f: int = 0,
                *, module: int = 1, six_ports: bool = False) -> bytes:
    """Build a motor command. Speeds are -255..255, negative means reverse.

    Both ports of a pair (A/B, C/D, E/F) share one speed byte, so when both
    run, the faster of the two is used.
    """
    if not 1 <= module <= 3:
        raise ValueError("module must be 1, 2 or 3")
    if not six_ports and (e or f):
        raise ValueError("ports E and F need six_ports=True")
    body = [HEADER, FRAME_TYPE, CMD_MOTORS, 0x00, 8 if six_ports else 5]
    body += _pair(a, b)
    body += _pair(c, d)
    body.append(module)
    if six_ports:
        dirs, _ = _pair(e, f)
        body += [dirs, abs(_clamp(e)), abs(_clamp(f))]
    body.append(checksum(body))
    return bytes(body)


def stop_frame(*, module: int = 1, six_ports: bool = False) -> bytes:
    return motor_frame(module=module, six_ports=six_ports)


@dataclass(frozen=True)
class Status:
    battery: int  # percent


def parse_status(data: bytes) -> Status | None:
    """Parse a FFF1 notification; returns None for anything unrecognised."""
    if len(data) < 7 or data[0] != HEADER or data[1] != FRAME_TYPE or data[2] != CMD_STATUS:
        return None
    if checksum(data[:-1]) != data[-1]:
        return None
    return Status(battery=data[5])
