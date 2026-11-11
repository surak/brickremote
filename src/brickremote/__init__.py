"""Control JG_JMC-xxxx Bluetooth LE motor modules (Lego-compatible, "XP Block" app)."""

from .car import Car
from .module import JMCModule, discover, find
from .protocol import GEARS, Status, motor_frame, parse_status, stop_frame

__all__ = ["GEARS", "Car", "JMCModule", "Status", "discover", "find", "motor_frame", "parse_status", "stop_frame"]
__version__ = "0.3.0"
