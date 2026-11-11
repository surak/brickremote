# brickremote

Python library for the cheap Lego-compatible Bluetooth LE motor bricks that advertise as
`JG_JMC-xxxx` and are normally driven by the **XP Block** phone app ("Smart creative module",
four motor ports A–D). Works anywhere [bleak](https://github.com/hbldh/bleak) does: Linux
(incl. Raspberry Pi), macOS and Windows.

```
pip install brickremote
```

Or from a checkout, without installing anything yourself:

```
./run.sh          # creates .venv on first run, then starts the remote control app
./run.sh scan     # any other command works the same way
```

## Remote control app

Run `brickremote` with no arguments for a terminal version of the phone app (everything except the
phone-only gyroscope and voice modes):

| Tab | Keys |
|-----|------|
| Car | arrows: forward/back, steer left/right (drive port A, steering port D; change with `--drive`/`--steer`) |
| Ports | like two game sticks: `W`/`S` port A, `A`/`D` port B, `I`/`K` port C, `J`/`L` port D |
| Program | `e` edit in `$EDITOR`, Enter run (see below) |
| Path | `e` edit, Enter run, `[` `]` / `{` `}` calibrate speed and turn rate |

Everywhere: `Tab` switch screen, `1`-`3` speed (60/120/255), `space` STOP, `q` quit, and

- `h` **hold**: motors keep running after you let go of the key (steering still springs back)
- `p` **pair**: one port copies another (off → A+B → A+C), for builds with two motors that
  should always move together, like a tank
- `r` + port letter **reverse**: that port turns the other way round, for a motor that was
  mounted backwards

Settings, programs and paths are saved in `~/.config/brickremote.json`.

Terminals report key presses but not releases, so a motor runs while its key auto-repeats and
stops a moment after you let go. Steering always springs back to centre, even with hold on.

## Web remote control

```
./run.sh web          # or: brickremote web   (needs `pip install 'brickremote[web]'`)
```

This starts a small web server and prints its address. Open it in any browser: on the same
computer, or on a tablet or laptop on the same Wi-Fi, which then becomes the remote control.
It has the same four screens as the terminal app, plus things a browser is good at:

- buttons you hold down, a steering wheel you can drag, and two joysticks for the ports
- program blocks you tap to build a program
- a grid where you draw the route for path mode

If the browser stops talking to the server (tab closed, Wi-Fi gone), the motors stop by themselves.

## Command line

```
brickremote scan                         # list modules nearby
brickremote run a=255 --time 2           # port A forward, full speed, 2 s
brickremote run a=-120 d=255 --time 1.5  # A reverse at medium speed, D forward
brickremote program dance.txt            # run a program script
brickremote path route.txt --mps 0.6     # drive a path script
brickremote --invert d run d=255         # global options: --address, --slot, --invert, --drive, --steer
```

### Program scripts (the app's block programming)

```
gear 2              # speed for + and -: 1 = 60, 2 = 120, 3 = 255
repeat 3
  a + 1.5           # port A forward for 1.5 s
  ac - 1            # ports A and C reverse for 1 s
  wait 0.5
end
d 200               # port D at speed 200, keep running
forever             # until Ctrl-C / space
  b + 1
  b - 1
end
```

### Path scripts (the app's path mode)

```
forward 1.5   # metres
left 90       # degrees, driving forward with full steering
back 0.5
right 45
```

Distances and angles become times through a calibration (`--mps`, `--dps`, or `[ ] { }` in the
app): measure how far the car goes in one second at full speed, and how many degrees it turns.

## Library

```python
import asyncio
from brickremote import JMCModule

async def main():
    async with JMCModule() as m:          # connects to the first module found
        await m.drive(a=255, d=-120)      # speeds -255..255, negative = reverse
        await asyncio.sleep(2)
        await m.stop()
        print("battery:", m.battery, "%")

asyncio.run(main())
```

`JMCModule(address)` targets a specific module, `discover()` lists all of them. While connected,
the current state is resent every 0.2 s, which keeps the module from going to sleep.

```python
JMCModule(inverted="d", links={"a": "c"})   # reverse port D; C follows A
await m.run(2, a=255)                       # A forward for 2 s, then stop
```

`brickremote.script` has the program and path engines (`parse_program`, `run_program`,
`parse_path`, `run_path`) if you want them from your own code.

For the stock car (drive motor on A, steering on D) there is a small wrapper:

```python
from brickremote import Car, JMCModule

async with JMCModule() as m:
    car = Car(m)
    await car.left(speed=200)   # forward while steering left
```

## Protocol

Reverse-engineered from the XP Block Android app. The module exposes service `FFF0`;
commands go to `FFF2` and **must be written with response** (write-without-response is
silently ignored):

```
5A 6B 02 00 05  dirAB speedAB  dirCD speedCD  slot  SUM
```

| Field   | Meaning |
|---------|---------|
| dirAB   | A: `1` forward, `2` reverse; B: `+4` forward, `+8` reverse |
| speedAB | 0–255, shared by A and B (the app's gears are 60 / 120 / 255) |
| dirCD, speedCD | same for C and D |
| slot    | 1–3, module slot (the app can drive three modules at once) |
| SUM     | sum of all previous bytes, modulo 256 |

Six-port modules use length `08` and append `dirEF speedE speedF`.

The module reports on `FFF1` with `5A 6B 01 00 01 <battery %> SUM`. It switches itself off
after about ten minutes without commands.

## Limitations

- The two ports of a pair share one speed byte, so A and B (or C and D) cannot run at different
  speeds at the same time; the faster one wins.
- Only tested with a four-port module. Six-port support follows the app's code but is unverified.

## License

MIT
