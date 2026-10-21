import argparse
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

from app.config import DB_PATH
from app.database.logs import LogRepository
from app.devices.manager import Device
from app.devices.repository import DeviceRepository
from app.monitoring.runner import DeviceReport
from app.monitoring.scheduler import MonitorLoop
from app.monitoring.tracker import StatusChange, StatusTracker

log = logging.getLogger(__name__)

RoundCallback = Callable[[List[StatusChange], int], None]


class MonitoringService:
    """Wires devices, checks, status tracking and storage together."""

    def __init__(self, db_path: Optional[Path] = None, interval_s: float = 30.0,
                 timeout_s: float = 3.0, confirmations: int = 2,
                 on_round: Optional[RoundCallback] = None):
        self.devices = DeviceRepository(db_path)
        self.logs = LogRepository(db_path)
        self.tracker = StatusTracker(confirmations=confirmations)
        self._on_round_done = on_round

        # Resume where we left off: remember each device's last confirmed status.
        for row in self.logs.current_statuses():
            self.tracker.restore(row["device_id"], row["status"])

        self._loop = MonitorLoop(self.devices, interval_s=interval_s, timeout_s=timeout_s,
                                 on_round=self._handle_round)

    # ---- lifecycle --------------------------------------------------------
    def start(self) -> bool:
        return self._loop.start()

    def stop(self) -> None:
        self._loop.stop()

    @property
    def is_running(self) -> bool:
        return self._loop.is_running

    @property
    def rounds_completed(self) -> int:
        return self._loop.rounds_completed

    # ---- one monitoring round ---------------------------------------------
    def _handle_round(self, reports: List[DeviceReport]) -> None:
        saved = self.logs.save_reports(reports)
        changes = self.tracker.process_round(reports)
        try:
            self.logs.save_changes(changes)
        except Exception:
            log.error("Could not save status changes: %s", changes)
            raise
        if self._on_round_done is not None:
            self._on_round_done(changes, saved)


# ---------------------------------------------------------------------------
# Command-line interface:  python -m app.monitoring.service <command>
# ---------------------------------------------------------------------------

def _fmt_ms(value: Optional[float]) -> str:
    return f"{value:.1f} ms" if value is not None else "-"


def _print_round(changes: List[StatusChange], saved: int) -> None:
    print(f"[{datetime.now():%H:%M:%S}] saved {saved} check result(s)")
    for c in changes:
        if c.kind == "INITIAL":
            print(f"           INITIAL {c.device_name}: {c.current}")
        else:
            print(f"           CHANGE  {c.device_name}: {c.previous} -> {c.current}")


def _cmd_add(args) -> int:
    device = DeviceRepository().add(Device(
        args.name, args.ip, args.device_type, hostname=args.hostname, ports=args.port))
    print(f"Added [{device.device_id}] {device.device_name} ({device.ip_address}) "
          f"type={device.device_type} ports={device.ports}")
    return 0


def _cmd_list(_args) -> int:
    devices = DeviceRepository().list()
    if not devices:
        print("No devices registered yet.")
        return 0
    print(f"{'ID':<4}{'Name':<16}{'IP':<16}{'Type':<13}{'Ports':<14}Active")
    for d in devices:
        ports = ",".join(str(p) for p in d.ports) or "-"
        print(f"{d.device_id:<4}{d.device_name:<16}{d.ip_address:<16}{d.device_type:<13}"
              f"{ports:<14}{'yes' if d.is_active else 'no'}")
    return 0


def _cmd_run(args) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    service = MonitoringService(interval_s=args.interval, timeout_s=args.timeout,
                                confirmations=args.confirmations, on_round=_print_round)
    active = service.devices.list(active_only=True)
    if not active:
        print('No active devices. Add one first, for example:\n'
              '  python -m app.monitoring.service add "My laptop" 127.0.0.1 --port 8080')
        return 1
    print(f"Database: {DB_PATH}")
    print(f"Monitoring {len(active)} device(s) every {args.interval:g} s; "
          f"a status change needs {args.confirmations} consecutive rounds. Ctrl+C to stop.")
    service.start()
    try:
        while service.is_running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping... waiting for the current round to finish.")
    finally:
        service.stop()
        print(f"Monitor stopped cleanly after {service.rounds_completed} round(s).")
    return 0


def _cmd_history(args) -> int:
    rows = LogRepository().query_logs(
        device_id=args.device, status=args.status, check_type=args.check_type,
        start=args.start, end=args.end, limit=args.limit)
    if not rows:
        print("No matching results.")
        return 0
    print(f"{'Time (UTC)':<27}{'Device':<16}{'Type':<6}{'Port':<6}{'Status':<13}Response")
    for r in rows:
        print(f"{r['checked_at']:<27}{r['device_name']:<16}{r['check_type']:<6}"
              f"{str(r['port'] or '-'):<6}{r['status']:<13}{_fmt_ms(r['response_time_ms'])}")
    return 0


def _cmd_changes(args) -> int:
    rows = LogRepository().query_changes(
        device_id=args.device, start=args.start, end=args.end, limit=args.limit)
    if not rows:
        print("No status changes recorded.")
        return 0
    print(f"{'Time (UTC)':<27}{'Device':<16}{'Kind':<9}{'From':<27}To")
    for r in rows:
        print(f"{r['changed_at']:<27}{r['device_name']:<16}{r['kind']:<9}"
              f"{(r['previous_status'] or '-'):<27}{r['new_status']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.monitoring.service",
        description="LAN monitor: manage registered devices and monitor them.")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="register a device (private LAN addresses only)")
    add.add_argument("name")
    add.add_argument("ip")
    add.add_argument("--type", dest="device_type", default="other")
    add.add_argument("--hostname")
    add.add_argument("--port", type=int, action="append", default=[],
                     help="TCP port to check (repeat for several)")

    sub.add_parser("list", help="show registered devices")

    run = sub.add_parser("run", help="monitor all active devices until Ctrl+C")
    run.add_argument("--interval", type=float, default=30.0, help="seconds between rounds")
    run.add_argument("--timeout", type=float, default=3.0, help="seconds per check")
    run.add_argument("--confirmations", type=int, default=2,
                     help="consecutive rounds needed to confirm a status change")

    history = sub.add_parser("history", help="show saved check results (newest first)")
    history.add_argument("--device", type=int)
    history.add_argument("--status")
    history.add_argument("--check-type", dest="check_type")
    history.add_argument("--start")
    history.add_argument("--end")
    history.add_argument("--limit", type=int, default=20)

    changes = sub.add_parser("changes", help="show recorded status changes (newest first)")
    changes.add_argument("--device", type=int)
    changes.add_argument("--start")
    changes.add_argument("--end")
    changes.add_argument("--limit", type=int, default=20)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    commands = {"add": _cmd_add, "list": _cmd_list, "run": _cmd_run,
                "history": _cmd_history, "changes": _cmd_changes}
    try:
        return commands[args.command](args)
    except (ValueError, LookupError) as exc:
        print(f"Error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())