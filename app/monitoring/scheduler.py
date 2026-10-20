import logging
import threading
import time
from datetime import datetime
from typing import Callable, List, Optional

from app.devices.manager import Device, DeviceRegistry
from app.monitoring.runner import DeviceReport, check_all

log = logging.getLogger(__name__)

MIN_INTERVAL_S = 5


class MonitorLoop:
    """Runs check_all repeatedly in a background thread."""

    def __init__(self, registry: DeviceRegistry, interval_s: float = 30.0,
                 timeout_s: float = 3.0,
                 on_round: Optional[Callable[[List[DeviceReport]], None]] = None):
        if interval_s < MIN_INTERVAL_S:
            raise ValueError(f"Interval must be at least {MIN_INTERVAL_S} seconds")
        if timeout_s <= 0:
            raise ValueError("Timeout must be greater than zero")
        self._registry = registry
        self.interval_s = interval_s
        self.timeout_s = timeout_s
        self._on_round = on_round
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self.rounds_completed = 0

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        """Start the loop. Returns False (and does nothing) if it is already running."""
        with self._lock:
            if self.is_running:
                return False
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, name="lan-monitor-loop")
            self._thread.start()
            return True

    def stop(self) -> None:
        """Ask the loop to stop and wait for the current round to finish."""
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            started = time.monotonic()
            try:
                reports = check_all(self._registry.list(), timeout_s=self.timeout_s)
                self.rounds_completed += 1
                if self._on_round is not None:
                    self._on_round(reports)
            except Exception:
                log.exception("Monitoring round failed")
            elapsed = time.monotonic() - started
            # Wait out the rest of the interval, but wake immediately if stop() is called.
            self._stop_event.wait(max(0.0, self.interval_s - elapsed))


if __name__ == "__main__":
    registry = DeviceRegistry()
    registry.add(Device("Local machine", "127.0.0.1", "workstation", ports=[8080]))
    registry.add(Device("Test 1", "192.0.2.1", "other", ports=[80]))

    def show(reports: List[DeviceReport]) -> None:
        print(f"\n[{datetime.now():%H:%M:%S}] Round complete")
        for r in reports:
            print(f"  {r.device_name:<14} {r.verdict}")

    loop = MonitorLoop(registry, interval_s=10, timeout_s=3, on_round=show)
    print("First start() accepted: ", loop.start())
    print("Second start() accepted:", loop.start(), "(duplicate loop refused)")
    print("Monitoring every 10 s. Press Ctrl+C to stop.")

    try:
        while loop.is_running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping... waiting for the current round to finish.")
    finally:
        loop.stop()
        print(f"Monitor stopped cleanly after {loop.rounds_completed} round(s).")