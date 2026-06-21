"""Handshake procedures per DeviceFamily.

Platform-pure: produces the sequence of command frames to send.
Actual IO (BLE writes, waiting for responses) is handled by the BLE layer.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from whoop.protocol.commands import Command
from whoop.protocol.device_family import DeviceFamily


@dataclass
class HandshakeStep:
    """A single step in the device handshake sequence."""

    command: int  # Command code (-1 for special CLIENT_HELLO)
    payload: bytes
    description: str


@dataclass
class HandshakeResult:
    """Result of a completed handshake."""

    device_info: str | None = None
    strap_rtc: int | None = None
    clock_ref_local: float | None = None


# ===========================================================================
# Handshake sequences
# ===========================================================================


def whoop4_sequence(
    current_unix_seconds: int, current_sub_seconds: int = 0
) -> list[HandshakeStep]:
    """Generate the WHOOP 4.0 handshake command sequence.

    Steps: GET_HELLO → GET_ADVERTISING_NAME → SET_CLOCK → GET_CLOCK →
           STOP_RAW_FLOOD → GET_DATA_RANGE
    """
    return [
        HandshakeStep(
            Command.GET_HELLO_HARVARD.value,
            b"",
            "GET_HELLO_HARVARD(35)",
        ),
        HandshakeStep(
            Command.GET_ADVERTISING_NAME_HARVARD.value,
            b"",
            "GET_ADVERTISING_NAME_HARVARD(76)",
        ),
        HandshakeStep(
            Command.SET_CLOCK.value,
            build_clock_payload(current_unix_seconds, current_sub_seconds),
            "SET_CLOCK(10)",
        ),
        HandshakeStep(
            Command.GET_CLOCK.value,
            b"",
            "GET_CLOCK(11) — empty payload",
        ),
        HandshakeStep(
            Command.SEND_R10_R11_REALTIME.value,
            b"\x00",
            "SEND_R10_R11_REALTIME(63) — STOP",
        ),
        HandshakeStep(
            Command.GET_DATA_RANGE.value,
            b"",
            "GET_DATA_RANGE(34)",
        ),
    ]


def whoop5_sequence(
    current_unix_seconds: int, current_sub_seconds: int = 0
) -> list[HandshakeStep]:
    """Generate the WHOOP 5.0 handshake command sequence.

    Steps: CLIENT_HELLO → then same commands as 4.0.
    UNVALIDATED — no 5.0 hardware.
    """
    whoop5 = DeviceFamily.WHOOP_5()
    client_hello = whoop5.client_hello if whoop5.client_hello else DeviceFamily.CLIENT_HELLO

    steps = [
        HandshakeStep(-1, client_hello, "CLIENT_HELLO (static frame)"),
    ]
    steps.extend(whoop4_sequence(current_unix_seconds, current_sub_seconds))
    return steps


def sequence_for(
    family: DeviceFamily,
    current_unix_seconds: int | None = None,
    current_sub_seconds: int = 0,
) -> list[HandshakeStep]:
    """Get the handshake sequence for a given device family."""
    import time

    unix = current_unix_seconds if current_unix_seconds is not None else int(time.time())
    if family.is_whoop4:
        return whoop4_sequence(unix, current_sub_seconds)
    return whoop5_sequence(unix, current_sub_seconds)


# ===========================================================================
# Payload builders
# ===========================================================================


def build_clock_payload(unix_seconds: int, sub_seconds: int = 0) -> bytes:
    """Build the SET_CLOCK payload: [unixSeconds u32 LE][subSeconds u32 LE] (8 bytes)."""
    return struct.pack("<II", unix_seconds, sub_seconds)


def build_ack_payload(end_timestamp: int) -> bytes:
    """Build a HISTORICAL_DATA_RESULT ack payload: [0x01] + [endTimestamp u64 LE] (9 bytes)."""
    return b"\x01" + struct.pack("<Q", end_timestamp)
