"""Convenience wrapper for a car with one drive motor and one steering motor."""

from __future__ import annotations

from .module import JMCModule


class Car:
    """A car built on a JMCModule.

    Defaults match the stock car: drive motor on port A (positive = forward),
    steering on port D (positive = left, self-centring when stopped).
    """

    def __init__(self, module: JMCModule, *, drive_port: str = "a", steer_port: str = "d",
                 invert_drive: bool = False, invert_steer: bool = False):
        self.module = module
        self.drive_port, self.steer_port = drive_port, steer_port
        self._drive_sign = -1 if invert_drive else 1
        self._steer_sign = -1 if invert_steer else 1

    async def drive(self, speed: int, steer: int = 0) -> None:
        """speed: -255..255 (positive = forward); steer: -255..255 (positive = left)."""
        await self.module.drive(**{self.drive_port: self._drive_sign * speed,
                                   self.steer_port: self._steer_sign * steer})

    async def forward(self, speed: int = 255) -> None:
        await self.drive(speed)

    async def backward(self, speed: int = 255) -> None:
        await self.drive(-speed)

    async def left(self, speed: int = 255, steer: int = 255) -> None:
        await self.drive(speed, steer)

    async def right(self, speed: int = 255, steer: int = 255) -> None:
        await self.drive(speed, -steer)

    async def stop(self) -> None:
        await self.module.stop()
