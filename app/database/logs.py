from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.database.db import init_schema, transaction
from app.monitoring.result import CheckResult
from app.monitoring.runner import DeviceReport
from app.monitoring.tracker import StatusChange

ALLOWED_STATUSES = {"SUCCESS", "TIMEOUT", "UNREACHABLE", "REFUSED", "ERROR"}
ALLOWED_CHECK_TYPES = {"ICMP", "TCP"}
DEFAULT_LIMIT = 500
MAX_LIMIT = 1000


class LogQueryError(ValueError):
    """Raised when a history filter is invalid."""


def _to_utc_iso(value: str, end_of_day: bool) -> str:
    """Turn '2026-10-20' or an ISO timestamp into the stored UTC format."""
    text = value.strip()
    try:
        if len(text) == 10:                                   # date only
            clock = time(23, 59, 59) if end_of_day else time(0, 0, 0)
            moment = datetime.combine(date.fromisoformat(text), clock)
        else:
            moment = datetime.fromisoformat(text)
    except ValueError:
        raise LogQueryError(
            f"Invalid date {value!r} (use YYYY-MM-DD or an ISO timestamp)") from None
    if moment.tzinfo is None:                                 # no timezone given: assume UTC
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _where(conditions: List[Tuple[str, Any]]) -> Tuple[str, List[Any]]:
    """Join fixed SQL fragments with AND; values travel separately as parameters."""
    if not conditions:
        return "", []
    return " WHERE " + " AND ".join(sql for sql, _ in conditions), [v for _, v in conditions]


def _page(limit: int, offset: int) -> Tuple[int, int]:
    return max(1, min(int(limit), MAX_LIMIT)), max(0, int(offset))


def _time_range(column: str, start: Optional[str], end: Optional[str]) -> List[Tuple[str, Any]]:
    conditions: List[Tuple[str, Any]] = []
    start_iso = _to_utc_iso(start, end_of_day=False) if start else None
    end_iso = _to_utc_iso(end, end_of_day=True) if end else None
    if start_iso and end_iso and start_iso > end_iso:
        raise LogQueryError("Start date must not be after the end date")
    if start_iso:
        conditions.append((f"{column} >= ?", start_iso))
    if end_iso:
        conditions.append((f"{column} <= ?", end_iso))
    return conditions


