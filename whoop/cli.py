"""WHOOP CLI — scan, connect, record, dashboard, export.

Usage:
    whoop scan              Scan for WHOOP devices
    whoop info <address>    Show device info
    whoop record <address>  Stream data to DB
    whoop dashboard         Show today's metrics
    whoop export <path>     Export data to CSV
    whoop battery <address> Read battery level
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import logging
import os
import sys
import time

from whoop.ble_client import WhoopBleClient, ScannedDevice, ConnectionState
from whoop.database import WhoopDatabase
from whoop.analytics import (
    compute_rmssd,
    compute_resting_hr,
    compute_strain,
    compute_recovery,
)
from whoop.protocol.device_family import DeviceFamilyKind

logger = logging.getLogger("whoop.cli")


# ---------------------------------------------------------------------------
# Pretty-print helpers (Rich if available, plain otherwise)
# ---------------------------------------------------------------------------

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    _console = Console()
    _HAS_RICH = True
except ImportError:
    _console = None  # type: ignore[assignment]
    _HAS_RICH = False


def _print_header(text: str) -> None:
    if _HAS_RICH:
        _console.print(Panel(text, style="bold cyan"))
    else:
        print(f"\n=== {text} ===")


def _print_kv(key: str, value: str) -> None:
    if _HAS_RICH:
        _console.print(f"  [bold]{key}[/bold]: {value}")
    else:
        print(f"  {key}: {value}")


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    if _HAS_RICH and _console is not None:
        table = Table()
        for h in headers:
            table.add_column(h, style="cyan")
        for row in rows:
            table.add_row(*row)
        _console.print(table)
    else:
        col_widths = [max(len(str(r[i])) for r in rows + [headers]) for i in range(len(headers))]
        fmt = "  " + "  ".join(f"{{:<{w}}}" for w in col_widths)
        print(fmt.format(*headers))
        print("  " + "  ".join("-" * w for w in col_widths))
        for row in rows:
            print(fmt.format(*row))


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


async def _cmd_scan(args: argparse.Namespace) -> int:
    """Scan for WHOOP devices."""
    _print_header("Scanning for WHOOP devices...")
    client = WhoopBleClient()
    devices = await client.scan(timeout=args.timeout)

    if not devices:
        _print_kv("Result", "No WHOOP devices found")
        return 0

    rows = []
    for d in devices:
        rows.append([d.name, d.address, str(d.rssi), d.family.value])

    _print_header(f"Found {len(devices)} device(s)")
    _print_table(["Name", "Address", "RSSI", "Family"], rows)
    return 0


async def _cmd_info(args: argparse.Namespace) -> int:
    """Connect briefly and show device info."""
    address = args.address
    _print_header(f"Connecting to {address}...")

    client = WhoopBleClient()

    # Try both families
    for family in (DeviceFamilyKind.WHOOP_4, DeviceFamilyKind.WHOOP_5):
        ok = await client.connect(address, family, timeout=10.0)
        if ok:
            _print_kv("Family", family.value)
            _print_kv("Address", address)
            _print_kv("State", client.state.value)

            battery = await client.read_battery()
            if battery:
                _print_kv("Battery", f"{battery.level}% {'(charging)' if battery.is_charging else ''}")

            await client.disconnect()
            return 0

    _print_kv("Result", "Could not connect to device")
    return 1


async def _cmd_record(args: argparse.Namespace) -> int:
    """Connect, stream realtime data, and save to DB."""
    address = args.address
    db_path = args.database or os.path.expanduser("~/.whoop/whoop.db")
    duration = args.duration

    _print_header(f"Recording from {address} for {duration}s...")

    db = WhoopDatabase(db_path)
    client = WhoopBleClient()

    family = DeviceFamilyKind.WHOOP_4
    if args.family:
        try:
            family = DeviceFamilyKind(args.family)
        except ValueError:
            _print_kv("Error", f"Unknown family: {args.family}")
            return 1

    ok = await client.connect(address, family, timeout=10.0)
    if not ok:
        _print_kv("Error", "Failed to connect")
        db.close()
        return 1

    device_id = db.ensure_device(address, family=family.value)
    hr_buffer: list[tuple[int, int]] = []
    rr_buffer: list[tuple[int, float]] = []

    def on_data(parsed: object) -> None:
        from whoop.protocol.parsed_frame import RealtimeData, Event
        if isinstance(parsed, RealtimeData):
            ts = parsed.timestamp
            if ts == 0:
                ts = int(time.time())
            hr = parsed.heart_rate
            if hr > 0:
                hr_buffer.append((ts, hr))
                print(f"  HR: {hr} bpm  |  RR count: {parsed.rr_count}")
            for rr in parsed.rr_intervals:
                rr_buffer.append((ts, float(rr)))
        elif isinstance(parsed, Event):
            ts = parsed.event_timestamp or int(time.time())
            db.insert_events(device_id, [(ts, parsed.event_kind.value)])

    client.on_data = on_data

    await client.start_realtime_hr()
    await client.start_data_stream()

    # Flush periodically
    async def _flush_loop() -> None:
        while True:
            await asyncio.sleep(5)
            if hr_buffer:
                db.insert_hr_samples(device_id, hr_buffer)
                hr_buffer.clear()
            if rr_buffer:
                db.insert_rr_intervals(device_id, rr_buffer)
                rr_buffer.clear()

    flush_task = asyncio.create_task(_flush_loop())

    try:
        await asyncio.sleep(duration)
    finally:
        flush_task.cancel()
        try:
            await flush_task
        except asyncio.CancelledError:
            pass

        # Final flush
        if hr_buffer:
            db.insert_hr_samples(device_id, hr_buffer)
        if rr_buffer:
            db.insert_rr_intervals(device_id, rr_buffer)

    await client.disconnect()

    _print_header("Recording complete")
    _print_kv("DB path", db_path)
    hr_count = len(db.get_hr_range(device_id, 0, int(time.time())))
    _print_kv("HR samples saved", str(hr_count))
    db.close()
    return 0


async def _cmd_dashboard(args: argparse.Namespace) -> int:
    """Show today's metrics from the database."""
    db_path = args.database or os.path.expanduser("~/.whoop/whoop.db")
    db = WhoopDatabase(db_path)

    today = datetime.date.today().isoformat()

    # Get devices
    import sqlite3
    devices = db._conn.execute("SELECT id, address, name, family FROM device").fetchall()
    if not devices:
        _print_header("No devices in database")
        db.close()
        return 0

    for dev_id, addr, name, family in devices:
        _print_header(f"Device: {name or addr} ({family})")

        metrics = db.get_daily_metrics(dev_id, today)
        if metrics:
            _print_kv("Recovery", f"{metrics['recovery']}%" if metrics['recovery'] else "N/A")
            _print_kv("Strain", str(metrics['strain']) if metrics['strain'] else "N/A")
            _print_kv("Resting HR", f"{metrics['resting_hr']} bpm" if metrics['resting_hr'] else "N/A")
            _print_kv("HRV (RMSSD)", f"{metrics['hrv_rmssd']} ms" if metrics['hrv_rmssd'] else "N/A")
            _print_kv("Sleep Efficiency", f"{metrics['sleep_efficiency']}%" if metrics['sleep_efficiency'] else "N/A")

        # Latest HR
        latest = db.get_latest_hr(dev_id, limit=5)
        if latest:
            ts, hr = latest[0]
            t = datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S")
            _print_kv("Latest HR", f"{hr} bpm at {t}")

        # Battery
        bat = db.get_latest_battery(dev_id)
        if bat:
            _print_kv("Battery", f"{bat[0]}% {'(charging)' if bat[1] else ''}")

    db.close()
    return 0


