"""Packet type codes from the WHOOP protocol."""

from __future__ import annotations

from enum import IntEnum


class PacketTypes(IntEnum):
    """Packet type codes matching the whoop_protocol.json schema."""

    COMMAND = 35
    COMMAND_RESPONSE = 36
    PUFFIN_COMMAND = 37
    PUFFIN_COMMAND_RESPONSE = 38
    REALTIME_DATA = 40
    REALTIME_RAW_DATA = 43
    HISTORICAL_DATA = 47
    EVENT = 48
    METADATA = 49
    CONSOLE_LOGS = 50

    @classmethod
    def from_code(cls, code: int) -> PacketTypes | None:
        """Resolve a packet type from its numeric code. Returns None if unknown."""
        try:
            return cls(code)
        except ValueError:
            return None

    def to_display_string(self) -> str:
        """Return a human-readable string with name and code number."""
        return f"{self.name}({self.value})"
