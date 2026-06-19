"""Frame reassembler and encoder for the WHOOP protocol.

SOF-synced, length-declared, two-family frame reassembler with CRC validation.
"""

from __future__ import annotations

import struct
from typing import Callable

from whoop.protocol.crc import crc8, crc32, crc16_modbus
from whoop.protocol.device_family import DeviceFamily, DeviceFamilyKind

_SOF: int = 0xAA
_BUFFER_SIZE: int = 16384  # 16 KB


class FrameReassembler:
    """SOF-synced, length-declared, two-family frame reassembler.

    Accumulates BLE notification fragments in a 16 KB ring buffer and emits
    complete, CRC-validated inner records.

    Usage::

        reassembler = FrameReassembler(DeviceFamily.WHOOP_4())
        reassembler.on_frame = lambda inner: handle_frame(inner)

        # Feed incoming BLE notification data:
        reassembler.feed(some_bytes)
    """

    def __init__(self, family: DeviceFamily) -> None:
        self.family: DeviceFamily = family
        self._ring: bytearray = bytearray(_BUFFER_SIZE)
        self._write_pos: int = 0
        self._read_pos: int = 0
        self.on_frame: Callable[[bytes], None] | None = None

    # -- Public API -----------------------------------------------------------

    def feed(self, data: bytes) -> None:
        """Feed incoming BLE notification bytes.

        Calls ``on_frame`` for each complete, CRC-validated inner record extracted.
        """
        # Append new bytes to ring buffer
        buf = self._ring
        for b in data:
            buf[self._write_pos] = b
            self._write_pos = (self._write_pos + 1) % _BUFFER_SIZE

        # Attempt to extract complete frames
        while True:
            available = self._available()
            min_len = 4 if self.family.is_whoop4 else 8
            if available < min_len:
                break

            # Hunt for SOF
            sof_idx = self._find_sof(self._read_pos, available)
            if sof_idx < 0:
                # No SOF — discard everything
                self._read_pos = 0
                self._write_pos = 0
                break

            # Discard garbage before SOF
            if sof_idx != self._read_pos:
                self._read_pos = sof_idx

            old_read_pos = self._read_pos
            available_after = self._available()

            frame: bytes | None
            if self.family.is_whoop4:
                frame = self._try_extract4(available_after)
            else:
                frame = self._try_extract5(available_after)

            if frame is None:
                if self._read_pos != old_read_pos:
                    continue  # CRC failure — readPos advanced, hunt for next SOF
                break  # Incomplete — wait for more data

            if self.on_frame is not None:
                self.on_frame(frame)

        # Compact buffer if readPos advanced significantly
        if self._read_pos > 8192:
            remaining = self._available()
            for i in range(remaining):
                buf[i] = buf[(self._read_pos + i) % _BUFFER_SIZE]
            self._write_pos = remaining
            self._read_pos = 0

    def reset(self) -> None:
        """Reset the reassembler state."""
        self._read_pos = 0
        self._write_pos = 0

    def pending_bytes(self) -> int:
        """Number of unprocessed bytes in the buffer."""
        return self._available()

    # -- Internal helpers -----------------------------------------------------

    def _available(self) -> int:
        return (self._write_pos - self._read_pos + _BUFFER_SIZE) % _BUFFER_SIZE

    def _find_sof(self, start: int, available: int) -> int:
        buf = self._ring
        for i in range(available):
            idx = (start + i) % _BUFFER_SIZE
            if buf[idx] == _SOF:
                return idx
        return -1

    def _try_extract4(self, available: int) -> bytes | None:
        """Try to extract a WHOOP 4.0 frame.

        Returns the inner record on success, None if more data needed or CRC failure.
        """
        if available < 4:
            return None

        buf = self._ring
        sof_pos = self._read_pos

        # Read declared length (u16 LE) from buffer positions 1,2 (relative to SOF)
        len_lo = buf[(sof_pos + 1) % _BUFFER_SIZE] & 0xFF
        len_hi = buf[(sof_pos + 2) % _BUFFER_SIZE] & 0xFF
        declared_length = len_lo | (len_hi << 8)

        if declared_length < 7:
            self._read_pos = (self._read_pos + 1) % _BUFFER_SIZE
            return None

        total_frame_size = declared_length + 4
        if total_frame_size > _BUFFER_SIZE:
            self._read_pos = (self._read_pos + 1) % _BUFFER_SIZE
            return None

        if available < total_frame_size:
            return None  # Incomplete — wait

        # Extract full frame
        frame = bytearray(total_frame_size)
        for i in range(total_frame_size):
            frame[i] = buf[(sof_pos + i) % _BUFFER_SIZE]

        # Validate CRC8 over the 2 length bytes only: frame[1:3]
        computed_crc8 = crc8(bytes(frame[1:3]))
        if computed_crc8 != frame[3]:
            self._read_pos = (self._read_pos + 1) % _BUFFER_SIZE
            return None

        # Validate CRC32 over inner record: frame[4:total_frame_size-4]
        payload_end = total_frame_size - 4
        inner = bytes(frame[4:payload_end])
        expected_crc32: int = (
            (frame[payload_end] & 0xFF)
            | ((frame[payload_end + 1] & 0xFF) << 8)
            | ((frame[payload_end + 2] & 0xFF) << 16)
            | ((frame[payload_end + 3] & 0xFF) << 24)
        )
        if crc32(inner) != expected_crc32:
            self._read_pos = (self._read_pos + 1) % _BUFFER_SIZE
            return None

        self._read_pos = (self._read_pos + total_frame_size) % _BUFFER_SIZE
        return inner

    def _try_extract5(self, available: int) -> bytes | None:
        """Try to extract a WHOOP 5.0 frame. UNVALIDATED.

        Returns the inner record on success, None if more data needed or CRC failure.
        """
        if available < 8:
            return None

        buf = self._ring
        sof_pos = self._read_pos

        # Read declared length (u16 LE) from buffer positions 2,3
        len_lo = buf[(sof_pos + 2) % _BUFFER_SIZE] & 0xFF
        len_hi = buf[(sof_pos + 3) % _BUFFER_SIZE] & 0xFF
        declared_length = len_lo | (len_hi << 8)

        if declared_length < 7:
            self._read_pos = (self._read_pos + 1) % _BUFFER_SIZE
            return None

        total_frame_size = declared_length + 8
        if total_frame_size > _BUFFER_SIZE:
            self._read_pos = (self._read_pos + 1) % _BUFFER_SIZE
            return None

        if available < total_frame_size:
            return None  # Incomplete — wait

        # Extract full frame
        frame = bytearray(total_frame_size)
        for i in range(total_frame_size):
            frame[i] = buf[(sof_pos + i) % _BUFFER_SIZE]

        # Validate CRC16-Modbus over frame[0:6] (6 header bytes)
        header = bytes(frame[:6])
        expected_crc16: int = (frame[6] & 0xFF) | ((frame[7] & 0xFF) << 8)
        if crc16_modbus(header) != expected_crc16:
            self._read_pos = (self._read_pos + 1) % _BUFFER_SIZE
            return None

        # Validate CRC32 over inner record: frame[8:declared_length+4]
        inner_length = declared_length - 4
        inner = bytes(frame[8 : 8 + inner_length])
        crc32_offset = declared_length + 4
        expected_crc32: int = (
            (frame[crc32_offset] & 0xFF)
            | ((frame[crc32_offset + 1] & 0xFF) << 8)
            | ((frame[crc32_offset + 2] & 0xFF) << 16)
            | ((frame[crc32_offset + 3] & 0xFF) << 24)
        )
        if crc32(inner) != expected_crc32:
            self._read_pos = (self._read_pos + 1) % _BUFFER_SIZE
            return None

        self._read_pos = (self._read_pos + total_frame_size) % _BUFFER_SIZE
        return inner


