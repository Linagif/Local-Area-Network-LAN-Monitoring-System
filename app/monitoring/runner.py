import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from app.devices.manager import Device, DeviceRegistry
from app.monitoring.diagnose import interpret
from app.monitoring.ping_check import ping_host
from app.monitoring.result import CheckResult
from app.monitoring.tcp_check import tcp_check

# When a device has several ports, the most informative TCP result wins:
# SUCCESS (service up) > REFUSED (host alive) > anything else.
_TCP_RANK = {"SUCCESS": 0, "REFUSED": 1}


@dataclass
class DeviceReport:
    """Everything we learned about one device in one monitoring round."""

    device_id: int
    device_name: str
    ip_address: str
    ping: CheckResult
    tcp_results: List[CheckResult]
    verdict: str
    reason: str
    checked_at: str


def _best_tcp(results: List[CheckResult]) -> Optional[CheckResult]:
    if not results:
        return None
    return min(results, key=lambda r: _TCP_RANK.get(r.status, 2))


def check_device(device: Device, timeout_s: float = 3.0) -> DeviceReport:
    """Run ICMP plus every configured TCP port check for one registered device."""
    ping = ping_host(device.ip_address, timeout_ms=int(timeout_s * 1000))
    tcp_results = [tcp_check(device.ip_address, port, timeout_s=timeout_s)
                   for port in device.ports]
    verdict, reason = interpret(ping, _best_tcp(tcp_results))
    return DeviceReport(
        device_id=device.device_id,
        device_name=device.device_name,
        ip_address=device.ip_address,
        ping=ping,
        tcp_results=tcp_results,
        verdict=verdict,
        reason=reason,
        checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def _error_report(device: Device, exc: Exception) -> DeviceReport:
    """Used when a check crashes unexpectedly, so one device can't break the round."""
    failed = CheckResult(device.ip_address, "ICMP", "ERROR",
                         error_message=f"Unexpected error: {exc}")
    return DeviceReport(device.device_id, device.device_name, device.ip_address,
                        failed, [], "ERROR", f"Check failed unexpectedly: {exc}",
                        failed.checked_at)


def check_all(devices: List[Device], timeout_s: float = 3.0,
              max_workers: int = 10) -> List[DeviceReport]:
    """Check all registered, active devices concurrently."""
    # Authorization by design: only registered (has an ID) and enabled devices are checked.
    eligible = [d for d in devices if d.device_id is not None and d.is_active]
    if not eligible:
        return []

    workers = max(1, min(max_workers, len(eligible)))
    reports: List[DeviceReport] = []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(check_device, d, timeout_s): d for d in eligible}
        for future in as_completed(futures):
            device = futures[future]
            try:
                reports.append(future.result())
            except Exception as exc:        # a crash in one thread must not stop the rest
                reports.append(_error_report(device, exc))

    reports.sort(key=lambda r: r.device_id)  # results arrive in finishing order; sort for display
    return reports


if __name__ == "__main__":
    registry = DeviceRegistry()
    registry.add(Device("Local machine", "127.0.0.1", "workstation", ports=[8080]))
    registry.add(Device("Test 1", "192.0.2.1", "other", ports=[80]))
    registry.add(Device("Test 2", "192.0.2.2", "other", ports=[80]))
    registry.add(Device("Test 3", "192.0.2.3", "other", ports=[80]))
    disabled = registry.add(Device("Disabled", "192.0.2.4", "other", ports=[80]))
    registry.set_active(disabled.device_id, False)

    started = time.perf_counter()
    reports = check_all(registry.list(), timeout_s=3.0)
    elapsed = time.perf_counter() - started

    skipped = len(registry.list()) - len(reports)
    print(f"Checked {len(reports)} active devices in {elapsed:.1f} s "
          f"({skipped} inactive skipped)\n")
    print(f"{'ID':<3} {'Name':<14} {'IP':<12} {'Verdict':<30} Details")
    for r in reports:
        details = f"ping={r.ping.status}"
        for t in r.tcp_results:
            details += f", {t.target}={t.status}"
        print(f"{r.device_id:<3} {r.device_name:<14} {r.ip_address:<12} {r.verdict:<30} {details}")