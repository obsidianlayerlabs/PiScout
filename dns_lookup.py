"""
dns_lookup.py

Resolves a hostname to an IP address. Used by the KEY3 button as a
quick DNS health check ("is DNS resolution working right now?").

This uses Python's built-in socket resolver rather than shelling out
to the external `nslookup` binary. `dnsutils` isn't part of PiScout's
installed package set (see install.sh's package list), so resolving
via socket answers the same question without adding a new dependency
or apt package. The result is the same: it succeeds or fails exactly
when `nslookup google.com` would.

What this file does:
  - Resolve a hostname to an IPv4 address with a timeout
  - Report how long resolution took

What this file does NOT do:
  - Touch the display
  - Know anything about GPIO or buttons
"""

import logging
import socket
import time

log = logging.getLogger(__name__)


def resolve_host(hostname: str, timeout: float = 2.0) -> dict:
    """
    Resolve `hostname` to an IPv4 address.

    Returns a dict:
        {
            "hostname": str,
            "success": bool,
            "ip": str or None,
            "elapsed_ms": float or None,
        }
    """
    original_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    start = time.monotonic()
    try:
        ip = socket.gethostbyname(hostname)
        elapsed_ms = (time.monotonic() - start) * 1000
        return {
            "hostname": hostname,
            "success": True,
            "ip": ip,
            "elapsed_ms": elapsed_ms,
        }
    except Exception:
        log.debug("DNS resolution failed for %s", hostname, exc_info=True)
        return {
            "hostname": hostname,
            "success": False,
            "ip": None,
            "elapsed_ms": None,
        }
    finally:
        socket.setdefaulttimeout(original_timeout)
