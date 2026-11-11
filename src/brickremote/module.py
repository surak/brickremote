"""Async client for a JG_JMC-xxxx motor module."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import subprocess
from collections.abc import Callable, Iterable

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

from . import protocol
from .protocol import Status

log = logging.getLogger(__name__)


async def discover(timeout: float = 10.0, settle: float | None = 2.0,
                   on_found: Callable[[BLEDevice], None] | None = None) -> list[BLEDevice]:
    """Scan for modules.

    Stops `settle` seconds after the last new module appeared (None = always scan the full
    `timeout`), or after `timeout` seconds. `on_found` is called as each module is seen.
    """
    found: dict[str, BLEDevice] = {}
    last_new = asyncio.Event()

    def seen(dev: BLEDevice, adv) -> None:
        if dev.address not in found and (adv.local_name or dev.name or "").startswith(protocol.NAME_PREFIX):
            found[dev.address] = dev
            last_new.set()
            if on_found:
                on_found(dev)

    async with BleakScanner(seen):
        deadline = asyncio.get_running_loop().time() + timeout
        while (remaining := deadline - asyncio.get_running_loop().time()) > 0:
            wait = remaining if settle is None or not found else min(settle, remaining)
            last_new.clear()
            try:
                await asyncio.wait_for(last_new.wait(), wait)
            except asyncio.TimeoutError:
                if found and settle is not None:
                    break
    return list(found.values())


async def find(address: str | None = None, timeout: float = 10.0) -> BLEDevice | None:
    """Find one module, by address if given, otherwise the first one advertising."""
    if address:
        return await BleakScanner.find_device_by_address(address, timeout=timeout)
    return await BleakScanner.find_device_by_filter(
        lambda dev, adv: (adv.local_name or dev.name or "").startswith(protocol.NAME_PREFIX),
        timeout=timeout)


class JMCModule:
    """A connected motor module.

    The current motor state is resent every `resend_interval` seconds while
    connected, which keeps the module awake and recovers from dropped writes.

        async with JMCModule() as m:
            await m.drive(a=255)
            await asyncio.sleep(2)
            await m.stop()

    `inverted` flips the direction of the given ports (like the app's wrench
    screen); `links` makes ports follow others, e.g. {"a": "c"} drives C
    whenever A is driven (the app's A+C button).
    """

    def __init__(self, device: BLEDevice | str | None = None, *, slot: int = 1,
                 six_ports: bool = False, resend_interval: float | None = 0.2,
                 timeout: float = 20.0, inverted: Iterable[str] = (),
                 links: dict[str, str] | None = None):
        self._device = device
        self.slot = slot
        self.six_ports = six_ports
        self.resend_interval = resend_interval
        self.timeout = timeout
        self.inverted = set(inverted)
        self.links = dict(links or {})
        self.status: Status | None = None
        self.on_status: Callable[[Status], None] | None = None
        self._client: BleakClient | None = None
        self._speeds = dict.fromkeys(protocol.PORTS, 0)
        self._resender: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._last_frame = b""

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    @property
    def address(self) -> str | None:
        """Address of the connected module."""
        return self._client.address if self._client else None

    def detach(self) -> list[str] | None:
        """Forget the connection without waiting for it to close, and return a command that closes it.

        For apps that want to exit at once: run the returned command in the background. Only
        available on Linux (BlueZ); returns None elsewhere, then use `disconnect()` instead.
        """
        path = getattr(getattr(self._client, "_backend", None), "_device_path", None)
        if not path or not shutil.which("busctl"):
            return None
        if self._resender:
            self._resender.cancel()
            self._resender = None
        self._client = None
        return ["busctl", "call", "org.bluez", path, "org.bluez.Device1", "Disconnect"]

    @property
    def battery(self) -> int | None:
        return self.status.battery if self.status else None

    async def connect(self) -> None:
        device = self._device
        if device is None or isinstance(device, str):
            device = await find(device, timeout=self.timeout)
            if device is None:
                raise ConnectionError("no JG_JMC module found; is it switched on and not connected elsewhere?")
        self._client = BleakClient(device, timeout=self.timeout)
        await self._client.connect()
        await self._client.start_notify(protocol.NOTIFY_UUID, self._handle_notify)
        if self.resend_interval:
            self._resender = asyncio.create_task(self._resend_loop())
        log.info("connected to %s", device)

    async def disconnect(self) -> None:
        if self._resender:
            self._resender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._resender
            self._resender = None
        if self._client:
            if self._client.is_connected:
                with contextlib.suppress(Exception):
                    await self.stop()
                await self._client.disconnect()
            self._client = None

    async def keep_connected(self, *, on_connected: Callable[[], None] | None = None,
                             on_lost: Callable[[], None] | None = None,
                             on_error: Callable[[Exception], None] | None = None,
                             retry: float = 1.0, poll: float = 0.5) -> None:
        """Connect, and reconnect whenever the connection drops (e.g. the module switched itself off).

        Runs until cancelled. `on_lost` is called before each connection attempt, `on_error` when
        an attempt fails, and `on_connected` when it succeeds.
        """
        while True:
            if not self.is_connected:
                if on_lost:
                    on_lost()
                try:
                    await self.disconnect()  # clean up a dropped connection
                    await self.connect()
                except Exception as exc:
                    log.debug("connect failed: %s", exc)
                    if on_error:
                        on_error(exc)
                    await asyncio.sleep(retry)
                    continue
                if on_connected:
                    on_connected()
            await asyncio.sleep(poll)

    async def close(self, *, fast: bool = True) -> None:
        """Stop the motors and disconnect.

        With `fast` (on Linux with BlueZ) this returns at once and BlueZ finishes closing the link
        in the background, so an app can exit without waiting a couple of seconds.
        """
        if not self.is_connected:
            return
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.stop(), 0.5)
        command = self.detach() if fast else None
        if command:
            subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.disconnect(), 5)

    async def __aenter__(self) -> JMCModule:
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.disconnect()

    async def drive(self, **speeds: int) -> None:
        """Set port speeds, e.g. drive(a=255, d=-120). Ports not given keep their speed."""
        for port, speed in speeds.items():
            if port not in self._speeds:
                raise ValueError(f"unknown port {port!r}; use one of {', '.join(protocol.PORTS)}")
            self._speeds[port] = speed
            if port in self.links:
                self._speeds[self.links[port]] = speed
        await self._send()

    async def run(self, seconds: float, **speeds: int) -> None:
        """Drive the given ports for `seconds`, then stop them."""
        await self.drive(**speeds)
        try:
            await asyncio.sleep(seconds)
        finally:
            await self.drive(**dict.fromkeys(speeds, 0))

    @property
    def speeds(self) -> dict[str, int]:
        """Current speed per port, as last set."""
        return dict(self._speeds)

    async def stop(self) -> None:
        """Stop every port."""
        self._speeds = dict.fromkeys(protocol.PORTS, 0)
        await self._send()

    async def _send(self) -> None:
        if not self.is_connected:
            raise ConnectionError("not connected")
        speeds = {p: -s if p in self.inverted else s for p, s in self._speeds.items()}
        frame = protocol.motor_frame(**speeds, module=self.slot, six_ports=self.six_ports)
        if frame != self._last_frame:
            log.debug("send %s", frame.hex(" "))
            self._last_frame = frame
        async with self._lock:
            # The module ignores write-without-response.
            await self._client.write_gatt_char(protocol.WRITE_UUID, frame, response=True)

    async def _resend_loop(self) -> None:
        while True:
            await asyncio.sleep(self.resend_interval)
            try:
                await self._send()
            except Exception as exc:  # keep resending; disconnects surface on the next user call
                log.debug("resend failed: %s", exc)

    def _handle_notify(self, _sender, data: bytearray) -> None:
        status = protocol.parse_status(bytes(data))
        if status:
            self.status = status
            if self.on_status:
                self.on_status(status)
