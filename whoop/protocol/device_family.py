"""Device family abstraction — WHOOP 4.0 and WHOOP 5.0."""

from __future__ import annotations

from enum import Enum


class DeviceFamilyKind(Enum):
    """Enumeration of known WHOOP device families."""

    WHOOP_4 = "Whoop4"
    WHOOP_4_PUFFIN = "Whoop4Puffin"  # Primary service from official APK
    WHOOP_5 = "Whoop5"


class DeviceFamily:
    """Two-generation device family with BLE UUIDs and framing metadata.

    WHOOP 4.0 is the active/validated target; WHOOP 5.0 is implemented
    but UNVALIDATED.
    """

    # -- WHOOP 4.0 ----------------------------------------------------------------

    WHOOP_4_ALT_SERVICE_UUID: str = "61080000-8d6d-82b8-614a-1c8cb0f8dcc6"
    # Primary service from official APK v5.456.0 (codename \"Puffin\")
    WHOOP_4_PUFFIN_SERVICE_UUID: str = "11500001-6215-11EE-8C99-0242AC120002"

    _whoop4: DeviceFamily | None = None
    _whoop5: DeviceFamily | None = None
    _whoop4_puffin: DeviceFamily | None = None

    def __init__(
        self,
        kind: DeviceFamilyKind,
        service_uuid: str,
        cmd_char_uuid: str,
        cmd_notify_uuid: str,
        event_notify_uuid: str,
        data_notify_uuid: str,
        aux_notify_uuid: str | None,
        client_hello: bytes | None,
    ) -> None:
        self.kind: DeviceFamilyKind = kind
        self.service_uuid: str = service_uuid
        self.cmd_char_uuid: str = cmd_char_uuid
        self.cmd_notify_uuid: str = cmd_notify_uuid
        self.event_notify_uuid: str = event_notify_uuid
        self.data_notify_uuid: str = data_notify_uuid
        self.aux_notify_uuid: str | None = aux_notify_uuid
        self.client_hello: bytes | None = client_hello

    @classmethod
    def WHOOP_4(cls) -> DeviceFamily:
        """Return the WHOOP 4.0 device family singleton."""
        if cls._whoop4 is None:
            cls._whoop4 = cls(
                kind=DeviceFamilyKind.WHOOP_4,
                service_uuid="61080001-8d6d-82b8-614a-1c8cb0f8dcc6",
                cmd_char_uuid="61080002-8d6d-82b8-614a-1c8cb0f8dcc6",
                cmd_notify_uuid="61080003-8d6d-82b8-614a-1c8cb0f8dcc6",
                event_notify_uuid="61080004-8d6d-82b8-614a-1c8cb0f8dcc6",
                data_notify_uuid="61080005-8d6d-82b8-614a-1c8cb0f8dcc6",
                aux_notify_uuid=None,
                client_hello=None,
            )
        return cls._whoop4

    @classmethod
    def WHOOP_4_PUFFIN(cls) -> DeviceFamily:
        """Return the WHOOP 4.0 variant using the PRIMARY Puffin service UUIDs.

        Discovered from official APK v5.456.0 decompilation. The 6108 service
        is tertiary/legacy; this 1150 service is the primary service used by
        the official WHOOP Android app for commands, data, and history.
        """
        if cls._whoop4_puffin is None:
            cls._whoop4_puffin = cls(
                kind=DeviceFamilyKind.WHOOP_4,
                service_uuid="11500001-6215-11EE-8C99-0242AC120002",
                cmd_char_uuid="11500001-6215-11EE-8C99-0242AC120002",
                cmd_notify_uuid="11500002-6215-11EE-8C99-0242AC120002",
                event_notify_uuid="11500004-6215-11EE-8C99-0242AC120002",
                data_notify_uuid="11500003-6215-11EE-8C99-0242AC120002",
                aux_notify_uuid="11500005-6215-11EE-8C99-0242AC120002",
                client_hello=None,
            )
        return cls._whoop4_puffin

    @classmethod
    def WHOOP_5(cls) -> DeviceFamily:
        """Return the WHOOP 5.0 device family singleton (UNVALIDATED)."""
        if cls._whoop5 is None:
            cls._whoop5 = cls(
                kind=DeviceFamilyKind.WHOOP_5,
                service_uuid="fd4b0001-cce1-4033-93ce-002d5875f58a",
                cmd_char_uuid="fd4b0002-cce1-4033-93ce-002d5875f58a",
                cmd_notify_uuid="fd4b0003-cce1-4033-93ce-002d5875f58a",
                event_notify_uuid="fd4b0004-cce1-4033-93ce-002d5875f58a",
                data_notify_uuid="fd4b0005-cce1-4033-93ce-002d5875f58a",
                aux_notify_uuid="fd4b0007-cce1-4033-93ce-002d5875f58a",
                client_hello=bytes.fromhex(
                    "AA0108000001E67123019101363E5C8D"
                ),
            )
        return cls._whoop5

    CLIENT_HELLO: bytes = bytes.fromhex(
        "AA0108000001E67123019101363E5C8D"
    )

    @property
    def is_whoop4(self) -> bool:
        return self.kind == DeviceFamilyKind.WHOOP_4

    @property
    def is_whoop5(self) -> bool:
        return self.kind == DeviceFamilyKind.WHOOP_5

    @classmethod
    def from_service_uuid(cls, uuid: str) -> DeviceFamily | None:
        """Resolve the device family from a BLE service UUID.

        Returns None if the UUID doesn't match any known WHOOP service.
        """
        whoop4 = cls.WHOOP_4()
        whoop4_puffin = cls.WHOOP_4_PUFFIN()
        if uuid == whoop4.service_uuid or uuid == cls.WHOOP_4_ALT_SERVICE_UUID:
            return whoop4
        if uuid == whoop4_puffin.service_uuid:
            return whoop4_puffin
        if uuid == cls.WHOOP_5().service_uuid:
            return cls.WHOOP_5()
        return None

    def __repr__(self) -> str:
        return f"DeviceFamily({self.kind.value})"
