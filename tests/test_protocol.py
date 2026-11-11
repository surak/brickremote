import pytest

from brickremote.protocol import motor_frame, parse_status, stop_frame


def test_port_a_forward_matches_captured_frame():
    assert motor_frame(a=255).hex(" ") == "5a 6b 02 00 05 01 ff 00 00 01 cd"


@pytest.mark.parametrize("speeds, expected", [
    ({"a": -255}, "5a 6b 02 00 05 02 ff 00 00 01 ce"),
    ({"b": 255}, "5a 6b 02 00 05 04 ff 00 00 01 d0"),
    ({"c": 255}, "5a 6b 02 00 05 00 00 01 ff 01 cd"),
    ({"d": -255}, "5a 6b 02 00 05 00 00 08 ff 01 d4"),
    ({"a": 120, "d": 255}, "5a 6b 02 00 05 01 78 04 ff 01 49"),
])
def test_directions(speeds, expected):
    assert motor_frame(**speeds).hex(" ") == expected


def test_stop():
    assert stop_frame().hex(" ") == "5a 6b 02 00 05 00 00 00 00 01 cd"


def test_speed_is_clamped():
    assert motor_frame(a=1000) == motor_frame(a=255)


def test_six_ports():
    frame = motor_frame(e=60, f=-120, six_ports=True)
    assert frame[4] == 8 and list(frame[10:13]) == [1 | 8, 60, 120]
    assert sum(frame[:-1]) & 0xFF == frame[-1]


def test_invalid_arguments():
    with pytest.raises(ValueError):
        motor_frame(module=4)
    with pytest.raises(ValueError):
        motor_frame(e=10)


def test_parse_status():
    assert parse_status(bytes.fromhex("5a6b010001642b")).battery == 100
    assert parse_status(bytes.fromhex("5a6b010001642c")) is None
    assert parse_status(b"\x00") is None
