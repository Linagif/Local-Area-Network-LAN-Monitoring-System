import sqlite3
from pathlib import Path
from typing import Dict, List, Optional

from app.database.db import init_schema, transaction
from app.devices.manager import Device, DeviceNotFoundError, DeviceValidationError

EDITABLE_FIELDS = {"device_name", "ip_address", "hostname", "device_type", "ports", "is_active"}


class DeviceRepository:
    """Stores registered devices in SQLite. Same interface as DeviceRegistry, plus update()."""

    def __init__(self, db_path: Optional[Path] = None):
        self._db_path = db_path
        init_schema(db_path)

    # ---- create -----------------------------------------------------------
    def add(self, device: Device) -> Device:
        try:
            with transaction(self._db_path) as conn:
                device_id = conn.execute(
                    "INSERT INTO devices (device_name, ip_address, hostname, device_type, "
                    "is_active, date_added) VALUES (?, ?, ?, ?, ?, ?)",
                    (device.device_name, device.ip_address, device.hostname,
                     device.device_type, int(device.is_active), device.date_added),
                ).lastrowid
                self._save_ports(conn, device_id, device.ports)
        except sqlite3.IntegrityError as exc:
            raise self._translate(exc, device.ip_address) from None
        device.device_id = device_id
        return device

    # ---- read -------------------------------------------------------------
    def get(self, device_id: int) -> Device:
        with transaction(self._db_path) as conn:
            row = conn.execute("SELECT * FROM devices WHERE device_id = ?",
                               (device_id,)).fetchone()
            ports = self._load_ports(conn, device_id)
        if row is None:
            raise DeviceNotFoundError(f"No device with ID {device_id}")
        return self._to_device(row, ports.get(device_id, []))

    def list(self, active_only: bool = False) -> List[Device]:
        sql = "SELECT * FROM devices"
        if active_only:
            sql += " WHERE is_active = 1"
        sql += " ORDER BY device_id"
        with transaction(self._db_path) as conn:
            rows = conn.execute(sql).fetchall()
            ports = self._load_ports(conn)
        return [self._to_device(row, ports.get(row["device_id"], [])) for row in rows]

    # ---- update -----------------------------------------------------------
    def update(self, device_id: int, **changes) -> Device:
        unknown = set(changes) - EDITABLE_FIELDS
        if unknown:
            raise DeviceValidationError(f"Cannot edit: {', '.join(sorted(unknown))}")

        current = self.get(device_id)
        merged = Device(                      # validation runs again on the new values
            device_name=changes.get("device_name", current.device_name),
            ip_address=changes.get("ip_address", current.ip_address),
            device_type=changes.get("device_type", current.device_type),
            hostname=changes.get("hostname", current.hostname),
            ports=list(changes.get("ports", current.ports)),
            is_active=changes.get("is_active", current.is_active),
            device_id=device_id,
            date_added=current.date_added,
        )
        try:
            with transaction(self._db_path) as conn:
                conn.execute(
                    "UPDATE devices SET device_name = ?, ip_address = ?, hostname = ?, "
                    "device_type = ?, is_active = ? WHERE device_id = ?",
                    (merged.device_name, merged.ip_address, merged.hostname,
                     merged.device_type, int(merged.is_active), device_id))
                conn.execute("DELETE FROM device_ports WHERE device_id = ?", (device_id,))
                self._save_ports(conn, device_id, merged.ports)
        except sqlite3.IntegrityError as exc:
            raise self._translate(exc, merged.ip_address) from None
        return merged

    def set_active(self, device_id: int, active: bool) -> Device:
        with transaction(self._db_path) as conn:
            changed = conn.execute("UPDATE devices SET is_active = ? WHERE device_id = ?",
                                   (int(active), device_id)).rowcount
        if changed == 0:
            raise DeviceNotFoundError(f"No device with ID {device_id}")
        return self.get(device_id)

    # ---- delete -----------------------------------------------------------
    def remove(self, device_id: int) -> None:
        """Delete a device AND (by cascade) its ports, logs and status changes."""
        with transaction(self._db_path) as conn:
            deleted = conn.execute("DELETE FROM devices WHERE device_id = ?",
                                   (device_id,)).rowcount
        if deleted == 0:
            raise DeviceNotFoundError(f"No device with ID {device_id}")

    # ---- helpers ----------------------------------------------------------
    @staticmethod
    def _save_ports(conn: sqlite3.Connection, device_id: int, ports: List[int]) -> None:
        conn.executemany("INSERT INTO device_ports (device_id, port) VALUES (?, ?)",
                         [(device_id, port) for port in ports])

    @staticmethod
    def _load_ports(conn: sqlite3.Connection,
                    device_id: Optional[int] = None) -> Dict[int, List[int]]:
        if device_id is None:
            rows = conn.execute("SELECT device_id, port FROM device_ports "
                                "ORDER BY device_id, port").fetchall()
        else:
            rows = conn.execute("SELECT device_id, port FROM device_ports "
                                "WHERE device_id = ? ORDER BY port", (device_id,)).fetchall()
        ports: Dict[int, List[int]] = {}
        for row in rows:
            ports.setdefault(row["device_id"], []).append(row["port"])
        return ports

    @staticmethod
    def _to_device(row: sqlite3.Row, ports: List[int]) -> Device:
        return Device(
            device_name=row["device_name"], ip_address=row["ip_address"],
            device_type=row["device_type"], hostname=row["hostname"], ports=ports,
            is_active=bool(row["is_active"]), device_id=row["device_id"],
            date_added=row["date_added"],
        )

    @staticmethod
    def _translate(exc: sqlite3.IntegrityError, ip_address: str) -> DeviceValidationError:
        if "ip_address" in str(exc):
            return DeviceValidationError(f"A device with IP {ip_address} is already registered")
        return DeviceValidationError(f"Rejected by the database: {exc}")


if __name__ == "__main__":
    import tempfile

    def show(title: str, devices: List[Device]) -> None:
        print(title)
        for d in devices:
            print(f"  [{d.device_id}] {d.device_name:<17} {d.ip_address:<12} "
                  f"{d.device_type:<12} ports={d.ports} active={d.is_active}")

    with tempfile.TemporaryDirectory() as folder:
        db_file = Path(folder) / "repo_demo.db"
        repo = DeviceRepository(db_file)

        repo.add(Device("My laptop", "127.0.0.1", "workstation", ports=[8080, 80]))
        repo.add(Device("Test address", "192.0.2.1"))
        show("After adding two devices:", repo.list())

        try:
            repo.add(Device("Copy", "127.0.0.1"))
        except DeviceValidationError as exc:
            print(f"\nDuplicate refused: {exc}")

        repo.update(2, device_name="Renamed test", hostname="test.local", ports=[443])
        repo.set_active(2, False)
        show("\nAfter editing and disabling device 2:", repo.list())
        show("\nActive devices only:", repo.list(active_only=True))

        # A brand-new repository object reads the same file: the data persisted.
        show("\nRead back through a new repository object:", DeviceRepository(db_file).list())

        hostile = repo.add(Device("x'); DROP TABLE devices;--", "10.0.0.7"))
        print(f"\nHostile-looking name stored as plain text; devices table still has "
              f"{len(repo.list())} rows.")

        repo.remove(hostile.device_id)
        try:
            repo.get(999)
        except DeviceNotFoundError as exc:
            print(f"Missing device: {exc}")