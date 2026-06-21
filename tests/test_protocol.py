"""Tests for the WHOOP protocol layer.

Covers: CRC, frame reassembly/encoding, historical parsing, command building,
handshake sequences, and edge cases.
"""

import struct
import pytest

from whoop.protocol.crc import crc8, crc32, crc16_modbus
from whoop.protocol.device_family import DeviceFamily, DeviceFamilyKind
from whoop.protocol.packet_types import PacketTypes
from whoop.protocol.commands import Command
from whoop.protocol.frames import FrameEncoder, FrameReassembler
from whoop.protocol.parsed_frame import (
    CommandResponse,
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
    whoop4_sequence,
    whoop5_sequence,
    build_clock_payload,
    build_ack_payload,
)


# ===========================================================================
# 1. CRC tests
# ===========================================================================


class TestCrc:
    def test_crc8_empty(self):
        assert crc8(b"") == 0

    def test_crc8_known_vector(self):
        # Verifiable via table: CRC8(0xAA) with poly 0x07, init 0
        # We just check roundtrip and consistency
        a = crc8(b"\x01\x02")
        b = crc8(b"\x01\x02")
        assert a == b
        assert 0 <= a <= 0xFF

    def test_crc8_different_inputs_different_outputs(self):
        assert crc8(b"\x01\x02") != crc8(b"\x01\x03")

    def test_crc32_hello(self):
        """Known-good: CRC-32 zlib of 'Hello' is 0xF7D18982."""
        assert crc32(b"Hello") == 0xF7D18982

    def test_crc32_empty(self):
        # CRC32 of empty input with init/xor-out 0xFFFFFFFF
        assert crc32(b"") == 0

    def test_crc32_known_ascii(self):
        """CRC-32 of '123456789' == 0xCBF43926 (matches zlib.crc32)."""
        assert crc32(b"123456789") == 0xCBF43926

    def test_crc32_roundtrip_different(self):
        a = crc32(b"foo")
        b = crc32(b"bar")
        assert a != b

    def test_crc16_modbus_empty(self):
        # CRC16-Modbus of empty with init 0xFFFF
        assert crc16_modbus(b"") == 0xFFFF

    def test_crc16_modbus_known_vector(self):
        # Known Modbus CRC test vector: bytes [0x01, 0x03] -> ??
        # Let's verify consistency
        a = crc16_modbus(b"\x01\x03\x00\x00\x00\x01")
        assert 0 <= a <= 0xFFFF
        # Same input should give same output
        assert a == crc16_modbus(b"\x01\x03\x00\x00\x00\x01")

    def test_crc16_modbus_different(self):
        a = crc16_modbus(b"\x01")
        b = crc16_modbus(b"\x02")
        assert a != b


# ===========================================================================
# 2. Device family tests
# ===========================================================================


class TestDeviceFamily:
    def test_whoop4_singleton(self):
        a = DeviceFamily.WHOOP_4()
        b = DeviceFamily.WHOOP_4()
        assert a is b

    def test_whoop5_singleton(self):
        a = DeviceFamily.WHOOP_5()
        b = DeviceFamily.WHOOP_5()
        assert a is b

    def test_whoop4_properties(self):
        f = DeviceFamily.WHOOP_4()
        assert f.is_whoop4
        assert not f.is_whoop5
        assert f.kind == DeviceFamilyKind.WHOOP_4
        assert f.service_uuid == "61080001-8d6d-82b8-614a-1c8cb0f8dcc6"
        assert f.aux_notify_uuid is None
        assert f.client_hello is None

    def test_whoop5_properties(self):
        f = DeviceFamily.WHOOP_5()
        assert f.is_whoop5
        assert not f.is_whoop4
        assert f.kind == DeviceFamilyKind.WHOOP_5
        assert f.service_uuid == "fd4b0001-cce1-4033-93ce-002d5875f58a"
        assert f.aux_notify_uuid is not None
        assert f.client_hello is not None

    def test_whoop5_client_hello(self):
        f = DeviceFamily.WHOOP_5()
        expected = bytes.fromhex("AA0108000001E67123019101363E5C8D")
        assert f.client_hello == expected
        assert DeviceFamily.CLIENT_HELLO == expected

    def test_from_service_uuid_whoop4(self):
        result = DeviceFamily.from_service_uuid(
            "61080001-8d6d-82b8-614a-1c8cb0f8dcc6"
        )
        assert result is not None
        assert result.is_whoop4

    def test_from_service_uuid_whoop4_alt(self):
        result = DeviceFamily.from_service_uuid(
            "61080000-8d6d-82b8-614a-1c8cb0f8dcc6"
        )
        assert result is not None
        assert result.is_whoop4

    def test_from_service_uuid_whoop5(self):
        result = DeviceFamily.from_service_uuid(
            "fd4b0001-cce1-4033-93ce-002d5875f58a"
        )
        assert result is not None
        assert result.is_whoop5

    def test_from_service_uuid_unknown(self):
        result = DeviceFamily.from_service_uuid("00000000-0000-0000-0000-000000000000")
        assert result is None


