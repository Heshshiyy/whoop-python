"""WHOOP Protocol — zero-dependency Python implementation.

CRC, framing, parsing, commands, and handshake for WHOOP 4.0 and WHOOP 5.0.
"""

from whoop.protocol.crc import crc8, crc32, crc16_modbus
from whoop.protocol.device_family import DeviceFamily, DeviceFamilyKind
from whoop.protocol.packet_types import PacketTypes
from whoop.protocol.commands import Command
from whoop.protocol.frames import FrameEncoder, FrameReassembler
from whoop.protocol.parsed_frame import (
    CommandResponse,
    ConsoleLogs,
    Event,
    EventKind,
    HistoricalDataFrame,
    HistoricalRecord,
    Metadata,
    MetadataKind,
    ParsedFrame,
    RealtimeData,
    RealtimeRawData,
)
from whoop.protocol.parse_frame import parse_frame
from whoop.protocol.handshake import (
    HandshakeStep,
    HandshakeResult,
    whoop4_sequence,
    whoop5_sequence,
    sequence_for,
    build_clock_payload,
    build_ack_payload,
)

__all__ = [
    # CRC
    "crc8",
    "crc32",
    "crc16_modbus",
    # Device family
    "DeviceFamily",
    "DeviceFamilyKind",
    # Packet types
    "PacketTypes",
    # Commands
    "Command",
    # Frames
    "FrameEncoder",
    "FrameReassembler",
    # Parsed frames
    "ParsedFrame",
    "HistoricalDataFrame",
    "HistoricalRecord",
    "RealtimeData",
    "RealtimeRawData",
    "Event",
    "EventKind",
    "Metadata",
    "MetadataKind",
    "CommandResponse",
    "ConsoleLogs",
    # Parsing
    "parse_frame",
    # Handshake
    "HandshakeStep",
    "HandshakeResult",
    "whoop4_sequence",
    "whoop5_sequence",
    "sequence_for",
    "build_clock_payload",
    "build_ack_payload",
]
