"""
WHOOP 4.0 BLE diagnostic — run on the Windows 10 box, from the repo root:
    python whoop_ble_diag.py AA:BB:CC:DD:EE:FF

It deliberately bypasses WhoopBleClient and talks to bleak directly, so we
isolate "bleak/WinRT behaviour" from "your wrapper". It answers, in order:

  1. Does the connection come up when built the IDIOMATIC way
     (address string -> internal find_device_by_address), vs your current
     BLEDevice(details=None) construction?
  2. What characteristics actually exist under the WHOOP service, and what
     PROPERTIES do they report? (notify vs indicate vs read-only)
  3. What does 0x2A19 return on an EXPLICIT uncached read, raw bytes?
  4. Do ANY notifications arrive on ANY channel in ~30s — standard HR
     (0x2A37), cmd (...0003), event (...0004), data (...0005) — while we
     poke the strap with SET_CLOCK / GET_BATTERY_LEVEL / GET_HELLO_HARVARD?

Read the three verdicts printed at the end.
"""
import asyncio
import sys
import time

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic

from whoop.protocol.commands import Command
from whoop.protocol.device_family import DeviceFamily

BATT = "00002a19-0000-1000-8000-00805f9b34fb"
STD_HR = "00002a37-0000-1000-8000-00805f9b34fb"

fam = DeviceFamily.WHOOP_4()
CMD_WRITE = fam.cmd_char_uuid       # 61080002...
CMD_NOTIFY = fam.cmd_notify_uuid    # 61080003...
EVT_NOTIFY = fam.event_notify_uuid  # 61080004...
DATA_NOTIFY = fam.data_notify_uuid  # 61080005...

seen: dict[str, int] = {}
first_ts: dict[str, float] = {}


def make_cb(tag: str):
    def cb(_sender: int, data: bytearray) -> None:
        seen[tag] = seen.get(tag, 0) + 1
        first_ts.setdefault(tag, time.monotonic())
        print(f"  [NOTIFY {tag:7}] {bytes(data).hex()}")
    return cb


async def poke(client: BleakClient, name: str, frame: bytes) -> None:
    print(f"-> writing {name} ({len(frame)} bytes)...")
    try:
        await client.write_gatt_char(CMD_WRITE, frame, response=True)
        print(f"   {name} write ACKed")
    except Exception as e:
        # "Insufficient Authentication" here = Windows is doing just-works pairing.
        print(f"   {name} write raised: {e!r}")
    await asyncio.sleep(4.0)


