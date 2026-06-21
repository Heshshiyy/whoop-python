"""Schema-driven frame parser.

Routes by packet type and decodes records to fully-populated ParsedFrame subtypes.
"""

from __future__ import annotations

import struct
from typing import cast

from whoop.protocol.device_family import DeviceFamily
from whoop.protocol.packet_types import PacketTypes
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


def _read_u16le(data: bytes, offset: int) -> int:
    """Read a little-endian uint16 with bounds-safe access."""
    if offset + 1 < len(data):
        return struct.unpack_from("<H", data, offset)[0]
    return 0


def _read_u32le(data: bytes, offset: int) -> int:
    """Read a little-endian uint32 with bounds-safe access."""
    if offset + 3 < len(data):
        return struct.unpack_from("<I", data, offset)[0]
    return 0


def _read_u64le(data: bytes, offset: int) -> int:
    """Read a little-endian uint64 with bounds-safe access."""
    if offset + 7 < len(data):
        return struct.unpack_from("<Q", data, offset)[0]
    return 0


def _read_f32le(data: bytes, offset: int) -> float:
    """Read a little-endian float32 with bounds-safe access."""
    if offset + 3 < len(data):
        bits = _read_u32le(data, offset)
        return struct.unpack("<f", struct.pack("<I", bits))[0]
    return 0.0


# ===========================================================================
# Public API
# ===========================================================================


def parse_frame(inner: bytes, family: DeviceFamily) -> ParsedFrame | None:
    """Parse a validated inner record into a fully populated ParsedFrame.

    The inner record has the format: [type][seq][cmd][payload...]

    Args:
        inner: CRC-validated inner record bytes.
        family: Device family.

    Returns:
        A fully populated ParsedFrame, or None if parsing fails.
    """
    if len(inner) < 3:
        return None

    ptype = inner[0] & 0xFF
    seq = inner[1] & 0xFF
    cmd = inner[2] & 0xFF

    packet_type = PacketTypes.from_code(ptype)
    if packet_type is None:
        return None

    frame = _from_packet_type(packet_type, inner, family, seq, cmd)
    if frame is None:
        return None

    frame.crc_ok = True

    if isinstance(frame, HistoricalDataFrame):
        return _parse_historical(inner, family, frame)

    return frame


# ===========================================================================
# Packet type dispatch
# ===========================================================================


def _from_packet_type(
    packet_type: PacketTypes,
    inner: bytes,
    family: DeviceFamily,
    seq: int,
    cmd: int,
) -> ParsedFrame | None:
    """Create the appropriate ParsedFrame subtype from a validated inner record."""
    match packet_type:
        case PacketTypes.HISTORICAL_DATA:
            return HistoricalDataFrame(
                family=family, seq=seq, cmd=cmd, record_version=seq
            )
        case PacketTypes.REALTIME_DATA:
            return _parse_realtime(inner, family, seq)
        case PacketTypes.REALTIME_RAW_DATA:
            return _parse_realtime_raw(inner, family, seq, cmd)
        case PacketTypes.EVENT:
            return _parse_event(inner, family, seq, cmd)
        case PacketTypes.METADATA:
            return _parse_metadata(inner, family, seq)
        case PacketTypes.COMMAND_RESPONSE | PacketTypes.PUFFIN_COMMAND_RESPONSE:
            return _parse_command_response(inner, family, seq, cmd)
        case PacketTypes.CONSOLE_LOGS:
            return ConsoleLogs(family=family, seq=seq, cmd=cmd, payload=inner[3:])
        case _:
            return None


# ===========================================================================
# Historical parsing
# ===========================================================================


def _parse_historical(
    inner: bytes, family: DeviceFamily, template: HistoricalDataFrame
) -> HistoricalDataFrame:
    """Parse historical records based on version and family."""
    version = template.seq  # Version stored in seq byte for historical
    records: list[HistoricalRecord] = []

    if family.is_whoop4 or version in (24, 12, 5, 7, 9):
        # WHOOP 4.0: version 24 (84+ byte records)
        if len(inner) >= 84:
            records.append(_parse_v24_record(inner))
    elif version == 18:
        # WHOOP 5.0 version 18 (124 byte records) — UNVALIDATED
        if len(inner) >= 124:
            records.append(_parse_v18_record(inner))
    elif version == 26:
        # WHOOP 5.0 version 26 (88 byte high-rate PPG records)
        if len(inner) >= 88:
            records.append(
                HistoricalRecord(
                    unix=_read_u32le(inner, 15),
                    heart_rate=0,
                    signal_quality=0,
                )
            )
    else:
        # Generic: try to parse at least HR + RR + timestamp
        if len(inner) >= 25:
            hr = (inner[21] if len(inner) > 21 else 0) & 0xFF
            rr_count = (inner[22] if len(inner) > 22 else 0) & 0xFF
            rr: list[int] = []
            if rr_count > 0 and len(inner) >= 23 + rr_count * 2:
                for i in range(rr_count):
                    rr.append(_read_u16le(inner, 23 + i * 2))
            records.append(
                HistoricalRecord(
                    unix=_read_u32le(inner, 11),
                    heart_rate=hr,
                    rr_count=rr_count,
                    rr_intervals=rr,
                )
            )

    template.records = records
    template.record_version = version
    return template


