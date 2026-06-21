# WHOOP Python Desktop App

Cross-platform Python application for WHOOP 4.0 and 5.0 straps — BLE client, local SQLite storage, analytics engine, and CLI.

## Architecture

```
whoop/
├── __init__.py
├── cli.py            # argparse CLI (scan, info, record, dashboard, export, battery)
├── ble_client.py     # Bleak-based BLE client (async, cross-platform)
├── database.py       # SQLite storage (stdlib sqlite3, WAL mode)
├── analytics.py      # HRV, recovery, strain, sleep (stdlib math/statistics)
└── protocol/         # Zero-dependency WHOOP protocol (CRC, framing, parsing, handshake)
    ├── __init__.py
    ├── crc.py
    ├── device_family.py
    ├── frames.py
    ├── commands.py
    ├── packet_types.py
    ├── parse_frame.py
    ├── parsed_frame.py
    └── handshake.py
```

## Installation

```bash
cd /root/projects/whoop-python
pip install bleak
pip install -e .
```

Or with Rich for prettier output:

```bash
pip install bleak rich
pip install -e .
```

## CLI Usage

```bash
# Scan for nearby WHOOP devices
whoop scan
whoop scan --timeout 10

# Show device info (connect, read battery, disconnect)
whoop info AA:BB:CC:DD:EE:FF

# Stream realtime HR data and save to SQLite DB
whoop record AA:BB:CC:DD:EE:FF
whoop record AA:BB:CC:DD:EE:FF --duration 120 --database ~/.whoop/my.db

# Show today's metrics from the database
whoop dashboard
whoop dashboard --database ~/.whoop/my.db

# Export HR data to CSV
whoop export output.csv

# Read battery level
whoop battery AA:BB:CC:DD:EE:FF
```

## Database

Data is stored in `~/.whoop/whoop.db` (SQLite with WAL journal mode).

### Tables

| Table         | Description                           |
|---------------|---------------------------------------|
| `device`      | Registered WHOOP straps               |
| `hr_sample`   | Heart rate samples (timestamp, bpm)   |
| `rr_interval` | RR intervals in ms                    |
| `event`       | Device events (on/off-wrist, etc.)    |
| `battery`     | Battery level readings                |
| `daily_metric`| Daily recovery, strain, HRV summary   |
| `sleep_session`| Sleep sessions with stage detection  |

## API

### BLE Client

```python
import asyncio
from whoop.ble_client import WhoopBleClient

async def main():
    client = WhoopBleClient()
    devices = await client.scan(timeout=5.0)
    if devices:
        await client.connect(devices[0].address, devices[0].family)
        batt = await client.read_battery()
        print(f"Battery: {batt.level}%")
        await client.disconnect()

asyncio.run(main())
```

### Database

```python
from whoop.database import WhoopDatabase

with WhoopDatabase() as db:
    dev_id = db.ensure_device("AA:BB:CC:DD:EE:FF", family="Whoop4")
    db.insert_hr_samples(dev_id, [(1718765432, 72), (1718765433, 73)])
    latest = db.get_latest_hr(dev_id, limit=5)
```

### Analytics

```python
from whoop.analytics import compute_rmssd, compute_strain, compute_recovery

rr = [800.0, 820.0, 810.0, 830.0]
rmssd = compute_rmssd(rr)       # → 17.32 ms
strain = compute_strain([140]*30, resting_hr=60, max_hr=190)
recovery = compute_recovery(hrv_rmssd=rmssd, resting_hr=60,
                            sleep_efficiency=90, strain=5.0)
```

## Running Tests

```bash
pip install pytest
python -m pytest tests/ -v
```

## Requirements

- Python ≥ 3.10
- [bleak](https://github.com/hbldh/bleak) ≥ 0.21.0 (cross-platform BLE)
- Optional: [rich](https://github.com/Textualize/rich) for colored CLI output

## License

MIT