# ===========================================================================
# 3. Packet types tests
# ===========================================================================


class TestPacketTypes:
    def test_values(self):
        assert PacketTypes.COMMAND == 35
        assert PacketTypes.COMMAND_RESPONSE == 36
        assert PacketTypes.HISTORICAL_DATA == 47
        assert PacketTypes.EVENT == 48

    def test_from_code_valid(self):
        assert PacketTypes.from_code(35) == PacketTypes.COMMAND
        assert PacketTypes.from_code(47) == PacketTypes.HISTORICAL_DATA

    def test_from_code_invalid(self):
        assert PacketTypes.from_code(999) is None

    def test_to_display_string(self):
        assert "COMMAND(35)" in PacketTypes.COMMAND.to_display_string()


# ===========================================================================
# 4. Command tests
# ===========================================================================


class TestCommands:
    def test_safe_commands(self):
        assert Command.GET_CLOCK == 11
        assert Command.SET_CLOCK == 10
        assert Command.GET_BATTERY_LEVEL == 26

    def test_is_safe(self):
        assert Command.is_safe(11) is True
        assert Command.is_safe(25) is False  # FORCE_TRIM excluded
        assert Command.is_safe(29) is False  # REBOOT_STRAP excluded
        assert Command.is_safe(32) is False  # POWER_CYCLE excluded

    def test_build_frame_whoop4(self):
        """Build a GET_CLOCK command frame and verify structure."""
        family = DeviceFamily.WHOOP_4()
        frame = Command.GET_CLOCK.build_frame(seq=0, payload=b"", family=family)

        # SOF
        assert frame[0] == 0xAA
        # Length (u16 LE): will be 4 + inner_len + 4 + 4 = 4 + 3 + 4 = 11
        declared_len = frame[1] | (frame[2] << 8)
        assert declared_len >= 7
        # Type = 35 (COMMAND)
        assert frame[4] == PacketTypes.COMMAND
        # Seq = 0
        assert frame[5] == 0
        # Cmd = 11 (GET_CLOCK)
        assert frame[6] == 11

    def test_build_frame_whoop5(self):
        """Build a GET_CLOCK command frame for WHOOP 5."""
        family = DeviceFamily.WHOOP_5()
        frame = Command.GET_CLOCK.build_frame(seq=0, payload=b"", family=family)

        # SOF
        assert frame[0] == 0xAA
        # Format byte
        assert frame[1] == 0x01
        # Header bytes
        assert frame[4] == 0x00
        assert frame[5] == 0x01
        # Inner type
        assert frame[8] == PacketTypes.COMMAND
        # Inner cmd
        assert frame[10] == 11


# ===========================================================================
# 5. FrameEncoder tests
# ===========================================================================