def _parse_v24_record(inner: bytes) -> HistoricalRecord:
    """Parse WHOOP 4.0 version 24 historical record (84+ bytes)."""
    unix = _read_u32le(inner, 11)
    hr = (inner[21] if len(inner) > 21 else 0) & 0xFF
    rr_count = (inner[22] if len(inner) > 22 else 0) & 0xFF
    rr: list[int] = []
    if rr_count > 0 and len(inner) >= 23 + rr_count * 2:
        for i in range(rr_count):
            rr.append(_read_u16le(inner, 23 + i * 2))

    return HistoricalRecord(
        unix=unix,
        heart_rate=hr,
        rr_count=rr_count,
        rr_intervals=rr,
        ppg_green=_read_u16le(inner, 33),
        ppg_red_ir=_read_u16le(inner, 35),
        gravity_x=_read_f32le(inner, 40),
        gravity_y=_read_f32le(inner, 44),
        gravity_z=_read_f32le(inner, 48),
        skin_contact=(inner[55] if len(inner) > 55 else 0) & 0xFF,
        spo2_red=_read_u16le(inner, 68),
        spo2_ir=_read_u16le(inner, 70),
        skin_temp_raw=_read_u16le(inner, 72),
        resp_rate_raw=_read_u16le(inner, 80),
        signal_quality=_read_u16le(inner, 82),
    )


def _parse_v18_record(inner: bytes) -> HistoricalRecord:
    """Parse WHOOP 5.0 version 18 historical record (124 bytes). UNVALIDATED."""
    unix = _read_u32le(inner, 15)
    hr = (inner[22] if len(inner) > 22 else 0) & 0xFF
    rr_count = (inner[23] if len(inner) > 23 else 0) & 0xFF
    rr: list[int] = []
    if rr_count > 0 and len(inner) >= 24 + rr_count * 2:
        for i in range(rr_count):
            rr.append(_read_u16le(inner, 24 + i * 2))

    return HistoricalRecord(
        unix=unix,
        heart_rate=hr,
        rr_count=rr_count,
        rr_intervals=rr,
        gravity_x=_read_f32le(inner, 45),
        gravity_y=_read_f32le(inner, 49),
        gravity_z=_read_f32le(inner, 53),
        skin_temp_raw=_read_u16le(inner, 73),
    )


# ===========================================================================
# Other frame parsers
# ===========================================================================


def _parse_realtime(inner: bytes, family: DeviceFamily, seq: int) -> RealtimeData:
    """Parse type-40 realtime data frame.

    WHOOP 4 type-40 inner record layout (20 bytes observed):
      [0]    type  = 0x28 = 40
      [1]    sub   = 0x02 (constant sub-type / version)
      [2:6]  ts    unix timestamp u32 LE  (LSB occupies the "cmd" slot at [2])
      [6:8]  sub-s sub-second counter u16 LE
      [8]    hr    heart rate uint8 bpm
      [9]    rr_n  number of RR intervals that follow
      [10:]  rr    rr_n × u16 LE values in milliseconds
    """
    ts = _read_u32le(inner, 2)   # was 6 — timestamp LSB sits in the "cmd" byte slot
    sub = _read_u16le(inner, 6)  # was 10
    hr = (inner[8] if len(inner) > 8 else 0) & 0xFF     # was 12
    rr_count = (inner[9] if len(inner) > 9 else 0) & 0xFF   # was 13
    rr: list[int] = []
    if rr_count > 0 and len(inner) >= 10 + rr_count * 2:    # was 14
        for i in range(rr_count):
            rr.append(_read_u16le(inner, 10 + i * 2))        # was 14
    return RealtimeData(
        family=family, seq=seq, timestamp=ts, subseconds=sub,
        heart_rate=hr, rr_count=rr_count, rr_intervals=rr,
    )


def _parse_realtime_raw(
    inner: bytes, family: DeviceFamily, seq: int, cmd: int
) -> RealtimeRawData:
    """Parse type-43 raw data frame."""
    rec_header = _read_u16le(inner, 3)
    ts = _read_u32le(inner, 6)
    sub = _read_u16le(inner, 10)
    raw = inner[12:] if len(inner) > 12 else b""
    return RealtimeRawData(
        family=family, seq=seq, cmd=cmd,
        record_header=rec_header, timestamp=ts, subseconds=sub,
        raw_payload=raw,
    )


def _parse_event(
    inner: bytes, family: DeviceFamily, seq: int, cmd: int
) -> Event:
    """Parse type-48 event frame."""
    ev = (inner[6] if len(inner) > 6 else 0) & 0xFF
    ev_ts = _read_u32le(inner, 8)
    try:
        event_kind = EventKind(ev)
    except ValueError:
        event_kind = EventKind.UNKNOWN
    return Event(
        family=family, seq=seq, cmd=cmd,
        event_kind=event_kind, event_timestamp=ev_ts,
    )


def _parse_metadata(inner: bytes, family: DeviceFamily, seq: int) -> Metadata:
    """Parse type-49 metadata frame."""
    mt = (inner[6] if len(inner) > 6 else 0) & 0xFF
    try:
        metadata_kind = MetadataKind(mt)
    except ValueError:
        metadata_kind = MetadataKind.UNKNOWN
    return Metadata(
        family=family, seq=seq, metadata_kind=metadata_kind,
    )


def _parse_command_response(
    inner: bytes, family: DeviceFamily, seq: int, cmd: int
) -> CommandResponse:
    """Parse type-36/38 command response frame."""
    resp_cmd = (inner[6] if len(inner) > 6 else 0) & 0xFF
    resp_payload = inner[7:] if len(inner) > 7 else b""
    return CommandResponse(
        family=family, seq=seq, cmd=cmd,
        response_code=resp_cmd, response_payload=resp_payload,
    )
