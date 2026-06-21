"""BLE client for WHOOP devices using Bleak (cross-platform).

Provides scan, connect, command/response, and realtime data streaming
for WHOOP 4.0 and WHOOP 5.0 straps.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Awaitable

from bleak import BleakScanner, BleakClient
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from whoop.protocol.device_family import DeviceFamily, DeviceFamilyKind
from whoop.protocol.frames import FrameReassembler, FrameEncoder
from whoop.protocol.parse_frame import parse_frame
from whoop.protocol.packet_types import PacketTypes

logger = logging.getLogger(__name__)

# Known WHOOP service UUIDs (both families + alt)
_WHOOP_SERVICE_UUIDS: set[str] = {
    DeviceFamily.WHOOP_4().service_uuid,
    DeviceFamily.WHOOP_4_ALT_SERVICE_UUID,
    DeviceFamily.WHOOP_5().service_uuid,
}

# CCCD UUID for notification subscription
_CCCD_UUID: str = "00002902-0000-1000-8000-00805f9b34fb"


class ConnectionState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTING = "disconnecting"


@dataclass
class ScannedDevice:
    """Result from a WHOOP BLE scan."""
    name: str
    address: str
    rssi: int
    family: DeviceFamilyKind


@dataclass
class BatteryInfo:
    """Battery level and charging status."""
    level: int = 0          # 0-100 percent
    is_charging: bool = False


# ---------------------------------------------------------------------------
# Reconnect backoff policy
# ---------------------------------------------------------------------------

class _Backoff:
    """Exponential backoff with jitter and a ceiling."""

    def __init__(self, base: float = 1.0, max_delay: float = 60.0) -> None:
        self._base = base
        self._max = max_delay
        self._attempt = 0

    def next_delay(self) -> float:
        self._attempt += 1
        import random
        raw = min(self._base * (2 ** (self._attempt - 1)), self._max)
        return raw * (0.5 + random.random())  # 50-150% jitter

    def reset(self) -> None:
        self._attempt = 0


# ---------------------------------------------------------------------------
# WhoopBleClient
# ---------------------------------------------------------------------------

class WhoopBleClient:
    """Asynchronous BLE client for WHOOP straps.

    Usage::

        client = WhoopBleClient()
        devices = await client.scan(timeout=5.0)
        if devices:
            ok = await client.connect(devices[0].address, devices[0].family)
            await client.start_realtime_hr()
            # ... data arrives via on_data ...
            await client.disconnect()
    """

    def __init__(self) -> None:
        self._ble_client: BleakClient | None = None
        self._device: BLEDevice | None = None
        self._family: DeviceFamily | None = None
        self._state: ConnectionState = ConnectionState.DISCONNECTED
        self._reassembler: FrameReassembler | None = None
        self._seq: int = 0
        self._backoff: _Backoff = _Backoff()
        self._auto_reconnect: bool = False
        self._bonded: bool = False
        self._reconnect_task: asyncio.Task[None] | None = None
        self._scan_filter_uuids: list[str] = []

        # Callbacks
        self.on_data: Callable[[ParsedFrame], None] | None = None
        self.on_state_change: Callable[[ConnectionState], None] | None = None
        self.on_disconnect: Callable[[BLEDevice | None], None] | None = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def is_connected(self) -> bool:
        return self._state == ConnectionState.CONNECTED

    @property
    def family(self) -> DeviceFamily | None:
        return self._family

    # ------------------------------------------------------------------
    # Scan
    # ------------------------------------------------------------------

    async def scan(self, timeout: float = 5.0) -> list[ScannedDevice]:
        """Scan for nearby WHOOP devices.

        Returns a list of discovered WHOOP straps ordered by RSSI (strongest first).
        """
        devices: list[ScannedDevice] = []

        def _on_detection(device: BLEDevice, adv_data: AdvertisementData) -> None:
            # Check service UUIDs in advertisement
            uuids = adv_data.service_uuids or []
            matched: DeviceFamily | None = None
            for u in uuids:
                matched = DeviceFamily.from_service_uuid(u)
                if matched is not None:
                    break

            if matched is None:
                return

            name = adv_data.local_name or device.name or device.address
            devices.append(ScannedDevice(
                name=name,
                address=device.address,
                rssi=adv_data.rssi if adv_data.rssi is not None else 0,
                family=matched.kind,
            ))

        scanner = BleakScanner(
            detection_callback=_on_detection,
            service_uuids=list(_WHOOP_SERVICE_UUIDS),
        )
        await scanner.start()
        await asyncio.sleep(timeout)
        await scanner.stop()

        devices.sort(key=lambda d: d.rssi, reverse=True)
        return devices

    # ------------------------------------------------------------------
    # Connect / Disconnect
    # ------------------------------------------------------------------

    async def connect(
        self,
        address: str,
        family: DeviceFamilyKind,
        timeout: float = 15.0,
        auto_reconnect: bool = False,
    ) -> bool:
        """Connect to a WHOOP strap at *address*.

        Returns True on success, False on failure.
        """
        self._auto_reconnect = auto_reconnect

        if family == DeviceFamilyKind.WHOOP_4:
            self._family = DeviceFamily.WHOOP_4()
        else:
            self._family = DeviceFamily.WHOOP_5()

        self._reassembler = FrameReassembler(self._family)
        self._reassembler.on_frame = self._on_frame_received

        self._set_state(ConnectionState.CONNECTING)

        try:
            # Use address string directly — idiomatic WinRT path (find_device_by_address
            # internally uses FromBluetoothAddressAsync which sets up the session properly).
            # BLEDevice(details=None) works on newer Bleak but address-string is safer.
            self._ble_client = BleakClient(
                address,
                disconnected_callback=self._on_ble_disconnect,
                timeout=timeout,
            )
            await self._ble_client.connect()
            # Stash a lightweight device reference (for reconnect).
            self._device = BLEDevice(address=address, name="WHOOP", details=None, rssi=0)
            
            self._bonded = False
            
            self._set_state(ConnectionState.CONNECTED)
            self._backoff.reset()
            logger.info("Connected to %s (family=%s)", address, family.value)
            
            # Trigger bonding — WHOOP 4.0 requires just-works pairing before accepting commands
            await self._trigger_bond()
            
            return True
        except Exception as exc:
            logger.error("Connection failed: %s", exc)
            self._set_state(ConnectionState.DISCONNECTED)
            return False

    async def _trigger_bond(self) -> None:
        """Subscribe to all notify channels and open the WHOOP CMD channel.

        The correct first command per handshake.py is GET_HELLO_HARVARD (35).
        Sending anything else first (e.g. SET_CLOCK) causes the strap to silently
        ignore all subsequent commands. If the strap rejects with
        Insufficient Authentication, Windows triggers just-works pairing, we
        reconnect, then re-send.

        Per-channel callbacks log every raw notification byte so we can see the
        strap's hello response.
        """
        from whoop.protocol.commands import Command
        if self._ble_client is None or self._family is None:
            return

        char_uuid = self._family.cmd_char_uuid
        address = self._device.address if self._device else ""

        _whoop4_aux = "61080007-8d6d-82b8-614a-1c8cb0f8dcc6"
        _channel_uuids: list[tuple[str, str]] = [
            (self._family.cmd_notify_uuid,   "CMD "),
            (self._family.event_notify_uuid, "EVT "),
            (self._family.data_notify_uuid,  "DATA"),
            (self._family.aux_notify_uuid or _whoop4_aux, "AUX "),
        ]

        def _make_channel_cb(tag: str):
            """Per-channel callback: logs raw bytes then feeds the reassembler."""
            def _cb(_sender: int, data: bytearray) -> None:
                logger.info("NOTIFY [%s] %d bytes: %s", tag, len(data), bytes(data).hex())
                self._on_notification(_sender, data)
            return _cb

        async def _subscribe_all() -> bool:
            subscribed = 0
            for _uuid, _tag in _channel_uuids:
                try:
                    await self._ble_client.start_notify(_uuid, _make_channel_cb(_tag))  # type: ignore[union-attr]
                    subscribed += 1
                    logger.debug("Subscribed to %s (%s)", _tag.strip(), _uuid)
                except Exception as e:
                    logger.debug("Could not subscribe to %s: %s", _uuid, e)
            return subscribed > 0

        if not await _subscribe_all():
            logger.warning("Could not subscribe to any notify characteristic")
            return

        # Wait for CCCD writes to settle before sending the first command.
        await asyncio.sleep(0.5)

        # GET_HELLO_HARVARD is the mandated first command — it must arrive before
        # any other write or the strap ignores all commands.
        frame = Command.GET_HELLO_HARVARD.build_frame(seq=0, payload=b"", family=self._family)

        for attempt in range(3):
            try:
                logger.debug("Bond attempt %d: writing GET_HELLO_HARVARD (write-without-response)...", attempt + 1)
                await self._ble_client.write_gatt_char(char_uuid, frame, response=False)
                self._bonded = True
                logger.info("Bond attempt %d: GET_HELLO_HARVARD written — watching for hello response...", attempt + 1)
                # Give the strap up to 2 s to send its hello reply on CMD notify.
                await asyncio.sleep(2.0)
                return
            except Exception as e:
                msg = str(e)
                logger.debug("Bond attempt %d error: %s", attempt + 1, msg)

                if "Insufficient Authentication" in msg or "Insufficient Encryption" in msg:
                    logger.info("Bonding triggered — waiting for Windows pairing...")
                    await asyncio.sleep(5.0)
                    try:
                        await self._ble_client.disconnect()
                    except Exception:
                        pass
                    try:
                        self._ble_client = BleakClient(
                            address,
                            disconnected_callback=self._on_ble_disconnect,
                            timeout=15.0,
                        )
                        await self._ble_client.connect()
                        await _subscribe_all()
                        await asyncio.sleep(0.5)
                        await self._ble_client.write_gatt_char(char_uuid, frame, response=False)
                        self._bonded = True
                        logger.info("Bond established after reconnect")
                        await asyncio.sleep(2.0)
                        return
                    except Exception as e3:
                        logger.debug("Reconnect/write failed: %s", e3)
                    continue

                elif "Protocol Error" in msg:
                    await asyncio.sleep(2.0)
                    continue
                elif "disconnected" in msg.lower() or "not connected" in msg.lower():
                    await asyncio.sleep(3.0)
                    try:
                        self._ble_client = BleakClient(
                            address,
                            disconnected_callback=self._on_ble_disconnect,
                            timeout=15.0,
                        )
                        await self._ble_client.connect()
                        await _subscribe_all()
                        await asyncio.sleep(0.5)
                        continue
                    except Exception:
                        break
                else:
                    logger.warning("Unexpected bond error: %s", e)
                    break

        logger.warning("Bond sequence did not complete after 3 attempts")

    async def disconnect(self) -> None:
        """Disconnect from the strap gracefully."""
        self._auto_reconnect = False
        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
            self._reconnect_task = None

        self._set_state(ConnectionState.DISCONNECTING)
        if self._ble_client is not None and self._ble_client.is_connected:
            try:
                await self._ble_client.disconnect()
            except Exception as exc:
                logger.warning("Error during disconnect: %s", exc)
        self._set_state(ConnectionState.DISCONNECTED)
        self._ble_client = None
        self._device = None
        self._reassembler = None

    # ------------------------------------------------------------------
    # Command send
    # ------------------------------------------------------------------

    async def send_command(self, frame: bytes) -> bytes | None:
        """Send a raw command frame and await the response.

        Does NOT stop/start notify — keeps the existing _on_notification
        subscription intact and routes the decoded response through the
        FrameReassembler via a temporary on_frame swap.

        Returns the decoded inner-frame bytes, or None on failure/timeout.
        """
        if self._ble_client is None or not self._ble_client.is_connected:
            logger.error("Cannot send command: not connected")
            return None

        if self._family is None:
            logger.error("Cannot send command: no device family")
            return None

        char_uuid = self._family.cmd_char_uuid

        response_future: asyncio.Future[bytes] = asyncio.get_event_loop().create_future()

        def _on_frame_capture(inner: bytes) -> None:
            logger.debug("Reassembler produced frame: %d bytes", len(inner))
            if not response_future.done():
                response_future.set_result(inner)

        original_on_frame = self._reassembler.on_frame if self._reassembler else None
        if self._reassembler:
            self._reassembler.on_frame = _on_frame_capture

        try:
            # Write-without-response first: WHOOP 4 command char lists this path
            # first in properties, and the firmware command handler is on this path.
            # response=True gets BLE-ACKed but the strap never fires a notification reply.
            try:
                await self._ble_client.write_gatt_char(char_uuid, frame, response=False)
                logger.debug("Command written (write-without-response)")
            except Exception as e:
                logger.debug("Write without response failed (%s), retrying with response...", e)
                await self._ble_client.write_gatt_char(char_uuid, frame, response=True)

            result = await asyncio.wait_for(response_future, timeout=2.0)
            logger.debug("Command response captured: %d bytes", len(result))
            return result
        except asyncio.TimeoutError:
            logger.debug("Command response timeout (write landed, no notify reply)")
            return None
        except Exception as exc:
            logger.error("Command failed: %s", exc)
            return None
        finally:
            if self._reassembler:
                self._reassembler.on_frame = original_on_frame

    # ------------------------------------------------------------------
    # Realtime HR
    # ------------------------------------------------------------------

    async def start_realtime_hr(self) -> bool:
        """Complete WHOOP handshake and start realtime HR streaming.
        
        Performs the full handshake sequence required for WHOOP 4.0 to begin
        sending type-40 REALTIME_DATA frames on the data notify channel.
        """
        if self._ble_client is None or not self._ble_client.is_connected:
            return False
        if self._family is None:
            return False
        
        from whoop.protocol.commands import Command
        
        # Step 1: Subscribe to standard BLE Heart Rate (0x2A37)
        try:
            await self._ble_client.start_notify(
                "00002a37-0000-1000-8000-00805f9b34fb",
                self._on_std_hr,
            )
            logger.info("Standard HR notifications enabled (0x2A37)")
        except Exception as exc:
            logger.debug("Standard HR notify failed (non-fatal): %s", exc)
        
        # Step 2: Subscribe to WHOOP event, data, and aux channels
        _whoop_notify_uuids = [
            self._family.event_notify_uuid,
            self._family.data_notify_uuid,
        ]
        if self._family.aux_notify_uuid:
            _whoop_notify_uuids.append(self._family.aux_notify_uuid)
        elif self._family.is_whoop4:
            # 61080007 is present on WHOOP 4 straps even though aux_notify_uuid=None
            _whoop_notify_uuids.append("61080007-8d6d-82b8-614a-1c8cb0f8dcc6")

        subscribed = 0
        for _uuid in _whoop_notify_uuids:
            try:
                await self._ble_client.start_notify(_uuid, self._on_notification)
                subscribed += 1
            except Exception as exc:
                logger.debug("Could not subscribe to %s (non-fatal): %s", _uuid, exc)

        if subscribed == 0:
            logger.error("Failed to start any WHOOP proprietary notifications")
            return False
        logger.info("WHOOP proprietary notifications enabled (%d channels)", subscribed)
        
        # Step 3: Full WHOOP 4.0 handshake — correct order per handshake.py.
        # GET_HELLO_HARVARD was already sent in _trigger_bond(); the strap needs
        # the remaining sequence to open the data stream.
        import time as _time
        from whoop.protocol.handshake import build_clock_payload
        now = int(_time.time())

        async def _cmd(seq: int, frame: bytes, desc: str) -> bytes | None:
            resp = await self.send_command(frame)
            if resp:
                logger.info("Handshake ack: %s (%d bytes) %s", desc, len(resp), resp.hex())
            else:
                logger.debug("Handshake no-response: %s (non-fatal)", desc)
            return resp

        seq = 1
        # GET_ADVERTISING_NAME_HARVARD — ask strap for its BLE name
        await _cmd(seq, Command.GET_ADVERTISING_NAME_HARVARD.build_frame(
            seq=seq, payload=b"", family=self._family), "GET_ADV_NAME")
        seq += 1

        # SET_CLOCK — sync time
        await _cmd(seq, Command.SET_CLOCK.build_frame(
            seq=seq, payload=build_clock_payload(now), family=self._family), "SET_CLOCK")
        seq += 1

        # GET_CLOCK — confirm time sync
        await _cmd(seq, Command.GET_CLOCK.build_frame(
            seq=seq, payload=b"", family=self._family), "GET_CLOCK")
        seq += 1

        # STOP raw type-43 sensor flood (0x00 = stop)
        await _cmd(seq, Command.SEND_R10_R11_REALTIME.build_frame(
            seq=seq, payload=b"\x00", family=self._family), "STOP_RAW")
        seq += 1

        # GET_DATA_RANGE — ask strap what historical data it has stored
        resp = await _cmd(seq, Command.GET_DATA_RANGE.build_frame(
            seq=seq, payload=b"", family=self._family), "GET_DATA_RANGE")
        if resp and len(resp) >= 11:
            import struct
            range_start = struct.unpack_from("<I", resp, 3)[0]
            range_end   = struct.unpack_from("<I", resp, 7)[0]
            logger.info("Strap data range: %d → %d (%d s)", range_start, range_end, range_end - range_start)
        seq += 1

        # TOGGLE_REALTIME_HR — start live HR stream (0x01 = start)
        await _cmd(seq, Command.TOGGLE_REALTIME_HR.build_frame(
            seq=seq, payload=b"\x01", family=self._family), "TOGGLE_REALTIME_HR")

        return True

    def _on_std_hr(self, _sender: int, data: bytearray) -> None:
        """Handle standard BLE HR Measurement (0x2A37) — non-enveloped format."""
        raw = bytes(data)
        if len(raw) < 2:
            return
        try:
            flags = raw[0]
            hr_fmt_16 = (flags & 0x01) != 0
            hr = (raw[2] << 8) | raw[1] if hr_fmt_16 and len(raw) >= 3 else raw[1]
            
            rr_intervals = []
            idx = 2 if not hr_fmt_16 else 3
            if (flags & 0x10) != 0:  # Energy present
                idx += 2
            if (flags & 0x20) != 0:  # RR present
                while idx + 1 < len(raw):
                    rr = (raw[idx + 1] << 8) | raw[idx]
                    rr_ms = round(rr * 1000 / 1024)
                    rr_intervals.append(rr_ms)
                    idx += 2
            
            logger.debug("Std HR: %d bpm, %d RR intervals", hr, len(rr_intervals))
            
            if self.on_data:
                # Emit as simple dict for the record callback
                self.on_data({"heart_rate": hr, "rr_intervals": rr_intervals})
        except Exception as e:
            logger.debug("Std HR parse error: %s", e)

    async def start_data_stream(self) -> bool:
        """Subscribe to the WHOOP data notification channel.

        Returns True on success.
        """
        if self._ble_client is None or not self._ble_client.is_connected:
            return False
        if self._family is None:
            return False

        try:
            await self._ble_client.start_notify(
                self._family.data_notify_uuid,
                self._on_notification,
            )
            logger.info("Data stream notifications enabled")
            return True
        except Exception as exc:
            logger.error("Failed to start data stream: %s", exc)
            return False

    async def stop_realtime_hr(self) -> None:
        """Unsubscribe from HR notifications."""
        if self._ble_client and self._family:
            try:
                await self._ble_client.stop_notify(self._family.event_notify_uuid)
            except Exception:
                pass

    async def stop_data_stream(self) -> None:
        """Unsubscribe from data stream notifications."""
        if self._ble_client and self._family:
            try:
                await self._ble_client.stop_notify(self._family.data_notify_uuid)
            except Exception:
                pass

    async def read_battery(self) -> BatteryInfo | None:
        """Read battery level via the WHOOP proprietary GET_BATTERY_LEVEL command.

        The diagnostic (whoop_ble_diag.py) confirmed:
        - 0x2A19 uncached read returns 10% while the WHOOP app shows 50% — it is
          NOT the real battery source on WHOOP 4.
        - WinRT routing works (0x2A37 HR delivers fine).
        - 61080003/0004/0005/0007 are completely silent when written with response=True.

        Primary fix applied here and in send_command: use write-without-response
        (response=False) — the WHOOP 4 command characteristic lists that write type
        first, meaning the firmware command handler is on that ATT path.

        Battery parsing: the GET_BATTERY_LEVEL (cmd 26) response arrives as a
        CommandResponse inner record. We log the full raw bytes on first run so we
        can identify the exact offset; best guesses are tried in order.
        Falls back to 0x2A19 read if no proprietary response arrives.
        """
        if not self._ble_client or not self._ble_client.is_connected:
            return None
        if not self._family:
            return None

        from whoop.protocol.commands import Command

        # ── Primary: send GET_BATTERY_LEVEL, capture response ───────────────
        # Ensure all proprietary notify channels are subscribed so the response
        # is routed through the reassembler regardless of which UUID carries it.
        _notify_uuids = [
            self._family.cmd_notify_uuid,
            self._family.event_notify_uuid,
            self._family.data_notify_uuid,
        ]
        _whoop4_aux = "61080007-8d6d-82b8-614a-1c8cb0f8dcc6"
        if self._family.aux_notify_uuid:
            _notify_uuids.append(self._family.aux_notify_uuid)
        elif self._family.is_whoop4:
            _notify_uuids.append(_whoop4_aux)

        for _uuid in _notify_uuids:
            try:
                await self._ble_client.start_notify(_uuid, self._on_notification)
            except Exception:
                pass  # already subscribed or not present — non-fatal

        # Build GET_BATTERY_LEVEL frame and send via send_command (which uses
        # write-without-response now that the write order has been corrected).
        self._seq = (self._seq + 1) & 0xFF
        batt_frame = Command.GET_BATTERY_LEVEL.build_frame(
            seq=self._seq, payload=b"", family=self._family
        )

        raw_inner = await self.send_command(batt_frame)

        if raw_inner is not None:
            logger.debug("GET_BATTERY_LEVEL response raw: %s", raw_inner.hex())
            print(f"  [battery] GET_BATTERY_LEVEL response ({len(raw_inner)} bytes): {raw_inner.hex()}")
            # Parse: inner = [type, seq, cmd, payload...]
            # Try the most common offsets for battery % in WHOOP responses.
            # Log everything so we can identify the right offset.
            for offset in (3, 4, 6, 7):
                if offset < len(raw_inner):
                    val = raw_inner[offset]
                    print(f"  [battery] offset {offset} = {val} (0x{val:02x})")
                    if 0 < val <= 100:
                        logger.info("Battery from GET_BATTERY_LEVEL (offset %d): %d%%", offset, val)
                        return BatteryInfo(level=val, is_charging=False)
            # If no plausible value found at common offsets, log and fall through.
            logger.warning("GET_BATTERY_LEVEL response received but no value 1-100 found at offsets 3/4/6/7")

        # ── Fallback: 0x2A19 direct read ────────────────────────────────────
        # Known to return a stale/strap-internal value that doesn't match the
        # WHOOP app, but better than returning None.
        BATT_UUID = "00002a19-0000-1000-8000-00805f9b34fb"
        try:
            data = await self._ble_client.read_gatt_char(BATT_UUID, use_cached=False)
            if data and len(data) > 0:
                level = data[0]
                logger.debug("Battery (0x2A19 fallback): %d%%", level)
                print(f"  [battery] 0x2A19 fallback: {level}%")
                return BatteryInfo(level=level, is_charging=False)
        except Exception as e:
            logger.debug("Battery 0x2A19 read failed: %s", e)

        return None

    # ------------------------------------------------------------------
    # Historical data offload
    # ------------------------------------------------------------------

    async def get_history(
        self,
        start_ts: int,
        end_ts: int | None = None,
        timeout: float = 60.0,
    ) -> list:
        """Offload historical records stored on the strap for a time window.

        Performs a minimal handshake (STOP_RAW to confirm the CMD channel is
        alive), then sends SEND_HISTORICAL_DATA and collects type-47 frames via
        the FrameReassembler until the strap sends a METADATA HISTORY_END/COMPLETE
        frame, then ACKs with HISTORICAL_DATA_RESULT.

        Returns a list of HistoricalRecord dataclass instances.
        NOTE: requires the CMD channel to be open — i.e. connect() must have
        been called (which sends GET_HELLO_HARVARD in _trigger_bond).
        """
        import struct
        import time as _time
        from whoop.protocol.commands import Command
        from whoop.protocol.parse_frame import parse_frame
        from whoop.protocol.parsed_frame import (
            CommandResponse, HistoricalDataFrame, Metadata, MetadataKind,
        )
        from whoop.protocol.handshake import build_ack_payload

        if not self._ble_client or not self._ble_client.is_connected:
            logger.error("get_history: not connected")
            return []
        if not self._family or not self._reassembler:
            return []

        if end_ts is None:
            end_ts = int(_time.time())

        char_uuid = self._family.cmd_char_uuid

        # Ensure all proprietary channels are subscribed so we receive
        # type-47 historical data frames on whichever UUID the strap uses.
        _notify_uuids = [
            self._family.cmd_notify_uuid,
            self._family.event_notify_uuid,
            self._family.data_notify_uuid,
            "61080007-8d6d-82b8-614a-1c8cb0f8dcc6",
        ]
        for _uuid in _notify_uuids:
            try:
                await self._ble_client.start_notify(_uuid, self._on_notification)
            except Exception:
                pass

        records: list = []
        done_event: asyncio.Event = asyncio.Event()
        frame_count: list[int] = [0]  # mutable counter for inner closure

        # Swap the reassembler callback so we collect every frame directly.
        original_on_frame = self._reassembler.on_frame

        def _collect(inner: bytes) -> None:
            frame_count[0] += 1
            logger.info(
                "get_history frame #%d: %d bytes %s",
                frame_count[0], len(inner), inner[:8].hex(),
            )
            parsed = parse_frame(inner, self._family)  # type: ignore[arg-type]
            if parsed is None:
                logger.debug("get_history: parse_frame returned None for inner %s", inner[:8].hex())
                return
            logger.debug("get_history parsed: %s", type(parsed).__name__)
            if isinstance(parsed, HistoricalDataFrame):
                batch = parsed.records
                records.extend(batch)
                logger.info(
                    "Historical record batch: %d records (total %d)",
                    len(batch), len(records),
                )
            elif isinstance(parsed, Metadata):
                logger.info("Metadata: %s", parsed.metadata_kind)
                if parsed.metadata_kind in (
                    MetadataKind.HISTORY_END,
                    MetadataKind.HISTORY_COMPLETE,
                ):
                    logger.info("History transfer complete — %d records", len(records))
                    done_event.set()
            elif isinstance(parsed, CommandResponse):
                logger.info(
                    "CMD response during history: cmd=%d code=%d payload=%s",
                    parsed.cmd, parsed.response_code, parsed.response_payload.hex(),
                )
            # Also forward to the normal handler so on_data still fires.
            if original_on_frame is not None:
                original_on_frame(inner)

        self._reassembler.on_frame = _collect

        try:
            # Step 0: SET_CLOCK — strap requires time sync before historical offload
            import time as _time
            from whoop.protocol.handshake import build_clock_payload
            now = int(_time.time())
            self._seq = (self._seq + 1) & 0xFF
            clock_frame = Command.SET_CLOCK.build_frame(
                seq=self._seq, payload=build_clock_payload(now), family=self._family
            )
            resp = await self.send_command(clock_frame)
            logger.info("get_history: SET_CLOCK %s", "ack" if resp else "no response (non-fatal)")
            
            # Step 1: STOP_RAW — confirms CMD channel is active and stops any
            # ongoing type-43 raw sensor flood before requesting history.
            self._seq = (self._seq + 1) & 0xFF
            stop_frame = Command.SEND_R10_R11_REALTIME.build_frame(
                seq=self._seq, payload=b"\x00", family=self._family
            )
            resp = await self.send_command(stop_frame)
            if resp:
                logger.info("get_history: STOP_RAW acknowledged")
            else:
                logger.info("get_history: sent STOP_RAW (no ack) — waiting 2s")
            await asyncio.sleep(2.0)

            # Step 2: SEND_HISTORICAL_DATA — request the time window.
            #   Payload format: start_ts (u32 LE) + end_ts (u32 LE) = 8 bytes.
            #   The strap responds with a stream of type-47 HistoricalDataFrame
            #   notifications followed by a type-49 Metadata(HISTORY_END) frame.
            self._seq = (self._seq + 1) & 0xFF
            hist_payload = struct.pack("<II", start_ts, end_ts)
            hist_frame = Command.SEND_HISTORICAL_DATA.build_frame(
                seq=self._seq, payload=hist_payload, family=self._family
            )
            await self._ble_client.write_gatt_char(char_uuid, hist_frame, response=True)
            logger.info(
                "get_history: sent SEND_HISTORICAL_DATA start=%d end=%d (window=%ds)",
                start_ts, end_ts, end_ts - start_ts,
            )

            # Step 3: Wait for HISTORY_END/COMPLETE metadata frame.
            try:
                await asyncio.wait_for(done_event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    "get_history timed out after %.0fs — "
                    "received %d frames, %d records",
                    timeout, frame_count[0], len(records),
                )

            # Step 4: ACK — tell strap we received data up to end_ts.
            if records:
                ack_payload = build_ack_payload(end_ts)
                self._seq = (self._seq + 1) & 0xFF
                ack_frame = Command.HISTORICAL_DATA_RESULT.build_frame(
                    seq=self._seq, payload=ack_payload, family=self._family
                )
                await self._ble_client.write_gatt_char(char_uuid, ack_frame, response=True)
                logger.info("get_history: sent HISTORICAL_DATA_RESULT ACK")

            return records

        finally:
            self._reassembler.on_frame = original_on_frame

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _set_state(self, new_state: ConnectionState) -> None:
        old = self._state
        self._state = new_state
        if old != new_state and self.on_state_change is not None:
            try:
                self.on_state_change(new_state)
            except Exception:
                pass

    def _on_notification(self, _sender: int, data: bytearray) -> None:
        """Handle incoming BLE notification bytes."""
        if self._reassembler is not None:
            self._reassembler.feed(bytes(data))

    def _on_frame_received(self, inner: bytes) -> None:
        """Called by FrameReassembler when a complete inner record is extracted."""
        if self.on_data is None or self._family is None:
            return
        try:
            parsed = parse_frame(inner, self._family)
            self.on_data(parsed)
        except Exception as exc:
            logger.debug("Failed to parse frame: %s", exc)

    def _on_ble_disconnect(self, device: BLEDevice | None) -> None:
        """Bleak callback: the BLE connection dropped."""
        logger.warning("BLE disconnected: %s", device.address if device else "unknown")
        self._set_state(ConnectionState.DISCONNECTED)

        if self.on_disconnect is not None:
            try:
                self.on_disconnect(device)
            except Exception:
                pass

        if self._auto_reconnect and self._device is not None and self._family is not None:
            self._reconnect_task = asyncio.create_task(self._auto_reconnect_loop())

    async def _auto_reconnect_loop(self) -> None:
        """Background task that attempts reconnection with backoff."""
        while self._auto_reconnect:
            delay = self._backoff.next_delay()
            logger.info("Reconnecting in %.1fs (attempt %d)...", delay, self._backoff._attempt)
            await asyncio.sleep(delay)

            if not self._auto_reconnect:
                break  # cancelled during sleep

            if self._device is None or self._family is None:
                break

            self._set_state(ConnectionState.CONNECTING)
            try:
                self._ble_client = BleakClient(
                    self._device,
                    disconnected_callback=self._on_ble_disconnect,
                )
                await self._ble_client.connect()
                self._set_state(ConnectionState.CONNECTED)
                self._backoff.reset()
                logger.info("Reconnected successfully")
                return
            except Exception as exc:
                logger.warning("Reconnect failed: %s", exc)
                self._set_state(ConnectionState.DISCONNECTED)


# Import at bottom to avoid circular dependency with whoop.protocol
from whoop.protocol.parsed_frame import ParsedFrame  # noqa: E402, F811
