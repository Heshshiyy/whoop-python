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

    for family in (DeviceFamilyKind.WHOOP_4, DeviceFamilyKind.WHOOP_4_PUFFIN, DeviceFamilyKind.WHOOP_5):
        ok = await client.connect(address, family, timeout=15.0)
        if ok:
            _print_kv("Family", family.value)
            _print_kv("Address", address)
            _print_kv("State", client.state.value)
            
            battery = await client.read_battery()
            if battery:
                _print_kv("Battery", f"{battery.level}% {'(charging)' if battery.is_charging else ''}")
            else:
                _print_kv("Battery", "Could not read")

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
        from whoop.protocol.parsed_frame import RealtimeData
        if isinstance(parsed, dict):
            # Standard BLE HR data
            hr = parsed.get("heart_rate", 0)
            if hr > 0:
                ts = int(time.time())
                hr_buffer.append((ts, hr))
                print(f"  HR: {hr} bpm (std)")
            for rr in parsed.get("rr_intervals", []):
                ts = int(time.time())
                rr_buffer.append((ts, float(rr)))
        elif isinstance(parsed, RealtimeData):
            ts = parsed.timestamp or int(time.time())
            hr = parsed.heart_rate
            if hr > 0:
                hr_buffer.append((ts, hr))
                print(f"  HR: {hr} bpm  |  RR: {parsed.rr_count} intervals")
            for rr in parsed.rr_intervals:
                rr_buffer.append((ts, float(rr)))

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


async def _cmd_history(args: argparse.Namespace) -> int:
    """Offload last N minutes of HR data from the strap, save to DB, and display."""
    address = args.address
    minutes = args.minutes
    db_path = args.database or os.path.expanduser("~/.whoop/whoop.db")

    end_ts = int(time.time())
    start_ts = end_ts - minutes * 60

    _print_header(f"Fetching last {minutes} min of HR from {address}...")

    db = WhoopDatabase(db_path)
    client = WhoopBleClient()

    ok = False
    for family in (DeviceFamilyKind.WHOOP_4_PUFFIN, DeviceFamilyKind.WHOOP_4, DeviceFamilyKind.WHOOP_5):
        ok = await client.connect(address, family, timeout=15.0)
        if ok:
            break
    if not ok:
        _print_kv("Error", "Could not connect")
        db.close()
        return 1

    device_id = db.ensure_device(address, family="Whoop4")

    # Try historical offload from strap memory
    _print_kv("Status", f"Requesting historical data from strap (60s timeout, window={minutes}min)...")
    _print_kv("Time range", f"{start_ts} → {end_ts}")
    records = await client.get_history(start_ts, end_ts, timeout=60.0)

    if records:
        hr_samples = [
            (r.unix, r.heart_rate) for r in records
            if r.unix >= start_ts and r.heart_rate > 0
        ]
        rr_samples = [
            (r.unix, float(rr)) for r in records for rr in r.rr_intervals
        ]
        saved_hr = db.insert_hr_samples(device_id, hr_samples)
        saved_rr = db.insert_rr_intervals(device_id, rr_samples)
        _print_kv("Records from strap", str(len(records)))
        _print_kv("HR samples saved", str(saved_hr))
        _print_kv("RR intervals saved", str(saved_rr))
    else:
        _print_kv(
            "Note",
            "Strap returned no historical data — showing what is already in DB. "
            "Run `whoop record` to build up a local history.",
        )

    await client.disconnect()

    # Show from DB (includes freshly offloaded data)
    rows = db.get_hr_range(device_id, start_ts, end_ts)
    db.close()

    if not rows:
        _print_kv(
            "No data",
            f"Nothing recorded in last {minutes} min. "
            f"Run: whoop record {address} --duration {minutes * 60}",
        )
        return 0

    _print_header(f"Heart rate — last {minutes} minutes  ({len(rows)} samples)")
    table_rows = [
        [datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S"), f"{hr} bpm"]
        for ts, hr in rows[-50:]
    ]
    _print_table(["Time", "Heart Rate"], table_rows)

    hrs = [hr for _, hr in rows]
    _print_kv("Average", f"{sum(hrs) // len(hrs)} bpm")
    _print_kv("Min / Max", f"{min(hrs)} / {max(hrs)} bpm")
    return 0


async def _cmd_show(args: argparse.Namespace) -> int:
    """Query local DB for last N minutes of HR — no BLE needed."""
    db_path = args.database or os.path.expanduser("~/.whoop/whoop.db")
    minutes = args.minutes
    end_ts = int(time.time())
    start_ts = end_ts - minutes * 60

    db = WhoopDatabase(db_path)
    devices = db._conn.execute("SELECT id, address FROM device").fetchall()

    if not devices:
        _print_kv("No data", "No devices in DB. Run `whoop record <address>` first.")
        db.close()
        return 0

    for dev_id, addr in devices:
        rows = db.get_hr_range(dev_id, start_ts, end_ts)
        if not rows:
            _print_kv(addr, f"No HR data in last {minutes} min")
            continue

        _print_header(f"{addr} — last {minutes} minutes  ({len(rows)} samples)")
        table_rows = [
            [datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S"), f"{hr} bpm"]
            for ts, hr in rows[-50:]
        ]
        _print_table(["Time", "Heart Rate"], table_rows)

        hrs = [hr for _, hr in rows]
        _print_kv("Average", f"{sum(hrs) // len(hrs)} bpm")
        _print_kv("Min / Max", f"{min(hrs)} / {max(hrs)} bpm")

        rr_rows = db.get_rr_range(dev_id, start_ts, end_ts)
        if rr_rows:
            from whoop.analytics import compute_rmssd
            rmssd = compute_rmssd([rr for _, rr in rr_rows])
            if rmssd:
                _print_kv("HRV (RMSSD)", f"{rmssd:.1f} ms")

    db.close()
    return 0


async def _cmd_battery(args: argparse.Namespace) -> int:
    """Read battery level from a connected strap."""
    address = args.address
    _print_header(f"Reading battery from {address}...")

    client = WhoopBleClient()
    for family in (DeviceFamilyKind.WHOOP_4, DeviceFamilyKind.WHOOP_4_PUFFIN, DeviceFamilyKind.WHOOP_5):
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

    # history — offload from strap + show
    p_hist = sub.add_parser("history", help="Pull last N min of HR from strap and show")
    p_hist.add_argument("address", help="BLE address")
    p_hist.add_argument("--minutes", type=int, default=10, help="How many minutes back (default 10)")
    p_hist.add_argument("--database", help="SQLite database path")

    # show — query local DB only (no BLE)
    p_show = sub.add_parser("show", help="Show last N min of HR from local database")
    p_show.add_argument("--minutes", type=int, default=10, help="How many minutes back (default 10)")
    p_show.add_argument("--database", help="SQLite database path")

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
        "history": _cmd_history,
        "show": _cmd_show,
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
