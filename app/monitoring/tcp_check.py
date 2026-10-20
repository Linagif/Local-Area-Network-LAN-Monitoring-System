import ipaddress
import socket
import time

from app.monitoring.result import CheckResult


def tcp_check(ip_address: str, port: int, timeout_s: float = 2.0) -> CheckResult:
    """Try one TCP connection to a registered device's configured port."""
    # 1. Validate the address and the port before touching the network.
    try:
        ip = ipaddress.ip_address(ip_address.strip())
    except ValueError:
        return CheckResult(ip_address, "TCP", "ERROR",
                           error_message="Invalid IP address")
    target = str(ip)

    if not isinstance(port, int) or not (1 <= port <= 65535):
        return CheckResult(target, "TCP", "ERROR",
                           error_message="Port must be a whole number from 1 to 65535")

    label = f"{target}:{port}"

    # 2. Time the connection attempt.
    start = time.perf_counter()
    try:
        # create_connection performs the full TCP handshake, then we close at once.
        with socket.create_connection((target, port), timeout=timeout_s):
            elapsed_ms = (time.perf_counter() - start) * 1000
            return CheckResult(label, "TCP", "SUCCESS",
                               response_time_ms=round(elapsed_ms, 2))

    # 3. Classify each failure separately (order matters, see notes below).
    except ConnectionRefusedError:
        elapsed_ms = (time.perf_counter() - start) * 1000
        return CheckResult(label, "TCP", "REFUSED",
                           response_time_ms=round(elapsed_ms, 2),
                           error_message="Host reachable, but nothing is accepting connections on this port")
    except (socket.timeout, TimeoutError):
        return CheckResult(label, "TCP", "TIMEOUT",
                           error_message="No response (host down, or a firewall is dropping packets)")
    except OSError as exc:
        return CheckResult(label, "TCP", "ERROR", error_message=str(exc))


if __name__ == "__main__":
    import sys

    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port_number = int(sys.argv[2]) if len(sys.argv) > 2 else 80
    print(tcp_check(host, port_number))