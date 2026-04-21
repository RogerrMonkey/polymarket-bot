"""Network-level helpers shared by the scheduler and dashboard.

Keeps DNS / connectivity probes in one place so the "is WARP active?"
check returns consistent results everywhere.
"""
from __future__ import annotations

import socket


_WARP_PROBE_HOST = "gamma-api.polymarket.com"


def check_warp_active(host: str = _WARP_PROBE_HOST, timeout_seconds: float = 3.0) -> bool:
    """Return True if `host` resolves via DNS within `timeout_seconds`.

    We use Polymarket's Gamma API as the canonical probe — if WARP is on,
    DNS resolves; if WARP is off (and the operator is in India), DNS fails.
    Never raises — always returns bool.
    """
    previous = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(max(0.1, float(timeout_seconds)))
        socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        socket.setdefaulttimeout(previous)
