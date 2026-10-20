import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from app.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    device_name TEXT    NOT NULL CHECK (length(trim(device_name)) BETWEEN 1 AND 100),
    ip_address  TEXT    NOT NULL UNIQUE,
    hostname    TEXT,
    device_type TEXT    NOT NULL DEFAULT 'other'
                CONSTRAINT ck_device_type
                CHECK (device_type IN ('router','switch','server','printer','workstation','other')),
    is_active   INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    date_added  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS device_ports (
    device_id INTEGER NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
    port      INTEGER NOT NULL CONSTRAINT ck_device_port_range CHECK (port BETWEEN 1 AND 65535),
    PRIMARY KEY (device_id, port)
);

CREATE TABLE IF NOT EXISTS monitoring_logs (
    log_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id        INTEGER NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
    checked_at       TEXT    NOT NULL,
    check_type       TEXT    NOT NULL CHECK (check_type IN ('ICMP', 'TCP')),
    port             INTEGER,
    status           TEXT    NOT NULL
                     CHECK (status IN ('SUCCESS','TIMEOUT','UNREACHABLE','REFUSED','ERROR')),
    response_time_ms REAL    CHECK (response_time_ms IS NULL OR response_time_ms >= 0),
    error_message    TEXT,
    CONSTRAINT ck_log_port_matches_type CHECK (
        (check_type = 'ICMP' AND port IS NULL) OR
        (check_type = 'TCP' AND port BETWEEN 1 AND 65535)
    )
);

CREATE TABLE IF NOT EXISTS status_changes (
    change_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id       INTEGER NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
    changed_at      TEXT    NOT NULL,
    kind            TEXT    NOT NULL CHECK (kind IN ('INITIAL', 'CHANGE')),
    previous_status TEXT,
    new_status      TEXT    NOT NULL,
    reason          TEXT
);

CREATE INDEX IF NOT EXISTS idx_logs_device_time ON monitoring_logs (device_id, checked_at);
CREATE INDEX IF NOT EXISTS idx_logs_time        ON monitoring_logs (checked_at);
CREATE INDEX IF NOT EXISTS idx_changes_device_time ON status_changes (device_id, changed_at);
"""


def connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open a connection with foreign keys enforced and WAL mode enabled."""
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)      # wait up to 10 s if the database is busy
    conn.row_factory = sqlite3.Row                # rows can be read by column name
    conn.execute("PRAGMA foreign_keys = ON")      # OFF by default in SQLite!
    conn.execute("PRAGMA journal_mode = WAL")     # readers and a writer can work together
    return conn


@contextmanager
def transaction(db_path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    """Open a connection, commit on success, roll back on any error, always close."""
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_schema(db_path: Optional[Path] = None) -> None:
    """Create all tables and indexes if they do not exist yet (safe to run repeatedly)."""
    with transaction(db_path) as conn:
        conn.executescript(SCHEMA)


# ---------------------------------------------------------------------------
# `python -m app.database.db`            creates the real database and lists it
# `python -m app.database.db --selftest` proves the constraints work (temporary DB)
# ---------------------------------------------------------------------------

def _describe() -> None:
    init_schema()
    with transaction() as conn:
        tables = [r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        indexes = [r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND name LIKE 'idx_%' ORDER BY name")]
        foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
    print(f"Database file : {DB_PATH}")
    print(f"Tables        : {', '.join(tables)}")
    print(f"Indexes       : {', '.join(indexes)}")
    print(f"Foreign keys  : {'ON' if foreign_keys else 'OFF'}")
    print(f"Journal mode  : {journal}")


def _selftest() -> None:
    now = "2026-10-20T10:00:00+00:00"
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "selftest.db"
        init_schema(path)

        with transaction(path) as conn:
            conn.execute("INSERT INTO devices (device_name, ip_address, device_type, date_added) "
                         "VALUES (?, ?, ?, ?)", ("Laptop", "127.0.0.1", "workstation", now))
        print("Inserted one valid device.\n")

        attempts = {
            "duplicate IP": (
                "INSERT INTO devices (device_name, ip_address, date_added) VALUES (?, ?, ?)",
                ("Copy", "127.0.0.1", now)),
            "bad device type": (
                "INSERT INTO devices (device_name, ip_address, device_type, date_added) "
                "VALUES (?, ?, ?, ?)", ("X", "10.0.0.9", "toaster", now)),
            "port 70000": (
                "INSERT INTO device_ports (device_id, port) VALUES (?, ?)", (1, 70000)),
            "log for missing device": (
                "INSERT INTO monitoring_logs (device_id, checked_at, check_type, status) "
                "VALUES (?, ?, ?, ?)", (999, now, "ICMP", "SUCCESS")),
            "ICMP log with a port": (
                "INSERT INTO monitoring_logs (device_id, checked_at, check_type, port, status) "
                "VALUES (?, ?, ?, ?, ?)", (1, now, "ICMP", 80, "SUCCESS")),
        }
        print("Rejected by the database:")
        for label, (sql, params) in attempts.items():
            try:
                with transaction(path) as conn:
                    conn.execute(sql, params)
                print(f"  {label}: ACCEPTED (unexpected)")
            except sqlite3.IntegrityError as exc:
                print(f"  {label}: rejected - {exc}")

        with transaction(path) as conn:
            temp_id = conn.execute(
                "INSERT INTO devices (device_name, ip_address, date_added) VALUES (?, ?, ?)",
                ("Temp", "10.0.0.50", now)).lastrowid
            conn.execute("INSERT INTO monitoring_logs (device_id, checked_at, check_type, status) "
                         "VALUES (?, ?, 'ICMP', 'SUCCESS')", (temp_id, now))
            conn.execute("DELETE FROM devices WHERE device_id = ?", (temp_id,))
            left = conn.execute("SELECT COUNT(*) FROM monitoring_logs WHERE device_id = ?",
                                (temp_id,)).fetchone()[0]
        print(f"\nCascade delete: logs left for the deleted device = {left}")


if __name__ == "__main__":
    import sys

    _selftest() if "--selftest" in sys.argv else _describe()