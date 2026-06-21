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
            device = BLEDevice(address=address, name="WHOOP", details=None, rssi=0)
            self._device = device
            self._ble_client = BleakClient(
                device,
                disconnected_callback=self._on_ble_disconnect,
                timeout=timeout,
            )
            await self._ble_client.connect()
            
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
        """Trigger just-works bonding by writing GET_BATTERY_LEVEL to the CMD characteristic.
        
        WHOOP 4.0 requires bonding before it will respond to commands. The bond flow:
        1. Write to CMD char → strap rejects with "Insufficient Authentication"
        2. Windows BLE stack triggers just-works pairing (no PIN)
        3. Connection drops during pairing
        4. Reconnect (now bonded)
        5. Re-subscribe to notifications
        """
        from whoop.protocol.commands import Command
        if self._ble_client is None or self._family is None:
            return
        
        notify_uuid = self._family.cmd_notify_uuid
        char_uuid = self._family.cmd_char_uuid
        address = self._device.address if self._device else ""
        
        # Step 1: Subscribe to notifications and trigger bond
        try:
            await self._ble_client.start_notify(notify_uuid, self._on_notification)
            await asyncio.sleep(0.5)
            logger.debug("Subscribed to CMD notify")
        except Exception as e:
            logger.warning("Could not subscribe to notify: %s", e)
            return
        
        frame = Command.GET_BATTERY_LEVEL.build_frame(seq=0, payload=b"", family=self._family)
        
        for attempt in range(3):
            try:
                logger.debug("Bond attempt %d: writing to CMD char...", attempt + 1)
                await self._ble_client.write_gatt_char(char_uuid, frame, response=True)
                self._bonded = True
                logger.info("Bond established — command accepted")
                return
            except Exception as e:
                msg = str(e)
                logger.debug("Bond attempt %d error: %s", attempt + 1, msg)
                
                if "Insufficient Authentication" in msg or "Insufficient Encryption" in msg:
                    logger.info("Bonding triggered — waiting for Windows pairing to complete...")
                    await asyncio.sleep(5.0)
                    
                    # Connection may have dropped — try to reconnect
                    try:
                        await self._ble_client.disconnect()
                    except Exception:
                        pass
                    
                    logger.debug("Reconnecting after pairing...")
                    try:
                        self._ble_client = BleakClient(
                            self._device or address,
                            disconnected_callback=self._on_ble_disconnect,
                            timeout=15.0,
                        )
                        await self._ble_client.connect()
                        logger.debug("Reconnected — re-subscribing notify")
                        await self._ble_client.start_notify(notify_uuid, self._on_notification)
                        await asyncio.sleep(0.5)
                        
                        # Try the write again on the new connection
                        try:
                            await self._ble_client.write_gatt_char(char_uuid, frame, response=True)
                            self._bonded = True
                            logger.info("Bond established after reconnect")
                            return
                        except Exception as e2:
                            logger.debug("Write after reconnect failed: %s", e2)
                    except Exception as e3:
                        logger.debug("Reconnect failed: %s", e3)
                    
                    continue  # Retry
                    
                elif "Protocol Error" in msg:
                    logger.debug("Protocol error — retrying in 2s")
                    await asyncio.sleep(2.0)
                    continue
                elif "disconnected" in msg.lower() or "not connected" in msg.lower():
                    logger.info("Connection lost during bonding — reconnecting...")
                    await asyncio.sleep(3.0)
                    try:
                        self._ble_client = BleakClient(
                            self._device or address,
                            disconnected_callback=self._on_ble_disconnect,
                            timeout=15.0,
                        )
                        await self._ble_client.connect()
                        await self._ble_client.start_notify(notify_uuid, self._on_notification)
                        await asyncio.sleep(0.5)
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

        Returns the response payload bytes, or None on failure.
        """
        if self._ble_client is None or not self._ble_client.is_connected:
            logger.error("Cannot send command: not connected")
            return None

        if self._family is None:
            logger.error("Cannot send command: no device family")
            return None

        char_uuid = self._family.cmd_char_uuid
        notify_uuid = self._family.cmd_notify_uuid

        # Queue to capture the response — raw bytes from CMD notify
        response_future: asyncio.Future[bytes] = asyncio.get_event_loop().create_future()
        raw_chunks: list[bytes] = []
        capture_ready: bool = False  # Set True after command write, skip stale
        
        def _capture_response(_sender: int, data: bytearray) -> None:
            raw = bytes(data)
            logger.debug("CMD notify raw: %d bytes: %s", len(raw), raw.hex()[:80])
            if not capture_ready:
                logger.debug("Discarding stale notification (pre-command)")
                return
            raw_chunks.append(raw)
            if not response_future.done():
                accumulated = b"".join(raw_chunks)
                if self._reassembler:
                    self._reassembler.feed(accumulated)
                def _set_result():
                    if not response_future.done():
                        response_future.set_result(accumulated)
                asyncio.get_event_loop().call_later(0.2, _set_result)
            else:
                if self._reassembler:
                    self._reassembler.feed(bytes(data))
        
        def _on_frame_capture(inner: bytes) -> None:
            logger.debug("Reassembler produced frame: %d bytes", len(inner))
            if not response_future.done():
                response_future.set_result(inner)

        original_on_frame = self._reassembler.on_frame if self._reassembler else None
        if self._reassembler:
            self._reassembler.on_frame = _on_frame_capture

        try:
            # Subscribe to cmd notify with our capture callback
            # Stop first — Bleak requires unsubscribing before changing callback
            try:
                await self._ble_client.stop_notify(notify_uuid)
            except Exception:
                pass
            await self._ble_client.start_notify(notify_uuid, _capture_response)
            await asyncio.sleep(0.5)
            logger.debug("Re-subscribed CMD notify for command capture")
            
            # Flush stale notifications (residual data from bonding)
            await asyncio.sleep(0.2)
            capture_ready = True
            logger.debug("Now capturing fresh command response")
            
            # Write command
            try:
                await self._ble_client.write_gatt_char(char_uuid, frame, response=True)
                logger.debug("Command written successfully")
            except Exception as e:
                logger.debug("Write with response failed (%s), trying without...", e)
                await self._ble_client.write_gatt_char(char_uuid, frame, response=False)

            # Wait for fresh response
            result = await asyncio.wait_for(response_future, timeout=10.0)
            logger.debug("Command response captured: %d bytes", len(result))
            return result
        except asyncio.TimeoutError:
            logger.warning("Command response timeout")
            return None
        except Exception as exc:
            logger.error("Command failed: %s", exc)
            return None
        finally:
            # Restore original notification handler
            if self._ble_client and self._ble_client.is_connected:
                try:
                    await self._ble_client.stop_notify(notify_uuid)
                except Exception:
                    pass
                try:
                    await self._ble_client.start_notify(notify_uuid, self._on_notification)
                except Exception:
                    pass
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
        
        # Step 2: Subscribe to WHOOP event and data channels
        try:
            await self._ble_client.start_notify(
                self._family.event_notify_uuid,
                self._on_notification,
            )
            await self._ble_client.start_notify(
                self._family.data_notify_uuid,
                self._on_notification,
            )
            logger.info("WHOOP event/data notifications enabled")
        except Exception as exc:
            logger.error("Failed to start WHOOP notifications: %s", exc)
            return False
        
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

    async def _wake_battery_reporting(self) -> None:
        """Wake the strap's battery reporting by cycling CMD notify + writing a command.
        
        On WHOOP 4.0, the standard BLE Battery Service (0x2A19) returns a stale
        value (usually 100%) until:
        1. The CMD notify subscription is cycled (stop → start)
        2. A command is written to the CMD characteristic (write_gatt_char)
        
        Both steps are required. Discovered empirically: commit 01034b4 (which
        did both) showed 18% correct; all other commits (missing one step)
        showed 100% stale.
        """
        if not self._ble_client or not self._family:
            return
        
        from whoop.protocol.commands import Command
        
        # Step 1: Cycle CMD notify subscription
        try:
            await self._ble_client.stop_notify(self._family.cmd_notify_uuid)
        except Exception:
            pass
        try:
            await self._ble_client.start_notify(
                self._family.cmd_notify_uuid, self._on_notification
            )
            await asyncio.sleep(0.3)
        except Exception as e:
            logger.debug("Battery notify cycle failed: %s", e)
            return
        
        # Step 2: Write a command to wake battery reporting
        # GET_BATTERY_LEVEL triggers the strap to update its battery value
        try:
            frame = Command.GET_BATTERY_LEVEL.build_frame(
                seq=0, payload=b"", family=self._family
            )
            await self._ble_client.write_gatt_char(
                self._family.cmd_char_uuid, frame, response=True
            )
            await asyncio.sleep(0.3)
        except Exception as e:
            logger.debug("Battery wake command failed (non-fatal): %s", e)

    async def read_battery(self) -> BatteryInfo | None:
        """Read battery level from standard BLE Battery Service (0x2A19).
        
        Requires a CMD notify subscription cycle to wake the battery reporting
        (see _wake_battery_reporting).
        """
        if not self._ble_client or not self._ble_client.is_connected:
            return None
        
        # Wake battery reporting by cycling CMD notify subscription
        await self._wake_battery_reporting()
        
        # Read standard BLE Battery Service
        try:
            data = await self._ble_client.read_gatt_char(
                "00002a19-0000-1000-8000-00805f9b34fb"
            )
            if data and len(data) > 0:
                level = data[0]
                logger.debug("Battery: %d%%", level)
                return BatteryInfo(level=level, is_charging=False)
        except Exception as e:
            logger.debug("Standard battery read failed: %s", e)
        
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