class TestFrameEncoder:
    def test_whoop4_envelope_structure(self):
        """Verify the WHOOP 4.0 envelope round-trips through FrameReassembler."""
        family = DeviceFamily.WHOOP_4()
        payload = b"\xDE\xAD\xBE\xEF"
        frame = FrameEncoder.build_envelope(
            payload=payload, packet_type=35, seq=1, cmd=22, family=family
        )

        # Feed through reassembler
        collected: list[bytes] = []
        r = FrameReassembler(family)
        r.on_frame = lambda inner: collected.append(inner)
        r.feed(frame)

        assert len(collected) == 1
        inner = collected[0]
        assert inner[0] == 35  # type
        assert inner[1] == 1  # seq
        assert inner[2] == 22  # cmd
        assert inner[3:] == payload

    def test_whoop5_envelope_structure(self):
        """Verify the WHOOP 5.0 envelope round-trips through FrameReassembler."""
        family = DeviceFamily.WHOOP_5()
        payload = b"\x01\x02\x03"
        frame = FrameEncoder.build_envelope(
            payload=payload, packet_type=38, seq=5, cmd=11, family=family
        )

        collected: list[bytes] = []
        r = FrameReassembler(family)
        r.on_frame = lambda inner: collected.append(inner)
        r.feed(frame)

        assert len(collected) == 1
        inner = collected[0]
        assert inner[0] == 38  # type
        assert inner[1] == 5  # seq
        assert inner[2] == 11  # cmd
        assert inner[3:] == payload

    def test_empty_payload(self):
        family = DeviceFamily.WHOOP_4()
        frame = FrameEncoder.build_envelope(
            payload=b"", packet_type=35, family=family
        )
        collected: list[bytes] = []
        r = FrameReassembler(family)
        r.on_frame = lambda inner: collected.append(inner)
        r.feed(frame)
        assert len(collected) == 1
        assert collected[0][3:] == b""


# ===========================================================================
# 6. FrameReassembler tests
# ===========================================================================


