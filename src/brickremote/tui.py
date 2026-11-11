"""Terminal remote control, modelled on the official app minus the phone-only modes.

Tabs: Car (steering wheel), Ports (A-D sticks), Program, Path.
Terminals only report key presses, not releases, so a motor keeps running while its key
auto-repeats and stops shortly after you let go. With hold on, the drive motors keep
running until you press space; steering always springs back.
"""

from __future__ import annotations

import asyncio
import curses
import functools
import math
import os
import subprocess
import tempfile
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from .car import Car
from .config import EXAMPLE_PATH, EXAMPLE_PROGRAM, load_config, save_config
from .module import JMCModule
from .protocol import GEARS
from .script import Calibration, ScriptError, parse_path, parse_program, run_path, run_program

log = logging.getLogger(__name__)

TABS = ("Car", "Ports", "Program", "Path")
TAB_ICONS = ("◉", "✚", "≡", "⌁")

# Release detection: before auto-repeat kicks in (~0.5 s after the first press) we must wait;
# once a key is repeating, a short gap in the repeats means it was let go.
FIRST_HOLD = 0.6
REPEAT_HOLD = 0.15

# Ports tab: (forward key, reverse key) per port.
# Like two game sticks: WASD drives A (up/down) and B (left/right), IJKL drives C and D.
PORT_KEYS = {"a": ("w", "s"), "b": ("d", "a"), "c": ("i", "k"), "d": ("l", "j")}
LINKS = (None, ("a", "b"), ("a", "c"))

BIG_DIGITS = {
    1: (" ██ ", "███ ", " ██ ", " ██ ", "████"),
    2: ("███ ", "   █", " ██ ", "█   ", "████"),
    3: ("███ ", "   █", " ██ ", "   █", "███ "),
}

# colour pair numbers
GREEN, ON_GREEN, DIM, YELLOW, RED, BLUE, ON_YELLOW, ON_RED, WHITE, ORANGE = range(1, 11)


def init_colours() -> None:
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    orange = 208 if curses.COLORS >= 256 else curses.COLOR_YELLOW
    green = 112 if curses.COLORS >= 256 else curses.COLOR_GREEN
    pairs = {
        GREEN: (green, bg), ON_GREEN: (curses.COLOR_BLACK, green), DIM: (8 if curses.COLORS > 8 else curses.COLOR_WHITE, bg),
        YELLOW: (curses.COLOR_YELLOW, bg), RED: (curses.COLOR_RED, bg), BLUE: (curses.COLOR_BLUE, bg),
        ON_YELLOW: (curses.COLOR_BLACK, curses.COLOR_YELLOW), ON_RED: (curses.COLOR_WHITE, curses.COLOR_RED),
        WHITE: (curses.COLOR_WHITE, bg), ORANGE: (orange, bg),
    }
    for n, (fg, b) in pairs.items():
        curses.init_pair(n, fg, b)


@functools.lru_cache(maxsize=256)
def wheel(angle: float, w: int = 23, h: int = 11) -> list[list[bool]]:
    """Rasterise a steering wheel turned by `angle` radians (positive = left)."""
    grid = [[False] * w for _ in range(h)]
    spokes = [angle + a for a in (0.0, math.pi, -math.pi / 2)]  # left, right, down
    for row in range(h):
        for col in range(w):
            x = (col - (w - 1) / 2) / ((w - 1) / 2)
            y = (row - (h - 1) / 2) / ((h - 1) / 2)
            r = math.hypot(x, y)
            on = 0.84 <= r <= 1.02 or r <= 0.2
            if not on and r < 0.86:
                for s in spokes:
                    dx, dy = math.cos(s), -math.sin(s)  # screen y grows downwards
                    along = x * dx + y * dy
                    across = abs(x * dy - y * dx)
                    if along > 0 and across < 0.1:
                        on = True
            grid[row][col] = on
    return grid


@dataclass
class Held:
    direction: int
    first: float
    last: float
    repeats: int = 0


