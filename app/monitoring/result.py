from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass
class CheckResult:
    """The outcome of one connectivity check (ICMP or TCP)."""

    target: str
    check_type: str                      # "ICMP" or "TCP"
    status: str                          # SUCCESS, TIMEOUT, UNREACHABLE, REFUSED, ERROR
    response_time_ms: Optional[float] = None
    error_message: Optional[str] = None
    checked_at: str = ""

    def __post_init__(self):
        if not self.checked_at:
            self.checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")