"""SQLite storage for WHOOP data using stdlib sqlite3.

Creates and manages tables for HR samples, RR intervals, events,
battery readings, daily metrics, and sleep sessions.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Sequence

DEFAULT_DB_DIR = os.path.expanduser("~/.whoop")
DEFAULT_DB_PATH = os.path.join(DEFAULT_DB_DIR, "whoop.db")


def _ensure_dir(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


class WhoopDatabase:
    """SQLite database for WHOOP data with WAL journal mode."""

    def __init__(self, path: str = DEFAULT_DB_PATH) -> None:
        _ensure_dir(path)
        self._path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._create_tables()

    # ----------------------------------------------------------------
    # Schema
    # ----------------------------------------------------------------

    def _create_tables(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS device (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                address     TEXT NOT NULL UNIQUE,
                name        TEXT,
                family      TEXT NOT NULL,
                first_seen  INTEGER NOT NULL,
                last_seen   INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS hr_sample (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id   INTEGER NOT NULL REFERENCES device(id),
                timestamp   INTEGER NOT NULL,
                heart_rate  INTEGER NOT NULL,
                UNIQUE(device_id, timestamp)
            );
            CREATE INDEX IF NOT EXISTS idx_hr_sample_timestamp
                ON hr_sample(device_id, timestamp);

            CREATE TABLE IF NOT EXISTS rr_interval (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id   INTEGER NOT NULL REFERENCES device(id),
                timestamp   INTEGER NOT NULL,
                rr_ms       REAL NOT NULL,
                UNIQUE(device_id, timestamp, rr_ms)
            );
            CREATE INDEX IF NOT EXISTS idx_rr_timestamp
                ON rr_interval(device_id, timestamp);

            CREATE TABLE IF NOT EXISTS event (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id   INTEGER NOT NULL REFERENCES device(id),
                timestamp   INTEGER NOT NULL,
                kind        INTEGER NOT NULL,
                UNIQUE(device_id, timestamp, kind)
            );

            CREATE TABLE IF NOT EXISTS battery (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id   INTEGER NOT NULL REFERENCES device(id),
                timestamp   INTEGER NOT NULL,
                level       INTEGER NOT NULL,
                is_charging INTEGER NOT NULL DEFAULT 0,
                UNIQUE(device_id, timestamp)
            );

            CREATE TABLE IF NOT EXISTS daily_metric (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id   INTEGER NOT NULL REFERENCES device(id),
                date        TEXT NOT NULL,  -- YYYY-MM-DD
                recovery    REAL,
                strain      REAL,
                resting_hr  INTEGER,
                hrv_rmssd   REAL,
                sleep_efficiency REAL,
                UNIQUE(device_id, date)
            );

            CREATE TABLE IF NOT EXISTS sleep_session (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id   INTEGER NOT NULL REFERENCES device(id),
                start_ts    INTEGER NOT NULL,
                end_ts      INTEGER,
                efficiency  REAL,
                stages_json TEXT,
                UNIQUE(device_id, start_ts)
            );
        """)

    # ----------------------------------------------------------------
    # Device registry
    # ----------------------------------------------------------------

    def ensure_device(
        self, address: str, name: str | None = None, family: str = "Whoop4"
    ) -> int:
        """Insert or update a device; return its internal id."""
        now = int(time.time())
        cur = self._conn.execute(
            """INSERT INTO device (address, name, family, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(address) DO UPDATE SET
                   name = COALESCE(excluded.name, name),
                   last_seen = excluded.last_seen""",
            (address, name, family, now, now),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT id FROM device WHERE address = ?", (address,)
        ).fetchone()
        assert row is not None
        return row[0]

    def get_device_id(self, address: str) -> int | None:
        row = self._conn.execute(
            "SELECT id FROM device WHERE address = ?", (address,)
        ).fetchone()
        return row[0] if row else None

    # ----------------------------------------------------------------
    # HR samples
    # ----------------------------------------------------------------

    def insert_hr_samples(
        self, device_id: int, samples: list[tuple[int, int]]
    ) -> int:
        """Insert (timestamp, heart_rate) tuples. Returns rows inserted."""
        count = 0
        for ts, hr in samples:
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO hr_sample (device_id, timestamp, heart_rate)
                   VALUES (?, ?, ?)""",
                (device_id, ts, hr),
            )
            count += cur.rowcount
        self._conn.commit()
        return count

    def get_hr_range(
        self, device_id: int, start: int, end: int
    ) -> list[tuple[int, int]]:
        """Return (timestamp, hr) rows in [start, end]."""
        rows = self._conn.execute(
            """SELECT timestamp, heart_rate FROM hr_sample
               WHERE device_id = ? AND timestamp >= ? AND timestamp <= ?
               ORDER BY timestamp""",
            (device_id, start, end),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def get_latest_hr(self, device_id: int, limit: int = 1) -> list[tuple[int, int]]:
        """Return the most recent HR readings."""
        rows = self._conn.execute(
            """SELECT timestamp, heart_rate FROM hr_sample
               WHERE device_id = ?
               ORDER BY timestamp DESC LIMIT ?""",
            (device_id, limit),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    # ----------------------------------------------------------------
    # RR intervals
    # ----------------------------------------------------------------

    def insert_rr_intervals(
        self, device_id: int, intervals: list[tuple[int, float]]
    ) -> int:
        """Insert (timestamp, rr_ms) tuples. Returns rows inserted."""
        count = 0
        for ts, rr in intervals:
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO rr_interval (device_id, timestamp, rr_ms)
                   VALUES (?, ?, ?)""",
                (device_id, ts, rr),
            )
            count += cur.rowcount
        self._conn.commit()
        return count

    def get_rr_range(
        self, device_id: int, start: int, end: int
    ) -> list[tuple[int, float]]:
        """Return (timestamp, rr_ms) rows in [start, end]."""
        rows = self._conn.execute(
            """SELECT timestamp, rr_ms FROM rr_interval
               WHERE device_id = ? AND timestamp >= ? AND timestamp <= ?
               ORDER BY timestamp""",
            (device_id, start, end),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    # ----------------------------------------------------------------
    # Events
    # ----------------------------------------------------------------

    def insert_events(
        self, device_id: int, events: list[tuple[int, int]]
    ) -> int:
        """Insert (timestamp, kind) event tuples."""
        count = 0
        for ts, kind in events:
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO event (device_id, timestamp, kind)
                   VALUES (?, ?, ?)""",
                (device_id, ts, kind),
            )
            count += cur.rowcount
        self._conn.commit()
        return count

    # ----------------------------------------------------------------
    # Battery
    # ----------------------------------------------------------------

    def insert_battery(
        self, device_id: int, timestamp: int, level: int, is_charging: bool
    ) -> None:
        self._conn.execute(
            """INSERT OR IGNORE INTO battery (device_id, timestamp, level, is_charging)
               VALUES (?, ?, ?, ?)""",
            (device_id, timestamp, level, int(is_charging)),
        )
        self._conn.commit()

    def get_latest_battery(self, device_id: int) -> tuple[int, int] | None:
        row = self._conn.execute(
            """SELECT level, is_charging FROM battery
               WHERE device_id = ?
               ORDER BY timestamp DESC LIMIT 1""",
            (device_id,),
        ).fetchone()
        return (row[0], bool(row[1])) if row else None

    # ----------------------------------------------------------------
    # Daily metrics
    # ----------------------------------------------------------------

    def upsert_daily_metric(
        self,
        device_id: int,
        date: str,
        recovery: float | None = None,
        strain: float | None = None,
        resting_hr: int | None = None,
        hrv_rmssd: float | None = None,
        sleep_efficiency: float | None = None,
    ) -> None:
        self._conn.execute(
            """INSERT INTO daily_metric
                   (device_id, date, recovery, strain, resting_hr, hrv_rmssd, sleep_efficiency)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(device_id, date) DO UPDATE SET
                   recovery = COALESCE(excluded.recovery, recovery),
                   strain = COALESCE(excluded.strain, strain),
                   resting_hr = COALESCE(excluded.resting_hr, resting_hr),
                   hrv_rmssd = COALESCE(excluded.hrv_rmssd, hrv_rmssd),
                   sleep_efficiency = COALESCE(excluded.sleep_efficiency, sleep_efficiency)""",
            (device_id, date, recovery, strain, resting_hr, hrv_rmssd, sleep_efficiency),
        )
        self._conn.commit()

    def get_daily_metrics(self, device_id: int, date: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            """SELECT recovery, strain, resting_hr, hrv_rmssd, sleep_efficiency
               FROM daily_metric WHERE device_id = ? AND date = ?""",
            (device_id, date),
        ).fetchone()
        if row is None:
            return None
        return {
            "recovery": row[0],
            "strain": row[1],
            "resting_hr": row[2],
            "hrv_rmssd": row[3],
            "sleep_efficiency": row[4],
        }

    # ----------------------------------------------------------------
    # Sleep sessions
    # ----------------------------------------------------------------

    def insert_sleep_session(
        self,
        device_id: int,
        start_ts: int,
        end_ts: int | None = None,
        efficiency: float | None = None,
        stages_json: str | None = None,
    ) -> None:
        self._conn.execute(
            """INSERT OR IGNORE INTO sleep_session
                   (device_id, start_ts, end_ts, efficiency, stages_json)
               VALUES (?, ?, ?, ?, ?)""",
            (device_id, start_ts, end_ts, efficiency, stages_json),
        )
        self._conn.commit()

    # ----------------------------------------------------------------
    # Export
    # ----------------------------------------------------------------

    def export_hr_csv(self, device_id: int, path: str) -> int:
        """Export HR samples to CSV. Returns row count."""
        import csv
        rows = self._conn.execute(
            "SELECT timestamp, heart_rate FROM hr_sample WHERE device_id = ? ORDER BY timestamp",
            (device_id,),
        ).fetchall()
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "heart_rate"])
            w.writerows(rows)
        return len(rows)

    # ----------------------------------------------------------------
    # Lifecycle
    # ----------------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> WhoopDatabase:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
