import asyncio

import pytest

from brickremote import script
from brickremote.script import ScriptError, parse_path, parse_program


class FakeModule:
    def __init__(self):
        self.calls = []

    async def drive(self, **speeds):
        self.calls.append(("drive", speeds))

    async def run(self, seconds, **speeds):
        self.calls.append(("run", seconds, speeds))

    async def stop(self):
        self.calls.append(("stop",))


def test_parse_program_blocks():
    steps = parse_program("""
        gear 2
        repeat 2   # comment
          ac + 1
          wait 0.5
        end
        d -120
        stop
    """)
    assert [s.op for s in steps] == ["gear", "repeat", "drive", "stop"]
    assert steps[1].count == 2 and [s.op for s in steps[1].body] == ["drive", "wait"]
    assert steps[2].speed == -120 and steps[2].seconds is None


@pytest.mark.parametrize("bad", ["x + 1", "repeat 2\na + 1", "end", "gear 5", "a", "wait"])
def test_parse_program_errors(bad):
    with pytest.raises(ScriptError):
        parse_program(bad)


def test_run_program_uses_gear_speeds(monkeypatch):
    module = FakeModule()
    monkeypatch.setattr(asyncio, "sleep", lambda s: asyncio.sleep(0) if s else None)
    steps = parse_program("gear 1\nrepeat 2\na + 1\nend\ngear 3\nb -\nstop")
    asyncio.run(script.run_program(module, steps, log=lambda *_: None))
    assert module.calls == [
        ("run", 1.0, {"a": 60}), ("run", 1.0, {"a": 60}),
        ("drive", {"b": -255}), ("stop",), ("stop",),
    ]


def test_parse_path():
    assert parse_path("forward 1\nleft 90 # turn\nbwd 0.5") == [("forward", 1.0), ("left", 90.0), ("back", 0.5)]
    with pytest.raises(ScriptError):
        parse_path("jump 3")
