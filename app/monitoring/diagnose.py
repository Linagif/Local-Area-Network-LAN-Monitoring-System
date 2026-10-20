import argparse
import ipaddress
from typing import Optional

from app.monitoring.ping_check import ping_host
from app.monitoring.result import CheckResult
from app.monitoring.tcp_check import tcp_check


def interpret(ping: CheckResult, tcp: Optional[CheckResult]) -> tuple[str, str]:
    """Combine ping and TCP results into one (verdict, explanation) pair."""
    ping_ok = ping.status == "SUCCESS"

    # Ping only (no port given)
    if tcp is None:
        if ping_ok:
            return "REACHABLE", "The host answered ICMP ping."
        return ("INCONCLUSIVE",
                "No ping reply. The host may be down, or it may be blocking ICMP. "
                "Run again with --port to test a service.")

    # A completed TCP handshake proves the host is up
    if tcp.status == "SUCCESS":
        if ping_ok:
            return "REACHABLE", "The host answers ping and the service accepts connections."
        return ("REACHABLE",
                "The service accepts connections, but ping failed: "
                "ICMP is probably blocked by a firewall.")

    # A refusal can only come from a live host
    if tcp.status == "REFUSED":
        if ping_ok:
            return "REACHABLE (SERVICE DOWN)", "The host answers ping, but nothing listens on that port."
        return ("REACHABLE (SERVICE DOWN)",
                "The host is alive (it actively refused the connection), "
                "ping is blocked, and nothing listens on that port.")

    # TCP timed out or errored
    if ping_ok:
        return ("HOST UP, SERVICE UNREACHABLE",
                "The host answers ping, but the port did not respond: "
                "the service is down or a firewall is blocking the port.")
    return ("INCONCLUSIVE",
            "Neither check got an answer. The host may be down, the address may be wrong, "
            "or a firewall may be blocking both. This does not prove the device is offline.")


def _format_line(label: str, result: CheckResult) -> str:
    timing = f"{result.response_time_ms} ms" if result.response_time_ms is not None else "-"
    note = f"  ({result.error_message})" if result.error_message else ""
    return f"{label:<10}: {result.status:<12}{timing}{note}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Check one authorized device with ICMP ping and an optional TCP port check."
    )
    parser.add_argument("ip", help="IP address of a device you own or may monitor")
    parser.add_argument("--port", type=int, help="TCP port to check (optional)")
    parser.add_argument("--timeout", type=float, default=3.0,
                        help="timeout in seconds for each check (default 3)")
    args = parser.parse_args(argv)

    # Validate before any network traffic
    try:
        ipaddress.ip_address(args.ip.strip())
    except ValueError:
        print(f"Invalid IP address: {args.ip}")
        return 2
    if args.port is not None and not (1 <= args.port <= 65535):
        print("Port must be a whole number from 1 to 65535")
        return 2
    if args.timeout <= 0:
        print("Timeout must be greater than zero")
        return 2

    ping = ping_host(args.ip, timeout_ms=int(args.timeout * 1000))
    tcp = tcp_check(args.ip, args.port, timeout_s=args.timeout) if args.port is not None else None
    verdict, reason = interpret(ping, tcp)

    line = "-" * 60
    print(f"Device check: {args.ip}   ({ping.checked_at} UTC)")
    print(line)
    print(_format_line("ICMP ping", ping))
    if tcp is not None:
        print(_format_line(f"TCP {args.port}", tcp))
    print(line)
    print(f"Verdict   : {verdict}")
    print(f"Reason    : {reason}")

    return 1 if verdict == "INCONCLUSIVE" else 0


if __name__ == "__main__":
    raise SystemExit(main())