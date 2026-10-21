import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

from app.devices.manager import Device, DeviceRegistry
from app.monitoring.result import CheckResult
from app.monitoring.runner import DeviceReport
from app.monitoring.scheduler import MonitorLoop


@dataclass
class StatusChange:
    """One event worth recording: a device's first observation, or a confirmed change."""

    device_id: int
    device_name: str
    kind: str                    # "INITIAL" or "CHANGE"
    previous: Optional[str]
    current: str
    reason: str
    changed_at: str


@dataclass
class _DeviceState:
    confirmed: str               # the status we currently believe
    pending: Optional[str] = None
    pending_count: int = 0       # how many times in a row we have seen the pending status


class StatusTracker:
    """Remembers each device's status and reports only confirmed transitions."""

    def __init__(self, confirmations: int = 2):
        if confirmations < 1:
            raise ValueError("confirmations must be at least 1")
        self.confirmations = confirmations
        self._states: Dict[int, _DeviceState] = {}
        self._lock = threading.Lock()

    def restore(self, device_id: int, status: str) -> None:
        """Seed a device's confirmed status (used when the monitor restarts)."""
        with self._lock:
            self._states[device_id] = _DeviceState(confirmed=status)

    def update(self, report: DeviceReport) -> Optional[StatusChange]:
        verdict = report.verdict
        with self._lock:
            state = self._states.get(report.device_id)

            # First time we see this device: record it as the starting point.
            if state is None:
                self._states[report.device_id] = _DeviceState(confirmed=verdict)
                return self._event(report, "INITIAL", None)

            # Same as before: nothing to report, and any pending doubt is cleared.
            if verdict == state.confirmed:
                state.pending, state.pending_count = None, 0
                return None

            # Different from before: count consecutive sightings of the new status.
            if verdict == state.pending:
                state.pending_count += 1
            else:
                state.pending, state.pending_count = verdict, 1

            if state.pending_count < self.confirmations:
                return None          # not confirmed yet: could be a blip

            previous = state.confirmed
            state.confirmed, state.pending, state.pending_count = verdict, None, 0
            return self._event(report, "CHANGE", previous)

    def process_round(self, reports: List[DeviceReport]) -> List[StatusChange]:
        """Feed a whole monitoring round in; get back only the events."""
        events = (self.update(report) for report in reports)
        return [event for event in events if event is not None]

    def current_status(self, device_id: int) -> Optional[str]:
        with self._lock:
            state = self._states.get(device_id)
            return state.confirmed if state else None

    @staticmethod
    def _event(report: DeviceReport, kind: str, previous: Optional[str]) -> StatusChange:
        return StatusChange(report.device_id, report.device_name, kind, previous,
                            report.verdict, report.reason, report.checked_at)


# ---------------------------------------------------------------------------
# Demonstrations: `python -m app.monitoring.tracker` (simulation, no network)
#                 `python -m app.monitoring.tracker --live` (real checks)
# ---------------------------------------------------------------------------

def _fake_report(verdict: str) -> DeviceReport:
    ping = CheckResult("127.0.0.1", "ICMP", "SUCCESS", response_time_ms=0.5)
    return DeviceReport(1, "Local machine", "127.0.0.1", ping, [], verdict,
                        f"simulated: {verdict}", ping.checked_at)


def simulate() -> None:
    sequence = [
        "REACHABLE",
        "REACHABLE",
        "REACHABLE (SERVICE DOWN)",   # a one-round blip
        "REACHABLE",
        "REACHABLE (SERVICE DOWN)",   # a real outage begins...
        "REACHABLE (SERVICE DOWN)",   # ...and is confirmed here
        "REACHABLE (SERVICE DOWN)",   # still down: must stay quiet
        "REACHABLE",
        "REACHABLE",                  # recovery confirmed
    ]
    tracker = StatusTracker(confirmations=2)
    print(f"Simulating {len(sequence)} rounds (confirmations=2)\n")
    for number, verdict in enumerate(sequence, start=1):
        change = tracker.update(_fake_report(verdict))
        if change is None:
            outcome = "no event"
        elif change.kind == "INITIAL":
            outcome = f"INITIAL -> {change.current}"
        else:
            outcome = f"CHANGE: {change.previous} -> {change.current}"
        print(f"  round {number}: observed {verdict:<26} {outcome}")


def live() -> None:
    registry = DeviceRegistry()
    registry.add(Device("Local machine", "127.0.0.1", "workstation", ports=[8080]))
    registry.add(Device("Test 1", "192.0.2.1", "other", ports=[80]))
    tracker = StatusTracker(confirmations=2)

    def on_round(reports: List[DeviceReport]) -> None:
        changes = tracker.process_round(reports)
        stamp = datetime.now().strftime("%H:%M:%S")
        for c in changes:
            if c.kind == "INITIAL":
                print(f"[{stamp}] INITIAL {c.device_name}: {c.current}")
            else:
                print(f"[{stamp}] CHANGE  {c.device_name}: {c.previous} -> {c.current}")
        if not changes:
            print(f"[{stamp}] round complete, no changes")

    loop = MonitorLoop(registry, interval_s=10, timeout_s=3, on_round=on_round)
    loop.start()
    print("Monitoring every 10 s; changes need 2 consecutive rounds. Press Ctrl+C to stop.")
    try:
        while loop.is_running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping... waiting for the current round to finish.")
    finally:
        loop.stop()
        print(f"Monitor stopped cleanly after {loop.rounds_completed} round(s).")


if __name__ == "__main__":
    live() if "--live" in sys.argv else simulate()