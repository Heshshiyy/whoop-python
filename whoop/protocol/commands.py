"""Safe WHOOP command codes.

Destructive commands (FORCE_TRIM=25, REBOOT_STRAP=29, POWER_CYCLE=32, firmware-write,
SET_* config, etc.) are intentionally EXCLUDED.
"""

from __future__ import annotations

from enum import IntEnum
from whoop.protocol.device_family import DeviceFamily
from whoop.protocol.frames import FrameEncoder


class Command(IntEnum):
    """Safe subset of WHOOP command codes."""

    TOGGLE_REALTIME_HR = 3
    SET_CLOCK = 10
    GET_CLOCK = 11
    SEND_HISTORICAL_DATA = 22
    HISTORICAL_DATA_RESULT = 23
    GET_BATTERY_LEVEL = 26
    GET_DATA_RANGE = 34
    GET_HELLO_HARVARD = 35
    SEND_R10_R11_REALTIME = 63
    GET_ADVERTISING_NAME_HARVARD = 76
    RUN_HAPTICS_PATTERN = 79

    def build_frame(self, seq: int, payload: bytes, family: DeviceFamily) -> bytes:
        """Build a complete wire frame for this command.

        Args:
            seq: Sequence number (0-255).
            payload: Command payload bytes.
            family: Device family determining envelope format and CRC scheme.

        Returns:
            Complete wire frame with SOF, length, CRC headers, and CRC32 trailer.
        """
        return FrameEncoder.build_envelope(
            payload=payload,
            packet_type=35,  # PacketTypes.COMMAND
            seq=seq,
            cmd=self.value,
            family=family,
        )

    @classmethod
    def is_safe(cls, code: int) -> bool:
        """Check if a command code is in our safe subset."""
        try:
            cls(code)
            return True
        except ValueError:
            return False