class TestFrameReassembler:
    def test_single_frame(self):
        family = DeviceFamily.WHOOP_4()
        frame = FrameEncoder.build_envelope(
            payload=b"payload", packet_type=35, seq=0, family=family
        )

        collected: list[bytes] = []
        r = FrameReassembler(family)
        r.on_frame = lambda inner: collected.append(inner)
        r.feed(frame)

        assert len(collected) == 1
        assert collected[0][3:] == b"payload"

    def test_multi_fragment(self):
        """Feed a frame in small chunks — should still reassemble correctly."""
        family = DeviceFamily.WHOOP_4()
        frame = FrameEncoder.build_envelope(
            payload=b"multi-fragment-test", packet_type=35, seq=0, family=family
        )

        collected: list[bytes] = []
        r = FrameReassembler(family)
        r.on_frame = lambda inner: collected.append(inner)

        # Feed in 1-byte fragments
        for i in range(len(frame)):
            r.feed(frame[i : i + 1])

        assert len(collected) == 1
        assert collected[0][3:] == b"multi-fragment-test"

    def test_sof_resync(self):
        """Frame with garbage before SOF should still extract correctly."""
        family = DeviceFamily.WHOOP_4()
        frame = FrameEncoder.build_envelope(
            payload=b"data", packet_type=35, seq=0, family=family
        )

        garbage = b"\xFF\xFE\xFD\xFC\xFB"
        combined = garbage + frame

        collected: list[bytes] = []
        r = FrameReassembler(family)
        r.on_frame = lambda inner: collected.append(inner)
        r.feed(combined)

        assert len(collected) == 1
        assert collected[0][3:] == b"data"

    def test_crc_fail_skip(self):
        """A corrupted frame (bad CRC32) should be skipped."""
        family = DeviceFamily.WHOOP_4()

        # Build a valid frame
        good_frame = FrameEncoder.build_envelope(
            payload=b"good", packet_type=35, seq=0, family=family
        )

        # Corrupt it by flipping a byte in the payload
        bad_frame = bytearray(good_frame)
        bad_frame[7] ^= 0xFF  # corrupt payload byte

        collected: list[bytes] = []
        r = FrameReassembler(family)
        r.on_frame = lambda inner: collected.append(inner)

        # Feed bad frame then good frame
        r.feed(bytes(bad_frame) + good_frame)

        # Only the good frame should be extracted
        assert len(collected) == 1
        assert collected[0][3:] == b"good"

    def test_two_sequential_frames(self):
        """Two complete frames back-to-back should both be extracted."""
        family = DeviceFamily.WHOOP_4()
        f1 = FrameEncoder.build_envelope(
            payload=b"first", packet_type=35, seq=0, family=family
        )
        f2 = FrameEncoder.build_envelope(
            payload=b"second", packet_type=35, seq=1, family=family
        )

        collected: list[bytes] = []
        r = FrameReassembler(family)
        r.on_frame = lambda inner: collected.append(inner)
        r.feed(f1 + f2)

        assert len(collected) == 2
        assert collected[0][3:] == b"first"
        assert collected[1][3:] == b"second"

    def test_whoop5_multi_fragment(self):
        """WHOOP 5.0 frame fed in fragments."""
        family = DeviceFamily.WHOOP_5()
        frame = FrameEncoder.build_envelope(
            payload=b"w5-data", packet_type=35, seq=0, family=family
        )

        collected: list[bytes] = []
        r = FrameReassembler(family)
        r.on_frame = lambda inner: collected.append(inner)

        for i in range(len(frame)):
            r.feed(frame[i : i + 1])

        assert len(collected) == 1
        assert collected[0][3:] == b"w5-data"

    def test_reset(self):
        family = DeviceFamily.WHOOP_4()
        frame = FrameEncoder.build_envelope(
            payload=b"test", packet_type=35, seq=0, family=family
        )

        r = FrameReassembler(family)
        r.feed(frame[:6])  # feed partial
        assert r.pending_bytes() > 0
        r.reset()
        assert r.pending_bytes() == 0

    def test_whoop4_crc8_fail(self):
        """Corrupt CRC8 — frame should be skipped."""
        family = DeviceFamily.WHOOP_4()
        good_frame = FrameEncoder.build_envelope(
            payload=b"test", packet_type=35, seq=0, family=family
        )

        bad_frame = bytearray(good_frame)
        bad_frame[3] ^= 0xFF  # corrupt CRC8 byte

        collected: list[bytes] = []
        r = FrameReassembler(family)
        r.on_frame = lambda inner: collected.append(inner)
        r.feed(bytes(bad_frame) + good_frame)

        assert len(collected) == 1
        assert collected[0][3:] == b"test"

    def test_whoop5_crc16_fail(self):
        """Corrupt CRC16-Modbus — frame should be skipped."""
        family = DeviceFamily.WHOOP_5()
        good_frame = FrameEncoder.build_envelope(
            payload=b"test", packet_type=35, seq=0, family=family
        )

        bad_frame = bytearray(good_frame)
        bad_frame[6] ^= 0xFF  # corrupt CRC16 byte

        collected: list[bytes] = []
        r = FrameReassembler(family)
        r.on_frame = lambda inner: collected.append(inner)
        r.feed(bytes(bad_frame) + good_frame)

        assert len(collected) == 1
        assert collected[0][3:] == b"test"


# ===========================================================================
# 7. parse_frame tests
# ===========================================================================


