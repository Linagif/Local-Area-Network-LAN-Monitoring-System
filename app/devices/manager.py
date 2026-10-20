import ipaddress
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

ALLOWED_DEVICE_TYPES = {"router", "switch", "server", "printer", "workstation", "other"}

# Letters, digits, hyphens and dots; each label 1-63 chars, not starting/ending with a hyphen
_HOSTNAME_RE = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*$")


class DeviceValidationError(ValueError):
    """Raised when device details are invalid or not allowed."""


class DeviceNotFoundError(LookupError):
    """Raised when a device ID is not in the registry."""


@dataclass
class Device:
    device_name: str
    ip_address: str
    device_type: str = "other"
    hostname: Optional[str] = None
    ports: List[int] = field(default_factory=list)   # TCP ports we are allowed to check
    is_active: bool = True
    device_id: Optional[int] = None
    date_added: str = ""

    def __post_init__(self):
        # Name
        self.device_name = (self.device_name or "").strip()
        if not self.device_name:
            raise DeviceValidationError("Device name is required")
        if len(self.device_name) > 100:
            raise DeviceValidationError("Device name must be 100 characters or fewer")

        # IP address: valid, and on a private (LAN) range only
        try:
            ip = ipaddress.ip_address((self.ip_address or "").strip())
        except ValueError:
            raise DeviceValidationError("Invalid IP address") from None
        if ip.is_unspecified or ip.is_multicast:
            raise DeviceValidationError(f"Address {ip} is not a valid device address")
        if not ip.is_private:
            raise DeviceValidationError(
                f"Only private LAN addresses can be registered (got {ip})")
        self.ip_address = str(ip)

        # Device type
        self.device_type = (self.device_type or "").strip().lower()
        if self.device_type not in ALLOWED_DEVICE_TYPES:
            raise DeviceValidationError(
                "Device type must be one of: " + ", ".join(sorted(ALLOWED_DEVICE_TYPES)))

        # Hostname (optional)
        if self.hostname:
            self.hostname = self.hostname.strip()
            if len(self.hostname) > 253 or not _HOSTNAME_RE.match(self.hostname):
                raise DeviceValidationError("Invalid hostname")
        else:
            self.hostname = None

        # Ports: whole numbers 1-65535, no duplicates
        for port in self.ports:
            if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
                raise DeviceValidationError("Ports must be whole numbers from 1 to 65535")
        self.ports = sorted(set(self.ports))

        if not self.date_added:
            self.date_added = datetime.now(timezone.utc).isoformat(timespec="seconds")


class DeviceRegistry:
    """Keeps the list of registered devices. Phase 5 will move this storage into SQLite."""

    def __init__(self):
        self._devices: Dict[int, Device] = {}
        self._next_id = 1

    def add(self, device: Device) -> Device:
        if any(d.ip_address == device.ip_address for d in self._devices.values()):
            raise DeviceValidationError(
                f"A device with IP {device.ip_address} is already registered")
        device.device_id = self._next_id
        self._devices[device.device_id] = device
        self._next_id += 1
        return device

    def get(self, device_id: int) -> Device:
        try:
            return self._devices[device_id]
        except KeyError:
            raise DeviceNotFoundError(f"No device with ID {device_id}") from None

    def list(self, active_only: bool = False) -> List[Device]:
        devices = list(self._devices.values())
        return [d for d in devices if d.is_active] if active_only else devices

    def set_active(self, device_id: int, active: bool) -> Device:
        device = self.get(device_id)
        device.is_active = active
        return device

    def remove(self, device_id: int) -> None:
        self.get(device_id)            # raises if missing
        del self._devices[device_id]


if __name__ == "__main__":
    registry = DeviceRegistry()
    registry.add(Device("My laptop", "127.0.0.1", "workstation", ports=[8080]))
    registry.add(Device("Test address", "192.0.2.1", "other"))

    print("Registered devices:")
    for d in registry.list():
        print(f"  [{d.device_id}] {d.device_name:<13} {d.ip_address:<12} "
              f"ports={d.ports} active={d.is_active}")

    bad_attempts = {
        "bad IP": lambda: Device("X", "999.1.1.1"),
        "public IP": lambda: Device("X", "8.8.8.8"),
        "bad port": lambda: Device("X", "10.0.0.5", ports=[70000]),
        "bad type": lambda: Device("X", "10.0.0.5", device_type="toaster"),
        "empty name": lambda: Device("   ", "10.0.0.5"),
        "duplicate": lambda: registry.add(Device("Again", "127.0.0.1")),
    }
    print("\nRejected inputs:")
    for label, attempt in bad_attempts.items():
        try:
            attempt()
            print(f"  {label}: ACCEPTED (unexpected)")
        except DeviceValidationError as exc:
            print(f"  {label}: rejected - {exc}")