# ===========================================================================
# Frame Encoder
# ===========================================================================


class FrameEncoder:
    """Encodes command frames for writing to the strap.

    Dispatches to the appropriate envelope based on DeviceFamily.
    """

    SOF: int = _SOF

    @staticmethod
    def build_envelope(
        payload: bytes,
        packet_type: int,
        seq: int = 0,
        cmd: int = 0,
        family: DeviceFamily | None = None,
    ) -> bytes:
        """Build a complete wire frame.

        Args:
            payload: Inner payload bytes.
            packet_type: The packet type byte (e.g., 35 = COMMAND).
            seq: Sequence number.
            cmd: Command number.
            family: Device family (defaults to WHOOP 4.0).

        Returns:
            Complete wire frame ready for BLE characteristic write.
        """
        if family is None:
            family = DeviceFamily.WHOOP_4()

        if family.is_whoop4:
            return _whoop4_encode(packet_type, seq, cmd, payload)
        else:
            return _whoop5_encode(packet_type, seq, cmd, payload)


# ---------------------------------------------------------------------------
# WHOOP 4.0 envelope
# ---------------------------------------------------------------------------

_WHOOP4_HEADER_SIZE = 7  # SOF + len(2) + CRC8 + type + seq + cmd
_WHOOP4_TRAILER_SIZE = 4  # CRC32
_WHOOP4_ENVELOPE_OVERHEAD = 4  # SOF + len(2) + CRC8