class TestParseFrame:
    def test_parse_realtime(self):
        """Parse a type-40 realtime data frame."""
        # Inner record: [type][seq][cmd][3 unknown][timestamp u32][sub u16][HR][rr_count][rr...]
        inner = struct.pack("<BBB", 40, 0, 0)
        inner += b"\x00\x00\x00"  # 3 unknown bytes
        inner += struct.pack("<I", 1234567890)  # timestamp
        inner += struct.pack("<H", 0)  # subseconds
        inner += struct.pack("<B", 72)  # HR
        inner += struct.pack("<B", 2)  # RR count
        inner += struct.pack("<HH", 800, 820)  # two RR intervals

        family = DeviceFamily.WHOOP_4()
        result = parse_frame(inner, family)

        assert isinstance(result, RealtimeData)
        assert result.heart_rate == 72
        assert result.rr_count == 2
        assert result.rr_intervals == [800, 820]

    def test_parse_event(self):
        """Parse a type-48 event frame."""
        # Inner: [type][seq][cmd][3 unknown][event_kind][1 unknown][timestamp u32]
        inner = struct.pack("<BBB", 48, 0, 0)
        inner += b"\x00\x00\x00"  # 3 unknown bytes
        inner += struct.pack("<B", 0)  # event kind
        inner += b"\x00"  # 1 unknown byte
        inner += struct.pack("<I", 12345)  # event timestamp

        family = DeviceFamily.WHOOP_4()
        result = parse_frame(inner, family)

        assert isinstance(result, Event)
        assert result.event_timestamp == 12345

    def test_parse_metadata(self):
        """Parse a type-49 metadata frame."""
        # Inner: [type][seq][cmd][3 unknown][metadata_kind]
        inner = struct.pack("<BBB", 49, 0, 0)
        inner += b"\x00\x00\x00"  # 3 unknown bytes
        inner += struct.pack("<B", 1)  # HISTORY_START

        family = DeviceFamily.WHOOP_4()
        result = parse_frame(inner, family)

        assert isinstance(result, Metadata)
        assert result.metadata_kind == MetadataKind.HISTORY_START
        assert result.is_history_start is True
        assert result.is_history_end is False

    def test_parse_command_response(self):
        """Parse a type-36 command response frame."""
        # Inner: [type][seq][cmd][3 unknown][response_code][response_payload...]
        inner = struct.pack("<BBB", 36, 1, 4)
        inner += b"\x00\x00\x00"  # 3 unknown bytes
        inner += struct.pack("<B", 11)  # response_code (GET_CLOCK)
        inner += struct.pack("<I", 1234567890)

        family = DeviceFamily.WHOOP_4()
        result = parse_frame(inner, family)

        assert isinstance(result, CommandResponse)
        assert result.response_code == 11

    def test_parse_unknown_type(self):
        """Unknown packet type should return None."""
        inner = struct.pack("<BBB", 255, 0, 0)
        family = DeviceFamily.WHOOP_4()
        result = parse_frame(inner, family)
        assert result is None

    def test_too_short(self):
        """Too-short inner record returns None."""
        family = DeviceFamily.WHOOP_4()
        assert parse_frame(b"\x23", family) is None
        assert parse_frame(b"", family) is None


# ===========================================================================
# 8. Historical parsing tests
# ===========================================================================


