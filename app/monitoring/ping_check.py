import ipaddress
import platform
import re
import subprocess

from app.monitoring.result import CheckResult

# Matches "time=12ms", "time<1ms" and Linux-style "time=0.045 ms"
_TIME_RE = re.compile(r"time\s*([=<])\s*([\d.]+)\s*ms", re.IGNORECASE)


def ping_host(ip_address: str, timeout_ms: int = 2000) -> CheckResult:
    """Send one ICMP echo request to a registered device and classify the outcome."""
    # 1. Validate the input before it reaches the operating system.
    try:
        ip = ipaddress.ip_address(ip_address.strip())
    except ValueError:
        return CheckResult(ip_address, "ICMP", "ERROR",
                           error_message="Invalid IP address")
    target = str(ip)

    # 2. Build the command as a list (never shell=True).
    if platform.system() == "Windows":
        command = ["ping", "-n", "1", "-w", str(timeout_ms), target]
    else:  # Linux/macOS, for teammates or a future server
        command = ["ping", "-c", "1", "-W", str(max(1, timeout_ms // 1000)), target]

    # 3. Run it, with a safety limit so a hung command can't freeze us.
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, errors="replace",
            timeout=timeout_ms / 1000 + 2,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(target, "ICMP", "TIMEOUT",
                           error_message="ping command did not finish in time")
    except FileNotFoundError:
        return CheckResult(target, "ICMP", "ERROR",
                           error_message="ping command not found on this system")

    # 4. Interpret the output. A real reply always contains TTL=.
    output = completed.stdout + completed.stderr
    lowered = output.lower()

    if "ttl=" in lowered:
        match = _TIME_RE.search(output)
        response_time = None
        if match:
            response_time = float(match.group(2))
            if match.group(1) == "<":      # "<1ms" -> store a small non-zero value
                response_time = 0.5
        return CheckResult(target, "ICMP", "SUCCESS", response_time_ms=response_time)

    if "timed out" in lowered or "100% packet loss" in lowered:
        return CheckResult(target, "ICMP", "TIMEOUT",
                           error_message="No ICMP reply (host down, or ICMP blocked)")

    if "unreachable" in lowered:
        return CheckResult(target, "ICMP", "UNREACHABLE",
                           error_message="Destination unreachable")

    first_line = next((ln.strip() for ln in output.splitlines() if ln.strip()), "No output")
    return CheckResult(target, "ICMP", "ERROR", error_message=first_line)


if __name__ == "__main__":
    import sys

    result = ping_host(sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1")
    print(result)