class App:
    def __init__(self, scr, module: JMCModule, car: Car):
        self.scr, self.module, self.car = scr, module, car
        cfg = load_config()
        self.cfg = cfg
        self.tab = 0
        self.gear = cfg.get("gear", 3)
        self.lock = False
        self.link = 0
        self.module.inverted = set(cfg.get("inverted", []))
        self.cal = Calibration(**cfg.get("calibration", {}))
        self.program = cfg.get("program", EXAMPLE_PROGRAM)
        self.path = cfg.get("path", EXAMPLE_PATH)
        self.held: dict[str, Held] = {}
        self.task: asyncio.Task | None = None
        self.log: list[str] = ["Hi! Use the arrow keys to drive."]
        self.awaiting_invert = False
        self.quit = False
        self.steer_angle = 0.0  # animated wheel angle
        self.connect_error = ""
        self.hits: list[tuple[int, int, int, int, object]] = []  # clickable regions
        self.mouse_hold: str | None = None  # motor button held down with the mouse

    # --- helpers -------------------------------------------------------------
    def say(self, msg: str) -> None:
        log.debug("say %s", msg)
        self.log = (self.log + [msg])[-3:]

    def persist(self) -> None:
        self.cfg.update(gear=self.gear, inverted=sorted(self.module.inverted),
                        calibration=vars(self.cal), program=self.program, path=self.path)
        save_config(self.cfg)

    @property
    def speed(self) -> int:
        return GEARS[self.gear - 1]

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def start(self, coro) -> None:
        self.cancel()
        self.task = asyncio.create_task(coro)
        self.task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception():
            self.say(f"Oops: {task.exception()}")
        else:
            self.say("All done!" if not task.cancelled() else "Stopped.")

    def cancel(self) -> None:
        if self.running:
            self.task.cancel()

    def edit(self, text: str) -> str:
        """Edit text in $EDITOR, suspending curses meanwhile."""
        with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False) as f:
            f.write(text)
            name = f.name
        curses.def_prog_mode()
        curses.endwin()
        try:
            subprocess.call([os.environ.get("EDITOR", "nano"), name])
            return Path(name).read_text()
        finally:
            os.unlink(name)
            curses.reset_prog_mode()
            self.scr.refresh()

    def press(self, name: str, direction: int, now: float) -> None:
        h = self.held.get(name)
        if h and h.direction == direction:
            h.last, h.repeats = now, h.repeats + 1
        else:
            self.held[name] = Held(direction, now, now)

    def active(self, name: str) -> int:
        h = self.held.get(name)
        return h.direction if h else 0

    # --- input ---------------------------------------------------------------
    def select_tab(self, i: int) -> None:
        self.tab = i
        self.held.clear()

    def mouse(self) -> None:
        try:
            _, mx, my, _, state = curses.getmouse()
        except curses.error:
            return
        if state & curses.BUTTON1_RELEASED and self.mouse_hold:
            self.held.pop(self.mouse_hold, None)
            self.mouse_hold = None
        if not state & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED):
            return
        for y, x, height, width, action in reversed(self.hits):
            if y <= my < y + height and x <= mx < x + width:
                if isinstance(action, tuple):
                    _, name, direction = action
                    self.press(name, direction, time.monotonic())
                    # a press waits for its release; a bare click acts like a short key press
                    self.mouse_hold = name if state & curses.BUTTON1_PRESSED else None
                else:
                    action()
                return

    def key(self, k: int) -> None:
        log.debug("key %r on %s", k, TABS[self.tab])
        ch = chr(k).lower() if 0 <= k < 256 else ""
        raw = chr(k) if 0 <= k < 256 else ""
        now = time.monotonic()
        if self.awaiting_invert:
            self.awaiting_invert = False
            if ch in PORT_KEYS:
                self.module.inverted ^= {ch}
                self.say(f"Port {ch.upper()} now turns {'the other way round' if ch in self.module.inverted else 'the normal way'}.")
                self.persist()
            return
        if k == 9:  # Tab
            self.tab = (self.tab + 1) % len(TABS)
            self.held.clear()
        elif k == curses.KEY_BTAB:
            self.tab = (self.tab - 1) % len(TABS)
            self.held.clear()
        elif ch == "q":
            self.quit = True
        elif ch == " ":
            self.held.clear()
            self.cancel()
            asyncio.create_task(self.module.stop())
            self.say("STOP!")
        elif ch and ch in "123":
            self.gear = int(ch)
            self.persist()
        elif ch == "h":
            self.lock = not self.lock
            self.say("Hold on: motors keep going when you let go." if self.lock else "Hold off.")
        elif ch == "p":
            self.link = (self.link + 1) % len(LINKS)
            pair = LINKS[self.link]
            self.module.links = {pair[0]: pair[1]} if pair else {}
            self.say("Pair off." if not pair else f"Paired: {pair[1].upper()} now does whatever {pair[0].upper()} does.")
        elif ch == "r":
            self.awaiting_invert = True
            self.say("Reverse which port? Press a, b, c or d.")
        elif TABS[self.tab] == "Car":
            axis = {curses.KEY_UP: ("drive", 1), curses.KEY_DOWN: ("drive", -1),
                    curses.KEY_LEFT: ("steer", 1), curses.KEY_RIGHT: ("steer", -1)}.get(k)
            if axis:
                self.press(*axis, now)
        elif TABS[self.tab] == "Ports":
            for port, (fwd, rev) in PORT_KEYS.items():
                if ch in (fwd, rev):
                    self.press(port, 1 if ch == fwd else -1, now)
        elif TABS[self.tab] == "Program":
            if ch == "e":
                self.program = self.edit(self.program)
                self.persist()
            elif k in (10, 13, curses.KEY_ENTER):
                try:
                    steps = parse_program(self.program)
                except ScriptError as exc:
                    self.say(f"Oops: {exc}")
                else:
                    self.say("Running your program!")
                    self.start(run_program(self.module, steps, self.gear, log=self.say))
        elif TABS[self.tab] == "Path":
            step = {"[": ("metres_per_second", -0.05), "]": ("metres_per_second", 0.05),
                    "{": ("degrees_per_second", -5), "}": ("degrees_per_second", 5)}.get(raw)
            if ch == "e":
                self.path = self.edit(self.path)
                self.persist()
            elif step:
                setattr(self.cal, step[0], max(0.05, round(getattr(self.cal, step[0]) + step[1], 2)))
                self.persist()
            elif k in (10, 13, curses.KEY_ENTER):
                try:
                    moves = parse_path(self.path)
                except ScriptError as exc:
                    self.say(f"Oops: {exc}")
                else:
                    self.say("Off we go!")
                    self.start(run_path(self.car, moves, self.cal, log=self.say))

    def expire_keys(self, now: float) -> None:
        for name, h in list(self.held.items()):
            if name == self.mouse_hold:
                continue
            latched = self.lock and name != "steer"  # steering always springs back
            limit = REPEAT_HOLD if h.repeats else FIRST_HOLD
            if not latched and now - h.last > limit:
                del self.held[name]

    async def apply_held(self) -> None:
        """Turn held keys into motor speeds (manual tabs only)."""
        if self.running or TABS[self.tab] not in ("Car", "Ports"):
            return
        self.expire_keys(time.monotonic())
        if TABS[self.tab] == "Car":
            want = {self.car.drive_port: self.active("drive") * self.speed * self.car._drive_sign,
                    self.car.steer_port: self.active("steer") * 255 * self.car._steer_sign}
        else:
            followers = set(self.module.links.values())
            want = {p: self.active(p) * self.speed for p in PORT_KEYS if p not in followers}
        current = self.module.speeds
        if any(current[p] != s for p, s in want.items()):
            await self.module.drive(**want)

    # --- drawing -------------------------------------------------------------
    def put(self, y: int, x: int, text: str, attr: int = 0) -> None:
        h, w = self.scr.getmaxyx()
        if 0 <= y < h and 0 <= x < w - 1:
            try:
                self.scr.addnstr(y, x, text, w - x - 1, attr)
            except curses.error:
                pass

    def hit(self, y: int, x: int, height: int, width: int, action) -> None:
        """Register a clickable region. `action` is a callable, or ("hold", name, direction)."""
        self.hits.append((y, x, height, width, action))

    def button(self, y: int, x: int, label: str, on: bool, width: int = 11, colour: int = GREEN,
               action=None) -> None:
        """A rounded button; filled when pressed."""
        if action:
            self.hit(y, x, 3, width, action)
        inner = label.center(width - 2)
        if on:
            fill = curses.color_pair(ON_GREEN if colour == GREEN else colour) | curses.A_BOLD
            self.put(y, x, "▗" + "▄" * (width - 2) + "▖", curses.color_pair(colour))
            self.put(y + 1, x, " " + inner + " ", fill)
            self.put(y + 2, x, "▝" + "▀" * (width - 2) + "▘", curses.color_pair(colour))
        else:
            c = curses.color_pair(colour)
            self.put(y, x, "╭" + "─" * (width - 2) + "╮", c)
            self.put(y + 1, x, "│", c)
            self.put(y + 1, x + 1, inner, curses.color_pair(WHITE) | curses.A_BOLD)
            self.put(y + 1, x + width - 1, "│", c)
            self.put(y + 2, x, "╰" + "─" * (width - 2) + "╯", c)

    def pill(self, y: int, x: int, text: str, attr: int, action=None) -> int:
        self.put(y, x, f" {text} ", attr)
        if action:
            self.hit(y, x, 1, len(text) + 2, action)
        return x + len(text) + 3

    def draw_header(self, w: int) -> None:
        g = curses.color_pair(GREEN) | curses.A_BOLD
        x = 2
        for colour in (RED, YELLOW, GREEN, BLUE):
            self.put(0, x, "●", curses.color_pair(colour) | curses.A_BOLD)
            x += 2
        self.put(0, x + 1, "BRICK  REMOTE", g)
        x = 26
        for i, (name, icon) in enumerate(zip(TABS, TAB_ICONS)):
            attr = curses.color_pair(ON_GREEN) | curses.A_BOLD if i == self.tab else curses.color_pair(DIM)
            x = self.pill(0, x, f"{icon} {name}", attr, action=lambda i=i: self.select_tab(i))
        # battery + connection, right aligned
        bat = self.module.battery
        level = 0 if bat is None else round(bat / 20)
        colour = GREEN if (bat or 0) > 50 else YELLOW if (bat or 0) > 20 else RED
        text = f"{'●' if self.module.is_connected else '○'} ▕{'█' * level}{'░' * (5 - level)}▏{bat if bat is not None else '?'}%"
        self.put(0, max(x + 1, w - len(text) - 2), text, curses.color_pair(colour) | curses.A_BOLD)

    def draw_gears(self, y: int, x: int) -> None:
        self.put(y, x + 1, "SPEED", curses.color_pair(ORANGE) | curses.A_BOLD)
        for i, g in enumerate((1, 2, 3)):
            on = g == self.gear
            attr = curses.color_pair(ON_GREEN) | curses.A_BOLD if on else curses.color_pair(DIM)
            for r, line in enumerate(BIG_DIGITS[g]):
                self.put(y + 1 + i * 6 + r, x, f" {line} ", attr)
            self.hit(y + 1 + i * 6, x, 5, 6, lambda g=g: self.key(ord(str(g))))
        self.put(y + 19, x, "keys 1 2 3", curses.color_pair(DIM))
        self.put(y + 20, x, "or click!", curses.color_pair(DIM))

    def draw_meter(self, y: int, x: int, label: str, speed: int, width: int = 16) -> None:
        n = round(abs(speed) / 255 * width)
        colour = GREEN if speed >= 0 else ORANGE
        self.put(y, x, f"{label} ", curses.color_pair(WHITE) | curses.A_BOLD)
        self.put(y, x + len(label) + 1, "▕" + "█" * n, curses.color_pair(colour) | curses.A_BOLD)
        self.put(y, x + len(label) + 2 + n, "░" * (width - n) + "▏", curses.color_pair(DIM))

    def draw_car(self, y: int, w: int, h: int) -> None:
        drive, steer = self.active("drive"), self.active("steer")
        # left: forward / back
        self.button(y + 2, 3, "▲", drive > 0, 13, action=("hold", "drive", 1))
        self.put(y + 5, 5, "FORWARD", curses.color_pair(GREEN) | curses.A_BOLD)
        self.button(y + 8, 3, "▼", drive < 0, 13, ORANGE, action=("hold", "drive", -1))
        self.put(y + 11, 6, "BACK", curses.color_pair(ORANGE) | curses.A_BOLD)
        self.draw_meter(y + 14, 3, "MOTOR", self.module.speeds[self.car.drive_port], 8)
        # right: left / right, with the gears just inside them
        rx = w - 30
        self.button(y + 5, rx, "◀", steer > 0, 13, action=("hold", "steer", 1))
        self.button(y + 5, rx + 14, "▶", steer < 0, 13, action=("hold", "steer", -1))
        self.put(y + 8, rx + 3, "LEFT", curses.color_pair(GREEN) | curses.A_BOLD)
        self.put(y + 8, rx + 18, "RIGHT", curses.color_pair(GREEN) | curses.A_BOLD)
        self.put(y + 10, rx, "hold the arrow keys", curses.color_pair(DIM))
        self.put(y + 11, rx, "or press the buttons", curses.color_pair(DIM))
        gx = rx - 10
        self.draw_gears(y - 1, gx)
        # centre: the steering wheel fills the space left over, eased towards the target angle
        left, right = 19, gx - 2
        wh = max(9, h - 9 - y)                  # rows available above the label
        ww = min(right - left, round(wh * 2.2))  # terminal cells are about twice as tall as wide
        wh = min(wh, round(ww / 2.2))
        wh -= 1 - wh % 2                          # odd sizes keep the hub centred
        ww -= 1 - ww % 2
        self.steer_angle += (steer * 0.6 - self.steer_angle) * 0.35
        grid = wheel(round(self.steer_angle, 2), ww, wh)
        wx = left + (right - left - ww) // 2
        colour = curses.color_pair(GREEN) | curses.A_BOLD
        for r, row in enumerate(grid):
            self.put(y + r, wx, "".join("█" if c else " " for c in row), colour)
        label = {1: "◀  LEFT", -1: "RIGHT  ▶", 0: "STRAIGHT"}[steer]
        self.put(y + wh + 1, wx + (ww - len(label)) // 2, label, curses.color_pair(WHITE) | curses.A_BOLD)

    def draw_ports(self, y: int, w: int) -> None:
        half = (w - 24) // 2
        speeds = self.module.speeds
        followers = set(self.module.links.values())
        for i, (vert, horiz) in enumerate((("a", "b"), ("c", "d"))):
            x = 3 + i * (half + 24)
            (vf, vr), (hf, hr) = PORT_KEYS[vert], PORT_KEYS[horiz]
            V, H = vert.upper(), horiz.upper()
            self.button(y, x + 9, f"{V} ▲  {vf}", self.active(vert) > 0, 11, action=("hold", vert, 1))
            self.button(y + 4, x, f"{H} ◀  {hr}", self.active(horiz) < 0, 11, ORANGE, action=("hold", horiz, -1))
            self.button(y + 4, x + 18, f"{H} ▶  {hf}", self.active(horiz) > 0, 11, action=("hold", horiz, 1))
            self.button(y + 8, x + 9, f"{V} ▼  {vr}", self.active(vert) < 0, 11, ORANGE, action=("hold", vert, -1))
            self.put(y + 5, x + 13, "◎", curses.color_pair(GREEN) | curses.A_BOLD)
            for j, p in enumerate((vert, horiz)):
                tag = " (paired)" if p in followers else (" (reversed)" if p in self.module.inverted else "")
                self.draw_meter(y + 12 + j, x, p.upper(), speeds[p], 12)
                self.put(y + 12 + j, x + 17, tag, curses.color_pair(DIM))
        self.draw_gears(y - 2, w // 2 - 4)

    def draw_script(self, y: int, w: int, h: int, title: str, text: str, extra: list[str]) -> None:
        box_w = min(w - 6, 60)
        lines = text.splitlines()
        rows = min(len(lines), h - y - 9)
        c = curses.color_pair(GREEN)
        self.put(y, 3, "╭─ " + title + " " + "─" * (box_w - len(title) - 5) + "╮", c)
        for i in range(rows):
            line = lines[i]
            attr = curses.color_pair(DIM) if line.strip().startswith("#") else curses.color_pair(WHITE) | curses.A_BOLD
            self.put(y + 1 + i, 3, "│", c)
            self.put(y + 1 + i, 5, f"{i + 1:2} ", curses.color_pair(DIM))
            self.put(y + 1 + i, 8, line[:box_w - 7], attr)
            self.put(y + 1 + i, 3 + box_w - 1, "│", c)
        self.put(y + 1 + rows, 3, "╰" + "─" * (box_w - 2) + "╯", c)
        bx = box_w + 6
        if bx + 16 < w:
            self.button(y, bx, "▶ RUN  ⏎", self.running, 14, action=lambda: self.key(10))
            self.button(y + 3, bx, "✎ EDIT  e", False, 14, ORANGE, action=lambda: self.key(ord("e")))
            self.button(y + 6, bx, "■ STOP  ␣", False, 14, RED, action=lambda: self.key(32))
            for i, line in enumerate(extra):
                self.put(y + 10 + i, bx, line, curses.color_pair(WHITE))

    def draw_footer(self, h: int, w: int) -> None:
        for i, line in enumerate(self.log):
            self.put(h - 6 + i, 3, ("» " if i == len(self.log) - 1 else "  ") + line,
                     curses.color_pair(YELLOW) | curses.A_BOLD if i == len(self.log) - 1 else curses.color_pair(DIM))
        y = h - 3
        x = 2
        link = LINKS[self.link]
        status = [("HOLD " + ("ON" if self.lock else "off"), ON_YELLOW if self.lock else DIM),
                  ("PAIR " + ("+".join(link).upper() if link else "off"), ON_YELLOW if link else DIM),
                  ("REVERSED " + (",".join(sorted(self.module.inverted)).upper() or "-"),
                   ON_YELLOW if self.module.inverted else DIM)]
        for (text, colour), k in zip(status, "hpr"):
            x = self.pill(y, x, text, curses.color_pair(colour) | curses.A_BOLD, action=lambda k=k: self.key(ord(k)))
        bar = curses.color_pair(ON_GREEN) | curses.A_BOLD
        self.put(h - 1, 0, " " * (w - 1), bar)
        keys = [("tab", "screen"), ("1-3", "speed"), ("h", "hold"), ("p", "pair"), ("r", "reverse"),
                ("space", "STOP"), ("q", "quit")]
        x = 1
        codes = {"tab": 9, "1-3": None, "space": 32}
        for key, what in keys:
            code = codes.get(key, ord(key[0]))
            if code is not None:
                self.hit(h - 1, x, 1, len(key) + len(what) + 5, lambda c=code: self.key(c))
            self.put(h - 1, x, f" {key} ", curses.color_pair(ON_YELLOW) | curses.A_BOLD)
            x += len(key) + 2
            self.put(h - 1, x, f" {what}  ", bar)
            x += len(what) + 3

    def draw_searching(self, h: int, w: int) -> None:
        """Splash screen while no module is connected."""
        t = time.monotonic()
        brick = ["   ▄▄▄    ▄▄▄    ▄▄▄    ▄▄▄   ",
                 "  █████  █████  █████  █████  ",
                 "▄▄█████▄▄█████▄▄█████▄▄█████▄▄",
                 "██████████████████████████████",
                 "██████████████████████████████",
                 "██████████████████████████████"]
        studs = (RED, YELLOW, GREEN, BLUE)
        y0, x0 = max(1, h // 2 - 8), (w - len(brick[0])) // 2
        hop = int(t * 3) % 4  # one stud bounces at a time
        for r, line in enumerate(brick):
            for i in range(4):
                seg = line[i * 7:i * 7 + 7] if r < 2 else None
                if seg is not None:
                    self.put(y0 + r - (1 if i == hop else 0), x0 + i * 7, seg,
                             curses.color_pair(studs[i]) | curses.A_BOLD)
            if r >= 2:
                self.put(y0 + r, x0, line, curses.color_pair(GREEN) | curses.A_BOLD)
        dots = "." * (int(t * 2) % 4)
        lines = [("Looking for your brick" + dots, YELLOW),
                 ("", WHITE),
                 ("Turn it on with the power button.", WHITE),
                 ("The light blinks while it waits for us.", DIM)]
        if self.connect_error:
            lines.append((self.connect_error, DIM))
        for i, (text, colour) in enumerate(lines):
            self.put(y0 + 8 + i, (w - len(text)) // 2, text, curses.color_pair(colour) | curses.A_BOLD)
        bar = curses.color_pair(ON_GREEN) | curses.A_BOLD
        self.put(h - 1, 0, " " * (w - 1), bar)
        self.put(h - 1, 1, " q ", curses.color_pair(ON_YELLOW) | curses.A_BOLD)
        self.hit(h - 1, 1, 1, 8, lambda: self.key(ord("q")))
        self.put(h - 1, 4, " quit", bar)

    def _lost(self) -> None:
        self.held.clear()
        self.cancel()

    def _connected(self) -> None:
        self.connect_error = ""
        self.say("Connected! Let's go!")

    def _connect_failed(self, exc: Exception) -> None:
        self.connect_error = "" if isinstance(exc, ConnectionError) else f"({exc})"

    def draw(self) -> None:
        self.scr.erase()
        self.hits = []
        h, w = self.scr.getmaxyx()
        if h < 26 or w < 80:
            self.put(0, 0, f"Make the window bigger please! ({w}x{h}, need 80x26)",
                     curses.color_pair(YELLOW) | curses.A_BOLD)
            self.scr.refresh()
            return
        if not self.module.is_connected:
            self.draw_searching(h, w)
            self.scr.refresh()
            return
        self.draw_header(w)
        tab = TABS[self.tab]
        if tab == "Car":
            self.draw_car(2, w, h)
        elif tab == "Ports":
            self.draw_ports(4, w)
        elif tab == "Program":
            self.draw_script(2, w, h, "MY PROGRAM", self.program, [
                "gear 2      a + 1.5", "repeat 3 … end", "wait 1      stop"])
        else:
            self.draw_script(2, w, h, "MY PATH", self.path, [
                f"speed {self.cal.metres_per_second:.2f} m/s  [ ]",
                f"turn  {self.cal.degrees_per_second:.0f} °/s    {{ }}",
                "forward 1   left 90"])
        self.draw_footer(h, w)
        self.scr.refresh()

    async def run(self) -> None:
        self.scr.nodelay(True)
        self.scr.keypad(True)
        curses.mousemask(curses.ALL_MOUSE_EVENTS)
        curses.mouseinterval(0)  # report press and release separately, without click delay
        curses.curs_set(0)
        init_colours()
        connector = asyncio.create_task(self.module.keep_connected(
            on_connected=self._connected, on_lost=self._lost, on_error=self._connect_failed))
        try:
            while not self.quit:
                while (k := self.scr.getch()) != -1:
                    if k == curses.KEY_MOUSE:
                        self.mouse()
                    elif self.module.is_connected or k in (ord("q"), ord("Q")):
                        self.key(k)
                if self.module.is_connected:
                    try:
                        await self.apply_held()
                    except Exception as exc:
                        self.say(f"Oops, send failed: {exc}")
                self.draw()
                await asyncio.sleep(0.03)
        finally:
            self.cancel()
            connector.cancel()
            await self.module.close()

async def _main(scr, address, drive_port, steer_port) -> None:
    module = JMCModule(address, timeout=5.0)
    await App(scr, module, Car(module, drive_port=drive_port, steer_port=steer_port)).run()


def run(address: str | None = None, drive_port: str = "a", steer_port: str = "d") -> None:
    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(lambda scr: asyncio.run(_main(scr, address, drive_port, steer_port)))
