from __future__ import annotations

import argparse
import asyncio
import logging
import socket
import ssl
import time
from dataclasses import dataclass
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_HOST_LIMIT = 120
_MESSAGE_LIMIT = 180
_DEFAULT_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class NetworkDiagnosticResult:
    check: str
    host: str
    status: str
    elapsed_ms: int
    addresses: int | None = None
    peer: str = ""
    exception_class: str = ""
    exception_message: str = ""


def safe_host(value: str) -> str:
    parsed = urlparse(value if "://" in value else f"https://{value}")
    return (parsed.hostname or value.split("/", 1)[0]).lower()[:_HOST_LIMIT]


def safe_exception_message(exc: BaseException) -> str:
    return str(exc).replace("\n", " ")[:_MESSAGE_LIMIT]


def run_network_diagnostics(hosts: list[str], *, timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS) -> list[NetworkDiagnosticResult]:
    results: list[NetworkDiagnosticResult] = []
    seen: set[str] = set()
    for raw_host in hosts:
        host = safe_host(raw_host)
        if not host or host in seen:
            continue
        seen.add(host)
        results.extend(_diagnose_host(host, timeout_seconds=timeout_seconds))
    return results


async def log_network_diagnostics(hosts: list[str], *, timeout_ms: int, reason: str, request_id: str = "") -> list[NetworkDiagnosticResult]:
    timeout_seconds = max(timeout_ms, 1) / 1000
    results = await asyncio.to_thread(run_network_diagnostics, hosts, timeout_seconds=timeout_seconds)
    for result in results:
        log_network_diagnostic(result, reason=reason, request_id=request_id)
    return results


def log_network_diagnostic(result: NetworkDiagnosticResult, *, reason: str, request_id: str = "") -> None:
    level = logging.INFO if result.status == "ok" else logging.WARNING
    fields: dict[str, object] = {
        "event": "container_network_diagnostic",
        "reason": reason,
        "check": result.check,
        "host": result.host,
        "status": result.status,
        "elapsed_ms": result.elapsed_ms,
        "addresses": result.addresses,
        "peer": result.peer,
        "request_id": request_id or None,
        "exception_class": result.exception_class or None,
        "exception_message": result.exception_message or None,
    }
    logger.log(level, "container_network_diagnostic %s", " ".join(f"{key}={value!r}" for key, value in fields.items() if value is not None))


def _diagnose_host(host: str, *, timeout_seconds: float) -> list[NetworkDiagnosticResult]:
    results: list[NetworkDiagnosticResult] = []
    dns_result, addresses = _check_dns(host, timeout_seconds=timeout_seconds)
    results.append(dns_result)
    if not addresses:
        return results
    peer = addresses[0]
    results.append(_check_tcp(host, peer=peer, timeout_seconds=timeout_seconds))
    results.append(_check_tls(host, peer=peer, timeout_seconds=timeout_seconds))
    return results


def _check_dns(host: str, *, timeout_seconds: float) -> tuple[NetworkDiagnosticResult, list[str]]:
    started = time.monotonic()
    previous_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout_seconds)
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        addresses = sorted({info[4][0] for info in infos})
        return NetworkDiagnosticResult(check="dns", host=host, status="ok", elapsed_ms=_elapsed_ms(started), addresses=len(addresses)), addresses
    except Exception as exc:
        return _failure_result("dns", host, started, exc), []
    finally:
        socket.setdefaulttimeout(previous_timeout)


def _check_tcp(host: str, *, peer: str, timeout_seconds: float) -> NetworkDiagnosticResult:
    started = time.monotonic()
    try:
        with socket.create_connection((peer, 443), timeout=timeout_seconds):
            return NetworkDiagnosticResult(check="tcp_connect", host=host, status="ok", elapsed_ms=_elapsed_ms(started), peer=peer)
    except Exception as exc:
        return _failure_result("tcp_connect", host, started, exc, peer=peer)


def _check_tls(host: str, *, peer: str, timeout_seconds: float) -> NetworkDiagnosticResult:
    started = time.monotonic()
    context = ssl.create_default_context()
    try:
        with socket.create_connection((peer, 443), timeout=timeout_seconds) as raw_sock:
            raw_sock.settimeout(timeout_seconds)
            with context.wrap_socket(raw_sock, server_hostname=host):
                return NetworkDiagnosticResult(check="tls_handshake", host=host, status="ok", elapsed_ms=_elapsed_ms(started), peer=peer)
    except Exception as exc:
        return _failure_result("tls_handshake", host, started, exc, peer=peer)


def _failure_result(check: str, host: str, started: float, exc: BaseException, *, peer: str = "") -> NetworkDiagnosticResult:
    return NetworkDiagnosticResult(
        check=check,
        host=host,
        status="failure",
        elapsed_ms=_elapsed_ms(started),
        peer=peer,
        exception_class=type(exc).__name__,
        exception_message=safe_exception_message(exc),
    )


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run safe container-side network diagnostics for reports_ad hosts.")
    parser.add_argument("hosts", nargs="*", help="Hostnames or URLs. Only hostnames are logged; paths and query strings are discarded.")
    parser.add_argument("--timeout-ms", type=int, default=5_000, help="Timeout per DNS/TCP/TLS check.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    hosts = args.hosts or ["dzen.ru", "firstdata.io", "api.telegram.org"]
    for result in run_network_diagnostics(hosts, timeout_seconds=max(args.timeout_ms, 1) / 1000):
        log_network_diagnostic(result, reason="manual_smoke")


if __name__ == "__main__":
    main()