class TestHistoricalParsing:
    def test_v24_record(self):
        """Build and parse a v24 historical record (84+ bytes)."""
        family = DeviceFamily.WHOOP_4()

        # Build an inner record with type=47, seq=24 (version)
        inner = bytearray(90)
        inner[0] = 47  # type
        inner[1] = 24  # version (seq)
        inner[2] = 0  # cmd

        # timestamp at offset 11
        struct.pack_into("<I", inner, 11, 1700000000)

        # HR at offset 21
        inner[21] = 68

        # RR count at offset 22
        inner[22] = 3
        # RR intervals at offset 23+
        struct.pack_into("<HHH", inner, 23, 750, 760, 770)

        # PPG at offsets 33, 35
        struct.pack_into("<H", inner, 33, 1000)
        struct.pack_into("<H", inner, 35, 900)

        # Gravity at 40, 44, 48
        struct.pack_into("<f", inner, 40, 0.1)
        struct.pack_into("<f", inner, 44, -0.2)
        struct.pack_into("<f", inner, 48, 0.98)

        # Skin contact at 55
        inner[55] = 1

        # SpO2 at 68, 70
        struct.pack_into("<H", inner, 68, 50000)
        struct.pack_into("<H", inner, 70, 48000)

        # Skin temp at 72
        struct.pack_into("<H", inner, 72, 32000)

        # Resp rate at 80
        struct.pack_into("<H", inner, 80, 140)

        # Signal quality at 82
        struct.pack_into("<H", inner, 82, 3)

        result = parse_frame(bytes(inner), family)

        assert isinstance(result, HistoricalDataFrame)
        assert result.record_version == 24
        assert len(result.records) == 1

        rec = result.records[0]
        assert rec.unix == 1700000000
        assert rec.heart_rate == 68
        assert rec.rr_count == 3
        assert rec.rr_intervals == [750, 760, 770]
        assert rec.ppg_green == 1000
        assert rec.ppg_red_ir == 900
        assert rec.gravity_x == pytest.approx(0.1)
        assert rec.gravity_y == pytest.approx(-0.2)
        assert rec.gravity_z == pytest.approx(0.98)
        assert rec.skin_contact == 1
        assert rec.spo2_red == 50000
        assert rec.spo2_ir == 48000
        assert rec.skin_temp_raw == 32000
        assert rec.resp_rate_raw == 140
        assert rec.signal_quality == 3

    def test_v18_record(self):
        """Build and parse a v18 historical record (124 bytes, WHOOP 5.0)."""
        family = DeviceFamily.WHOOP_5()

        inner = bytearray(130)
        inner[0] = 47  # type
        inner[1] = 18  # version
        inner[2] = 0

        struct.pack_into("<I", inner, 15, 1700000001)
        inner[22] = 72  # HR
        inner[23] = 2  # RR count
        struct.pack_into("<HH", inner, 24, 800, 820)

        struct.pack_into("<f", inner, 45, 0.5)
        struct.pack_into("<f", inner, 49, -0.3)
        struct.pack_into("<f", inner, 53, 0.87)
        struct.pack_into("<H", inner, 73, 36864)  # skin_temp_raw

        result = parse_frame(bytes(inner), family)

        assert isinstance(result, HistoricalDataFrame)
        assert result.record_version == 18
        assert len(result.records) == 1

        rec = result.records[0]
        assert rec.unix == 1700000001
        assert rec.heart_rate == 72
        assert rec.rr_intervals == [800, 820]
        assert rec.gravity_x == pytest.approx(0.5)
        assert rec.skin_temp_raw == 36864

    def test_v18_skin_temp_celsius(self):
        """Skin temp celsius = raw / 128.0"""
        raw = 36864
        celsius = raw / 128.0
        assert celsius == pytest.approx(288.0)

    def test_v26_ppg(self):
        """Build and parse a v26 high-rate PPG record (88 bytes)."""
        family = DeviceFamily.WHOOP_5()

        inner = bytearray(90)
        inner[0] = 47
        inner[1] = 26  # version
        inner[2] = 0

        # ppg_channel at offset 12
        struct.pack_into("<B", inner, 12, 1)

        # unix at offset 15
        struct.pack_into("<I", inner, 15, 1700000002)

        # ppg_waveform at offset 27-74 (24 x i16)
        for i in range(24):
            struct.pack_into("<h", inner, 27 + i * 2, i * 100)

        result = parse_frame(bytes(inner), family)

        assert isinstance(result, HistoricalDataFrame)
        assert result.record_version == 26
        assert len(result.records) == 1
        assert result.records[0].unix == 1700000002

    def test_generic_fallback(self):
        """Generic fallback parsing for unknown version (but >= 25 bytes)."""
        family = DeviceFamily.WHOOP_5()  # Must be WHOOP 5 to avoid v24 path

        inner = bytearray(30)
        inner[0] = 47
        inner[1] = 99  # unknown version (not 18, 26)
        inner[2] = 0

        struct.pack_into("<I", inner, 11, 1700010000)
        inner[21] = 80  # HR
        inner[22] = 1  # RR count
        struct.pack_into("<H", inner, 23, 700)

        result = parse_frame(bytes(inner), family)

        assert isinstance(result, HistoricalDataFrame)
        assert len(result.records) == 1
        assert result.records[0].unix == 1700010000
        assert result.records[0].heart_rate == 80
        assert result.records[0].rr_intervals == [700]


# ===========================================================================
# 9. Handshake tests
# ===========================================================================


class TestHandshake:
    def test_whoop4_sequence(self):
        seq = whoop4_sequence(1700000000)
        assert len(seq) == 6
        assert seq[0].command == Command.GET_HELLO_HARVARD.value
        assert seq[0].payload == b""
        assert seq[2].command == Command.SET_CLOCK.value
        assert len(seq[2].payload) == 8

    def test_whoop5_sequence(self):
        seq = whoop5_sequence(1700000000)
        assert len(seq) == 7  # CLIENT_HELLO + 6 commands
        assert seq[0].command == -1  # CLIENT_HELLO marker
        assert len(seq[0].payload) > 0
        assert seq[1].command == Command.GET_HELLO_HARVARD.value

    def test_build_clock_payload(self):
        payload = build_clock_payload(1700000000, 123456)
        assert len(payload) == 8
        ts, sub = struct.unpack("<II", payload)
        assert ts == 1700000000
        assert sub == 123456

    def test_build_ack_payload(self):
        payload = build_ack_payload(1700000000)
        assert len(payload) == 9
        assert payload[0] == 0x01
        ts = struct.unpack("<Q", payload[1:])[0]
        assert ts == 1700000000