def _whoop4_encode(packet_type: int, seq: int, cmd: int, payload: bytes) -> bytes:
    """Encode a command payload into a complete WHOOP 4.0 wire frame.

    Wire format: SOF(1) + Len(2, u16 LE) + CRC8(1, over len bytes) +
                 inner(type+seq+cmd+payload) + CRC32(4)
    """
    inner_len = 3 + len(payload)  # type + seq + cmd + payload
    declared_len = inner_len + 4  # inner record + CRC32
    total_len = declared_len + 4  # + SOF + len(2) + CRC8 envelope

    frame = bytearray(total_len)
    frame[0] = _SOF

    # Length (u16 LE) = declared_len (inner + CRC32 only, NOT including header)
    struct.pack_into("<H", frame, 1, declared_len)

    # CRC8 over the two length bytes only
    frame[3] = crc8(bytes(frame[1:3]))

    # Inner record: type + seq + cmd + payload
    frame[4] = packet_type
    frame[5] = seq
    frame[6] = cmd
    frame[7 : 7 + len(payload)] = payload

    # CRC32 over inner record [4:4+inner_len)
    inner_end = 4 + inner_len
    inner = bytes(frame[4:inner_end])
    crc32_val = crc32(inner)
    struct.pack_into("<I", frame, inner_end, crc32_val)

    return bytes(frame)


# ---------------------------------------------------------------------------
# WHOOP 5.0 envelope
# ---------------------------------------------------------------------------

_WHOOP5_HEADER_SIZE = 11  # SOF + fmt + len(2) + header(2) + CRC16(2) + type + seq + cmd
_WHOOP5_TRAILER_SIZE = 4
_WHOOP5_ENVELOPE_OVERHEAD = 8  # SOF + fmt + len(2) + header(2) + CRC16(2)
_WHOOP5_FORMAT_BYTE = 0x01


def _whoop5_encode(packet_type: int, seq: int, cmd: int, payload: bytes) -> bytes:
    """Encode a command payload into a complete WHOOP 5.0 wire frame.

    Wire format: SOF(1) + Fmt(1) + Len(2, u16 LE) + Hdr(2) + CRC16(2) +
                 inner(type+seq+cmd+payload) + CRC32(4)
    """
    inner_len = 3 + len(payload)
    declared_len = inner_len + 4
    total_len = declared_len + 8

    frame = bytearray(total_len)
    frame[0] = _SOF
    frame[1] = _WHOOP5_FORMAT_BYTE

    # Declared length (u16 LE)
    struct.pack_into("<H", frame, 2, declared_len)

    # Header bytes (0x0001 for CLIENT_HELLO style)
    frame[4] = 0x00
    frame[5] = 0x01

    # CRC16-Modbus over frame[0:6] — the 6 header bytes
    crc16_val = crc16_modbus(bytes(frame[:6]))
    struct.pack_into("<H", frame, 6, crc16_val)

    # Inner record
    frame[8] = packet_type
    frame[9] = seq
    frame[10] = cmd
    frame[11 : 11 + len(payload)] = payload

    # CRC32 over inner record
    inner = bytes(frame[8 : 8 + inner_len])
    crc32_val = crc32(inner)
    crc_pos = 8 + inner_len
    struct.pack_into("<I", frame, crc_pos, crc32_val)

    return bytes(frame)
