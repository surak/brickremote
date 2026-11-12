# Changelog

## 0.4.0

- Website in English, German, Brazilian Portuguese and Slovak, with a demo video.
- The README animation now also shows on PyPI.
- Code style checked with ruff in CI.
- `JMCModule.keep_connected()` and `JMCModule.close()`: the reconnect loop and the instant quit used
  by both apps now live in the library.
- Settings handling moved to `brickremote.config`.
- Tests for the module (with a fake Bluetooth client), the web server and the terminal app's controls.
- Python 3.10 or newer is required.

## 0.3.0

- Renamed from `jmcble` to `brickremote` (package, command and config file; old settings are picked up).
- Remote control app: kid-friendly look, big steering wheel, mouse support, searching screen with
  automatic reconnect, instant quit with `q`.
- Steering springs back as soon as the key is released; hold (`h`), pair (`p`) and reverse (`r`).
- `scan` prints modules as soon as they are seen and stops early.
- `JMCModule.detach()` for exiting without waiting for BlueZ to disconnect.

## 0.2.0

- Program and path scripts (`brickremote program`, `brickremote path`), port inversion and linking,
  `JMCModule.run()`, first version of the terminal remote control.

## 0.1.0

- First release: protocol, `JMCModule`, `Car`, `scan` and `run` commands.
