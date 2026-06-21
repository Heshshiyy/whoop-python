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
        """Trigger just-works bonding by writing SET_CLOCK to the CMD characteristic.

        WHOOP 4.0 requires bonding before it will respond to commands. The bond flow:
        1. Write to CMD char → strap rejects with "Insufficient Authentication"
        2. Windows BLE stack triggers just-works pairing (no PIN)
        3. Connection drops during pairing
        4. Reconnect (now bonded)
        5. Re-subscribe to notifications

        We deliberately use SET_CLOCK (not GET_BATTERY_LEVEL) here so that
        read_battery() can send GET_BATTERY_LEVEL fresh and get the strap's first
        response for that command — the strap ignores a second GET_BATTERY_LEVEL
        in the same connection.
        """
        from whoop.protocol.commands import Command
        if self._ble_client is None or self._family is None:
            return
        
        notify_uuid = self._family.cmd_notify_uuid
        char_uuid = self._family.cmd_char_uuid
        address = self._device.address if self._device else ""
        
        # Step 1: Subscribe to ALL proprietary notify characteristics.
        # 61080007 is present on WHOOP 4 straps as an aux notify channel —
        # subscribe to it too in case responses land there.
        _aux_uuids = [
            notify_uuid,
            self._family.event_notify_uuid,
            self._family.data_notify_uuid,
        ]
        # 61080007 (aux) — present on WHOOP 4 even though family.aux_notify_uuid=None
        _whoop4_aux = "61080007-8d6d-82b8-614a-1c8cb0f8dcc6"
        if self._family.aux_notify_uuid:
            _aux_uuids.append(self._family.aux_notify_uuid)
        elif self._family.is_whoop4:
            _aux_uuids.append(_whoop4_aux)

        subscribed_any = False
        for _uuid in _aux_uuids:
            try:
                await self._ble_client.start_notify(_uuid, self._on_notification)
                subscribed_any = True
                logger.debug("Subscribed to %s", _uuid)
            except Exception as e:
                logger.debug("Could not subscribe to %s: %s", _uuid, e)

        if not subscribed_any:
            logger.warning("Could not subscribe to any notify characteristic")
            return

        await asyncio.sleep(0.3)

        import time as _time
        _now = int(_time.time())
        _clock_payload = _now.to_bytes(4, "little") + b"\x00\x00\x00\x00"
        frame = Command.SET_CLOCK.build_frame(seq=0, payload=_clock_payload, family=self._family)

        for attempt in range(3):
            try:
                logger.debug("Bond attempt %d: writing to CMD char (write-without-response)...", attempt + 1)
                # Use write-without-response (response=False) — the WHOOP command
                # characteristic lists write-without-response FIRST in its properties,
                # meaning the firmware's command handler is on that ATT path.
                # response=True writes get BLE-layer ACKed but the app layer ignores them.
                await self._ble_client.write_gatt_char(char_uuid, frame, response=False)
                self._bonded = True
                logger.info("Bond established — command written (write-without-response)")
                return
            except Exception as e:
                msg = str(e)
                logger.debug("Bond attempt %d error: %s", attempt + 1, msg)

                if "Insufficient Authentication" in msg or "Insufficient Encryption" in msg:
                    logger.info("Bonding triggered — waiting for Windows pairing to complete...")
                    await asyncio.sleep(5.0)

                    try:
                        await self._ble_client.disconnect()
                    except Exception:
                        pass

                    logger.debug("Reconnecting after pairing...")
                    try:
                        self._ble_client = BleakClient(
                            address,
                            disconnected_callback=self._on_ble_disconnect,
                            timeout=15.0,
                        )
                        await self._ble_client.connect()
                        logger.debug("Reconnected — re-subscribing notify channels")
                        for _uuid in _aux_uuids:
                            try:
                                await self._ble_client.start_notify(_uuid, self._on_notification)
                            except Exception:
                                pass
                        await asyncio.sleep(0.3)

                        try:
                            await self._ble_client.write_gatt_char(char_uuid, frame, response=False)
                            self._bonded = True
                            logger.info("Bond established after reconnect")
                            return
                        except Exception as e2:
                            logger.debug("Write after reconnect failed: %s", e2)
                    except Exception as e3:
                        logger.debug("Reconnect failed: %s", e3)

                    continue

                elif "Protocol Error" in msg:
                    logger.debug("Protocol error — retrying in 2s")
                    await asyncio.sleep(2.0)
                    continue
                elif "disconnected" in msg.lower() or "not connected" in msg.lower():
                    logger.info("Connection lost during bonding — reconnecting...")
                    await asyncio.sleep(3.0)
                    try:
                        self._ble_client = BleakClient(
                            address,
                            disconnected_callback=self._on_ble_disconnect,
                            timeout=15.0,
                        )
                        await self._ble_client.connect()
                        for _uuid in _aux_uuids:
                            try:
                                await self._ble_client.start_notify(_uuid, self._on_notification)
                            except Exception:
                                pass
                        await asyncio.sleep(0.3)
                        continue
                    except Exception:
                        break
                else:
                    logger.warning("Unexpected bonding error: %s", e)
                    break

        logger.warning("Bonding did not complete after 3 attempts")

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

            result = await asyncio.wait_for(response_future, timeout=5.0)
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
        
        # Step 3: Run WHOOP 4.0 handshake sequence
        import time as _time
        now = int(_time.time())
        
        # SET_CLOCK (cmd 10) — sync phone time to strap (required before data flows)
        clock_payload = now.to_bytes(4, "little") + b"\x00\x00\x00\x00"
        resp = await self.send_command(
            Command.SET_CLOCK.build_frame(seq=1, payload=clock_payload, family=self._family)
        )
        if resp:
            logger.info("Clock synced")
        else:
            logger.debug("SET_CLOCK no response (non-fatal)")
        
        # STOP raw type-43 flood (cmd 63 with 0x00)
        resp = await self.send_command(
            Command.SEND_R10_R11_REALTIME.build_frame(seq=2, payload=b"\x00", family=self._family)
        )
        if resp:
            logger.info("Raw data flood stopped")
        
        # Start realtime HR streaming (cmd 3 with 0x01)
        resp = await self.send_command(
            Command.TOGGLE_REALTIME_HR.build_frame(seq=3, payload=b"\x01", family=self._family)
        )
        if resp:
            logger.info("Realtime HR streaming started")
        
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
