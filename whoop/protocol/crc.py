"""CRC implementations for the WHOOP protocol.

CRC-8 (poly 0x07, init 0x00) for WHOOP 4.0 length bytes.
CRC-32 zlib (poly 0xEDB88320, reflected, init 0xFFFFFFFF, XOR out 0xFFFFFFFF) for payload.
CRC-16-Modbus (poly 0xA001, init 0xFFFF, reflected) for WHOOP 5.0 header bytes.

All use precomputed 256-entry lookup tables.
"""

from __future__ import annotations

import array

# ---------------------------------------------------------------------------
# CRC-8  (WHOOP 4.0)
# ---------------------------------------------------------------------------

_CRC8_TABLE: array.array = array.array("B", [0] * 256)


def _build_crc8_table() -> None:
    for i in range(256):
        crc: int = i
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ 0x07) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
        _CRC8_TABLE[i] = crc


_build_crc8_table()


def crc8(data: bytes) -> int:
    """Compute CRC-8 (poly 0x07, init 0x00) over *data*.

    Used by WHOOP 4.0 envelopes: computed over the 2 length bytes.
    """
    crc: int = 0
    for b in data:
        crc = _CRC8_TABLE[crc ^ b]
    return crc


# ---------------------------------------------------------------------------
# CRC-32 zlib  (both families)
# ---------------------------------------------------------------------------

_CRC32_TABLE: list[int] = [0] * 256


def _build_crc32_table() -> None:
    for i in range(256):
        crc: int = i
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xEDB88320
            else:
                crc >>= 1
        _CRC32_TABLE[i] = crc


_build_crc32_table()


def crc32(data: bytes) -> int:
    """Compute CRC-32 zlib (reflected, poly 0xEDB88320, init/xor-out 0xFFFFFFFF).

    Used by both WHOOP 4.0 and 5.0 for payload validation.
    Matches Python's zlib.crc32 (e.g., ``crc32(b"Hello") == 0x3610A686``).
    """
    crc: int = 0xFFFFFFFF
    for b in data:
        index: int = (crc ^ b) & 0xFF
        crc = (crc >> 8) ^ _CRC32_TABLE[index]
    return crc ^ 0xFFFFFFFF


# ---------------------------------------------------------------------------
# CRC-16 Modbus  (WHOOP 5.0)
# ---------------------------------------------------------------------------

_CRC16_TABLE: array.array = array.array("H", [0] * 256)


def _build_crc16_table() -> None:
    for i in range(256):
        crc: int = i
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
        _CRC16_TABLE[i] = crc & 0xFFFF


_build_crc16_table()


def crc16_modbus(data: bytes) -> int:
    """Compute CRC-16-Modbus (poly 0xA001, init 0xFFFF, reflected).

    Used by WHOOP 5.0 envelopes: computed over the 6 header bytes frame[0..6).
    """
    crc: int = 0xFFFF
    for b in data:
        index: int = (crc ^ b) & 0x00FF
        crc = (crc >> 8) ^ _CRC16_TABLE[index]
    return crc
