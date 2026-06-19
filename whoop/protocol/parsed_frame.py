"""Parsed frame data classes for the WHOOP protocol."""

from __future__ import annotations

from dataclasses import dataclass, field

from whoop.protocol.device_family import DeviceFamily
from whoop.protocol.packet_types import PacketTypes
from enum import IntEnum


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class EventKind(IntEnum):
    UNKNOWN = 0


class MetadataKind(IntEnum):
    UNKNOWN = 0
    HISTORY_START = 1
    HISTORY_END = 2
    HISTORY_COMPLETE = 3


# ---------------------------------------------------------------------------
# Historical record
# ---------------------------------------------------------------------------


@dataclass
class HistoricalRecord:
    """A single historical record extracted from type-47 data."""

    unix: int = 0
    heart_rate: int = 0
    rr_count: int = 0
    rr_intervals: list[int] = field(default_factory=list)
    ppg_green: int = 0
    ppg_red_ir: int = 0
    gravity_x: float = 0.0
    gravity_y: float = 0.0
    gravity_z: float = 0.0
    skin_contact: int = 0
    spo2_red: int = 0
    spo2_ir: int = 0
    skin_temp_raw: int = 0
    resp_rate_raw: int = 0
    signal_quality: int = 0


# ---------------------------------------------------------------------------
# ParsedFrame base
# ---------------------------------------------------------------------------


@dataclass
class ParsedFrame:
    """Abstract base for all parsed frame types."""

    family: DeviceFamily | None = None
    packet_type: PacketTypes | None = None
    seq: int = 0
    cmd: int = 0
    payload: bytes = field(default_factory=bytes)
    crc_ok: bool = True


# ---------------------------------------------------------------------------
# Concrete frame types
# ---------------------------------------------------------------------------


@dataclass
class HistoricalDataFrame(ParsedFrame):
    """Type-47 historical data frame — the authoritative metric source."""

    record_version: int = 0
    records: list[HistoricalRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.packet_type = PacketTypes.HISTORICAL_DATA


@dataclass
class RealtimeData(ParsedFrame):
    """Type-40 realtime data (display-only convenience)."""

    timestamp: int = 0
    subseconds: int = 0
    heart_rate: int = 0
    rr_count: int = 0
    rr_intervals: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.packet_type = PacketTypes.REALTIME_DATA


@dataclass
class RealtimeRawData(ParsedFrame):
    """Type-43 raw data (IMU/optical flood)."""

    record_header: int = 0
    timestamp: int = 0
    subseconds: int = 0
    raw_payload: bytes = field(default_factory=bytes)

    def __post_init__(self) -> None:
        self.packet_type = PacketTypes.REALTIME_RAW_DATA


@dataclass
class Event(ParsedFrame):
    """Type-48 event frame."""

    event_kind: EventKind = EventKind.UNKNOWN
    event_timestamp: int = 0

    def __post_init__(self) -> None:
        self.packet_type = PacketTypes.EVENT


@dataclass
class Metadata(ParsedFrame):
    """Type-49 metadata frame (offload flow control)."""

    metadata_kind: MetadataKind = MetadataKind.UNKNOWN

    def __post_init__(self) -> None:
        self.packet_type = PacketTypes.METADATA

    @property
    def is_history_start(self) -> bool:
        return self.metadata_kind == MetadataKind.HISTORY_START

    @property
    def is_history_end(self) -> bool:
        return self.metadata_kind == MetadataKind.HISTORY_END

    @property
    def is_history_complete(self) -> bool:
        return self.metadata_kind == MetadataKind.HISTORY_COMPLETE


@dataclass
class CommandResponse(ParsedFrame):
    """Type-36/38 command response frame."""

    response_code: int = 0
    response_payload: bytes = field(default_factory=bytes)

    def __post_init__(self) -> None:
        self.packet_type = PacketTypes.COMMAND_RESPONSE


@dataclass
class ConsoleLogs(ParsedFrame):
    """Type-50 console logs frame."""

    def __post_init__(self) -> None:
        self.packet_type = PacketTypes.CONSOLE_LOGS