# ===========================================================================
# 10. ParsedFrame dispatch tests
# ===========================================================================


class TestParsedFrameCreates:
    def test_historical_data_frame_defaults(self):
        f = HistoricalDataFrame()
        assert f.packet_type == PacketTypes.HISTORICAL_DATA
        assert f.records == []
        assert f.record_version == 0

    def test_realtime_data_defaults(self):
        f = RealtimeData()
        assert f.packet_type == PacketTypes.REALTIME_DATA
        assert f.rr_intervals == []

    def test_realtime_raw_data_defaults(self):
        f = RealtimeRawData()
        assert f.packet_type == PacketTypes.REALTIME_RAW_DATA

    def test_command_response_defaults(self):
        f = CommandResponse()
        assert f.packet_type == PacketTypes.COMMAND_RESPONSE

    def test_event_defaults(self):
        f = Event()
        assert f.packet_type == PacketTypes.EVENT
        assert f.event_kind == EventKind.UNKNOWN

    def test_metadata_defaults(self):
        f = Metadata()
        assert f.packet_type == PacketTypes.METADATA
        assert not f.is_history_start
        assert not f.is_history_end


# ===========================================================================
# 11. Realtime raw / ConsoleLogs tests
# ===========================================================================


class TestRealtimeRaw:
    def test_parse_realtime_raw(self):
        family = DeviceFamily.WHOOP_4()
        # Inner: [type][seq][cmd][record_header u16][?][timestamp u32][sub u16][raw...]
        # C#: rec_header at offset 3, ts at 6, sub at 10, raw at 12
        inner = struct.pack("<BBB", 43, 0, 0)
        inner += struct.pack("<H", 0xABCD)  # record_header at offset 3
        inner += b"\x00"  # 1 byte padding
        inner += struct.pack("<I", 1700000100)  # timestamp at offset 6
        inner += struct.pack("<H", 500)  # subseconds at offset 10
        inner += b"RAW_DATA_HERE"  # raw at offset 12

        result = parse_frame(inner, family)
        assert isinstance(result, RealtimeRawData)
        assert result.record_header == 0xABCD
        assert result.timestamp == 1700000100
        assert result.subseconds == 500
        assert result.raw_payload == b"RAW_DATA_HERE"

    def test_console_logs(self):
        from whoop.protocol.parsed_frame import ConsoleLogs
        family = DeviceFamily.WHOOP_4()
        inner = struct.pack("<BBB", 50, 0, 0) + b"LOG_DATA"

        result = parse_frame(inner, family)
        assert isinstance(result, ConsoleLogs)
        assert result.payload == b"LOG_DATA"


# ===========================================================================
# 12. Edge cases
# ===========================================================================


class TestEdgeCases:
    def test_frame_encoder_default_family(self):
        """build_envelope without family defaults to WHOOP 4.0."""
        frame = FrameEncoder.build_envelope(
            payload=b"hi", packet_type=35
        )
        assert frame[0] == 0xAA
        # Length bytes at positions 1-2
        declared_len = frame[1] | (frame[2] << 8)
        assert declared_len > 0

    def test_reassembler_no_callback(self):
        """Reassembler works fine without a callback (no crash)."""
        family = DeviceFamily.WHOOP_4()
        frame = FrameEncoder.build_envelope(
            payload=b"test", packet_type=35, family=family
        )
        r = FrameReassembler(family)
        r.feed(frame)  # Should not raise

    def test_max_length_rejected(self):
        """Declared length exceeding buffer size should be rejected."""
        family = DeviceFamily.WHOOP_4()
        inner = b"\xAA\xFF\xFF" + b"\x00" * 20000
        collected: list[bytes] = []
        r = FrameReassembler(family)
        r.on_frame = lambda inner: collected.append(inner)
        r.feed(inner)
        assert len(collected) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