async def _cmd_export(args: argparse.Namespace) -> int:
    """Export data to CSV."""
    db_path = args.database or os.path.expanduser("~/.whoop/whoop.db")
    output_path = args.path

    db = WhoopDatabase(db_path)
    devices = db._conn.execute("SELECT id, address FROM device").fetchall()

    if not devices:
        _print_kv("Error", "No devices in database")
        db.close()
        return 1

    total = 0
    for dev_id, addr in devices:
        device_path = output_path
        if len(devices) > 1:
            base, ext = os.path.splitext(output_path)
            device_path = f"{base}_{addr.replace(':', '')}{ext}"
        count = db.export_hr_csv(dev_id, device_path)
        total += count
        _print_kv(f"Exported {addr}", f"{count} rows → {device_path}")

    _print_header(f"Export complete: {total} total rows")
    db.close()
    return 0


async def _cmd_battery(args: argparse.Namespace) -> int:
    """Read battery level from a connected strap."""
    address = args.address
    _print_header(f"Reading battery from {address}...")

    client = WhoopBleClient()
    for family in (DeviceFamilyKind.WHOOP_4, DeviceFamilyKind.WHOOP_5):
        ok = await client.connect(address, family, timeout=10.0)
        if ok:
            battery = await client.read_battery()
            if battery:
                _print_kv("Battery", f"{battery.level}%")
                _print_kv("Charging", "Yes" if battery.is_charging else "No")
            else:
                _print_kv("Error", "Could not read battery")
            await client.disconnect()
            return 0

    _print_kv("Error", "Could not connect")
    return 1


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="whoop",
        description="WHOOP Desktop CLI — BLE client for WHOOP straps",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # scan
    p_scan = sub.add_parser("scan", help="Scan for WHOOP devices")
    p_scan.add_argument("--timeout", type=float, default=5.0, help="Scan duration in seconds")

    # info
    p_info = sub.add_parser("info", help="Show device info")
    p_info.add_argument("address", help="BLE address (MAC or UUID)")

    # record
    p_rec = sub.add_parser("record", help="Connect and stream data to DB")
    p_rec.add_argument("address", help="BLE address")
    p_rec.add_argument("--duration", type=float, default=30.0, help="Recording duration in seconds")
    p_rec.add_argument("--database", help="SQLite database path")
    p_rec.add_argument("--family", choices=["Whoop4", "Whoop5"], default="Whoop4")

    # dashboard
    p_dash = sub.add_parser("dashboard", help="Show today's metrics")
    p_dash.add_argument("--database", help="SQLite database path")

    # export
    p_exp = sub.add_parser("export", help="Export data to CSV")
    p_exp.add_argument("path", help="Output CSV path")
    p_exp.add_argument("--database", help="SQLite database path")

    # battery
    p_bat = sub.add_parser("battery", help="Read battery level")
    p_bat.add_argument("address", help="BLE address")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    # Enable debug logging
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    cmd_map = {
        "scan": _cmd_scan,
        "info": _cmd_info,
        "record": _cmd_record,
        "dashboard": _cmd_dashboard,
        "export": _cmd_export,
        "battery": _cmd_battery,
    }

    handler = cmd_map.get(args.command)
    if handler is None:
        parser.print_help()
        sys.exit(1)

    try:
        rc = asyncio.run(handler(args))
        sys.exit(rc if rc is not None else 0)
    except KeyboardInterrupt:
        print("\nInterrupted")
        sys.exit(130)


if __name__ == "__main__":
    main()
