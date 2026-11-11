"""Settings shared by the terminal and web remote controls (~/.config/brickremote.json)."""

from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "brickremote.json"

EXAMPLE_PROGRAM = """# Program mode - see `brickremote program --help` for all commands
gear 2
repeat 2
  a + 1.5
  wait 0.5
  a - 1.5
end
stop
"""

EXAMPLE_PATH = """# Path mode - metres and degrees
forward 1
left 90
forward 0.5
right 90
back 0.5
"""


def load_config(path: Path | None = None) -> dict:
    path = path or CONFIG
    for candidate in (path, path.with_name("jmcble.json")):  # the package was called jmcble before 0.3
        try:
            return json.loads(candidate.read_text())
        except (OSError, ValueError):
            continue
    return {}


def save_config(cfg: dict, path: Path | None = None) -> None:
    path = path or CONFIG
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cfg, indent=2))
    except OSError:
        pass