async def main(address: str) -> None:
    print("=" * 64)
    print("STEP 1 — connect via address string (idiomatic WinRT path)")
    print("=" * 64)
    dev = await BleakScanner.find_device_by_address(address, timeout=15.0)
    print("find_device_by_address ->", dev)
    target = dev if dev is not None else address   # string fallback: from_bluetooth_address_async
    client = BleakClient(target, timeout=20.0)
    await client.connect()
    print("connected:", client.is_connected)

    print("\n" + "=" * 64)
    print("STEP 2 — full GATT table (look for ...0003 props, and 0x2A19)")
    print("=" * 64)
    found_cmd_notify: BleakGATTCharacteristic | None = None
    found_batt = False
    for svc in client.services:
        print(f"SVC  {svc.uuid}")
        for ch in svc.characteristics:
            props = ",".join(ch.properties)
            print(f"  CHAR {ch.uuid}  [{props}]")
            if ch.uuid.lower() == CMD_NOTIFY.lower():
                found_cmd_notify = ch
            if ch.uuid.lower() == BATT.lower():
                found_batt = True

    print("\n" + "=" * 64)
    print("STEP 3 — explicit UNCACHED read of 0x2A19")
    print("=" * 64)
    if found_batt:
        try:
            raw = await client.read_gatt_char(BATT, use_cached=False)
            b0 = raw[0] if raw else None
            print(f"0x2A19 raw={raw.hex()}  byte0={b0}  (={b0}% if this is a battery level)")
        except Exception as e:
            print("0x2A19 read error:", repr(e))
    else:
        print("0x2A19 NOT PRESENT in GATT table — strap has no standard Battery Service")

    print("\n" + "=" * 64)
    print("STEP 4 — subscribe everything, poke strap, watch ~30s")
    print("=" * 64)
    for uuid, tag in [
        (STD_HR,     "STD_HR"),
        (CMD_NOTIFY, "CMD"),
        (EVT_NOTIFY, "EVT"),
        (DATA_NOTIFY,"DATA"),
    ]:
        try:
            await client.start_notify(uuid, make_cb(tag))
            print(f"subscribed {tag} ({uuid})")
        except Exception as e:
            print(f"subscribe FAILED {tag}: {e!r}")

    await asyncio.sleep(0.5)

    await poke(
        client, "GET_BATTERY_LEVEL",
        Command.GET_BATTERY_LEVEL.build_frame(seq=0, payload=b"", family=fam),
    )
    await poke(
        client, "SET_CLOCK",
        Command.SET_CLOCK.build_frame(
            seq=1,
            payload=int(time.time()).to_bytes(4, "little") + b"\x00\x00\x00\x00",
            family=fam,
        ),
    )
    await poke(
        client, "GET_HELLO_HARVARD",
        Command.GET_HELLO_HARVARD.build_frame(seq=2, payload=b"", family=fam),
    )

    print("...idle-listening 15 more seconds...")
    await asyncio.sleep(15.0)

    print("\n" + "=" * 64)
    print("VERDICTS")
    print("=" * 64)
    total = sum(seen.values())
    print("notification counts:", seen if seen else "{} (NONE on any channel)")

    # Verdict A — routing problem or strap staying silent?
    if total == 0:
        print("\nA) ZERO notifications on EVERY channel including standard 0x2A37.")
        print("   -> Not a WHOOP-protocol gate. Either WinRT isn't routing")
        print("      ValueChanged events at all, or the strap sends nothing in")
        print("      this state. Wear the strap and re-run: if 0x2A37 (HR) STILL")
        print("      gives nothing, it is a bleak/WinRT/session problem, not WHOOP.")
    elif seen.get("STD_HR") and not any(seen.get(k) for k in ("CMD", "EVT", "DATA")):
        print("\nA) Standard 0x2A37 delivers, proprietary 6108xxxx do NOT.")
        print("   -> WinRT routing is fine. The strap is GATING its proprietary")
        print("      stream behind something you have not sent (handshake / crypto).")
        print("      This is the real wall — chase the handshake protocol.")
    else:
        print("\nA) Proprietary notifications DID arrive.")
        print("   Which channel carried the command response above tells you where")
        print("   to parse battery from. NOTE: it may be EVT/DATA (...0004/...0005),")
        print("   NOT ...0003 that your code subscribes to.")

    # Verdict B — battery source
    print("\nB) If 0x2A19 byte0 does NOT match the WHOOP app %, the standard")
    print("   Battery Service is NOT the real source on WHOOP 4. Parse battery")
    print("   from the GET_BATTERY_LEVEL response frame on whichever channel fired.")

    # Verdict C — characteristic properties
    if found_cmd_notify:
        props = ",".join(found_cmd_notify.properties)
        print(f"\nC) ...0003 (CMD_NOTIFY) reported properties: [{props}]")
        if "notify" not in props and "indicate" not in props:
            print("   WARNING: neither 'notify' nor 'indicate' in props.")
            print("   start_notify() on this char does nothing — that alone")
            print("   explains why _on_notification never fires.")
    else:
        print("\nC) ...0003 (CMD_NOTIFY) was NOT found in GATT table at all.")

    await client.disconnect()
    print("\nDone. Paste the full output back.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python whoop_ble_diag.py <MAC_ADDRESS>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