class LogRepository:
    """Saves monitoring results and status changes, and answers history queries."""

    def __init__(self, db_path: Optional[Path] = None):
        self._db_path = db_path
        init_schema(db_path)

    # ---- saving -----------------------------------------------------------
    def save_reports(self, reports: List[DeviceReport]) -> int:
        """Save one whole monitoring round atomically. Returns the number of rows saved."""
        rows = []
        for report in reports:
            for check in [report.ping] + list(report.tcp_results):   # ping first, then TCP
                rows.append((report.device_id, check.checked_at, check.check_type, check.port,
                             check.status, check.response_time_ms, check.error_message))
        if not rows:
            return 0
        with transaction(self._db_path) as conn:
            conn.executemany(
                "INSERT INTO monitoring_logs (device_id, checked_at, check_type, port, status, "
                "response_time_ms, error_message) VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        return len(rows)

    def save_changes(self, changes: List[StatusChange]) -> int:
        rows = [(c.device_id, c.changed_at, c.kind, c.previous, c.current, c.reason)
                for c in changes]
        if not rows:
            return 0
        with transaction(self._db_path) as conn:
            conn.executemany(
                "INSERT INTO status_changes (device_id, changed_at, kind, previous_status, "
                "new_status, reason) VALUES (?, ?, ?, ?, ?, ?)", rows)
        return len(rows)

    # ---- history queries --------------------------------------------------
    def query_logs(self, device_id: Optional[int] = None, status: Optional[str] = None,
                   check_type: Optional[str] = None, start: Optional[str] = None,
                   end: Optional[str] = None, limit: int = DEFAULT_LIMIT,
                   offset: int = 0) -> List[Dict[str, Any]]:
        conditions: List[Tuple[str, Any]] = []
        if device_id is not None:
            conditions.append(("l.device_id = ?", device_id))
        if status is not None:
            status = status.strip().upper()
            if status not in ALLOWED_STATUSES:
                raise LogQueryError(f"Unknown status {status!r} "
                                    f"(allowed: {', '.join(sorted(ALLOWED_STATUSES))})")
            conditions.append(("l.status = ?", status))
        if check_type is not None:
            check_type = check_type.strip().upper()
            if check_type not in ALLOWED_CHECK_TYPES:
                raise LogQueryError(f"Unknown check type {check_type!r} "
                                    f"(allowed: {', '.join(sorted(ALLOWED_CHECK_TYPES))})")
            conditions.append(("l.check_type = ?", check_type))
        conditions += _time_range("l.checked_at", start, end)

        where, params = _where(conditions)
        limit, offset = _page(limit, offset)
        sql = ("SELECT l.log_id, l.device_id, d.device_name, d.ip_address, l.checked_at, "
               "l.check_type, l.port, l.status, l.response_time_ms, l.error_message "
               "FROM monitoring_logs l JOIN devices d ON d.device_id = l.device_id"
               + where + " ORDER BY l.checked_at DESC, l.log_id DESC LIMIT ? OFFSET ?")
        with transaction(self._db_path) as conn:
            return [dict(row) for row in conn.execute(sql, params + [limit, offset])]

    def query_changes(self, device_id: Optional[int] = None, start: Optional[str] = None,
                      end: Optional[str] = None, limit: int = DEFAULT_LIMIT,
                      offset: int = 0) -> List[Dict[str, Any]]:
        conditions: List[Tuple[str, Any]] = []
        if device_id is not None:
            conditions.append(("c.device_id = ?", device_id))
        conditions += _time_range("c.changed_at", start, end)

        where, params = _where(conditions)
        limit, offset = _page(limit, offset)
        sql = ("SELECT c.change_id, c.device_id, d.device_name, d.ip_address, c.changed_at, "
               "c.kind, c.previous_status, c.new_status, c.reason "
               "FROM status_changes c JOIN devices d ON d.device_id = c.device_id"
               + where + " ORDER BY c.changed_at DESC, c.change_id DESC LIMIT ? OFFSET ?")
        with transaction(self._db_path) as conn:
            return [dict(row) for row in conn.execute(sql, params + [limit, offset])]

    # ---- "right now" views ------------------------------------------------
    def latest_per_device(self) -> List[Dict[str, Any]]:
        """The most recent check saved for each device."""
        sql = ("SELECT * FROM ("
               " SELECT l.log_id, l.device_id, d.device_name, d.ip_address, l.checked_at, "
               "  l.check_type, l.port, l.status, l.response_time_ms, l.error_message, "
               "  ROW_NUMBER() OVER (PARTITION BY l.device_id "
               "   ORDER BY l.checked_at DESC, l.log_id DESC) AS rn "
               " FROM monitoring_logs l JOIN devices d ON d.device_id = l.device_id"
               ") WHERE rn = 1 ORDER BY device_id")
        with transaction(self._db_path) as conn:
            rows = [dict(row) for row in conn.execute(sql)]
        for row in rows:
            row.pop("rn")
        return rows

    def current_statuses(self) -> List[Dict[str, Any]]:
        """The latest confirmed status (from status_changes) for each device."""
        sql = ("SELECT * FROM ("
               " SELECT c.device_id, d.device_name, d.ip_address, c.new_status AS status, "
               "  c.changed_at, c.reason, "
               "  ROW_NUMBER() OVER (PARTITION BY c.device_id "
               "   ORDER BY c.changed_at DESC, c.change_id DESC) AS rn "
               " FROM status_changes c JOIN devices d ON d.device_id = c.device_id"
               ") WHERE rn = 1 ORDER BY device_id")
        with transaction(self._db_path) as conn:
            rows = [dict(row) for row in conn.execute(sql)]
        for row in rows:
            row.pop("rn")
        return rows


if __name__ == "__main__":
    import tempfile

    from app.devices.manager import Device
    from app.devices.repository import DeviceRepository

    def fake_report(device: Device, when: str, ping_status: str, ping_ms: Optional[float],
                    tcp_status: str, tcp_ms: Optional[float]) -> DeviceReport:
        port = device.ports[0]

        def note(status: str) -> Optional[str]:
            return None if status == "SUCCESS" else f"simulated {status}"

        ping = CheckResult(device.ip_address, "ICMP", ping_status, response_time_ms=ping_ms,
                           error_message=note(ping_status), checked_at=when)
        tcp = CheckResult(f"{device.ip_address}:{port}", "TCP", tcp_status,
                          response_time_ms=tcp_ms, error_message=note(tcp_status),
                          checked_at=when, port=port)
        return DeviceReport(device.device_id, device.device_name, device.ip_address,
                            ping, [tcp], "demo", "demo", when)

    with tempfile.TemporaryDirectory() as folder:
        db_file = Path(folder) / "logs_demo.db"
        devices = DeviceRepository(db_file)
        laptop = devices.add(Device("My laptop", "127.0.0.1", "workstation", ports=[8080]))
        silent = devices.add(Device("Test address", "192.0.2.1", ports=[80]))
        logs = LogRepository(db_file)

        days = ["2026-10-18T10:00:00+00:00", "2026-10-19T10:00:00+00:00",
                "2026-10-20T10:00:00+00:00"]
        for number, when in enumerate(days):
            last_day = number == 2
            logs.save_reports([
                fake_report(laptop, when, "SUCCESS", 0.5,
                            "REFUSED" if last_day else "SUCCESS", 2040.0 if last_day else 5.2),
                fake_report(silent, when, "TIMEOUT", None, "TIMEOUT", None),
            ])
        logs.save_changes([
            StatusChange(laptop.device_id, laptop.device_name, "INITIAL", None,
                         "REACHABLE", "demo", days[0]),
            StatusChange(silent.device_id, silent.device_name, "INITIAL", None,
                         "INCONCLUSIVE", "demo", days[0]),
            StatusChange(laptop.device_id, laptop.device_name, "CHANGE", "REACHABLE",
                         "REACHABLE (SERVICE DOWN)", "demo", days[2]),
        ])

        def count(label: str, **filters: Any) -> None:
            print(f"  {label:<30}: {len(logs.query_logs(**filters))}")

        print("Saved 3 rounds for 2 devices.\n")
        print("History queries (rows returned):")
        count("All logs")
        count("Device 1 only", device_id=laptop.device_id)
        count("Status TIMEOUT", status="TIMEOUT")
        count("Status REFUSED", status="refused")
        count("Device 1, TCP only", device_id=laptop.device_id, check_type="TCP")
        count("Date 2026-10-19 (whole day)", start="2026-10-19", end="2026-10-19")
        count("From 2026-10-20 onwards", start="2026-10-20")
        count("Limit 5", limit=5)

        newest = logs.query_logs(limit=1)[0]
        print(f"\nNewest log first: {newest['checked_at']} {newest['device_name']} "
              f"{newest['check_type']} {newest['status']}")

        print(f"\nStatus changes recorded: {len(logs.query_changes())}")
        print("Current status per device:")
        for row in logs.current_statuses():
            print(f"  {row['device_name']:<14} {row['status']}")
        print("Latest check per device:")
        for row in logs.latest_per_device():
            print(f"  {row['device_name']:<14} {row['check_type']:<5} "
                  f"port={row['port']} {row['status']}")

        print("\nRejected filters:")
        bad_filters = {
            "unknown status": dict(status="BOGUS"),
            "injection attempt": dict(status="SUCCESS' OR '1'='1"),
            "unknown check type": dict(check_type="UDP"),
            "bad date": dict(start="yesterday"),
            "start after end": dict(start="2026-10-20", end="2026-10-19"),
        }
        for label, filters in bad_filters.items():
            try:
                logs.query_logs(**filters)
                print(f"  {label}: ACCEPTED (unexpected)")
            except LogQueryError as exc:
                print(f"  {label}: rejected - {exc}")