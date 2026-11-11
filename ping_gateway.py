"""
ping_gateway.py

Finds the default gateway for a given interface and pings it once.

Used by the KEY1 button (see buttons.py / main.py) to give a fast
"is my uplink alive" check without needing to SSH into the device.

What this file does:
  - Read the kernel routing table to find the default gateway for an
    interface
  - Send a single ping to that gateway and report success/latency

What this file does NOT do:
  - Touch the display
  - Know anything about GPIO or buttons
"""

import logging
import re
import socket
import struct
import subprocess

log = logging.getLogger(__name__)

_PING_TIME_RE = re.compile(r"time[=<]([\d.]+)\s*ms")


def get_default_gateway(interface: str):
    """
    Return the default gateway IP for `interface` as a string, or None
    if it cannot be determined (no route, interface down, etc.).
    """
    try:
        with open("/proc/net/route", encoding="ascii") as f:
            next(f)  # header line
            for line in f:
                fields = line.strip().split()
                if len(fields) < 3:
                    continue
                iface, dest, gateway_hex = fields[0], fields[1], fields[2]
                if iface != interface or dest != "00000000":
                    continue
                # /proc/net/route stores the gateway as a little-endian
                # hex-encoded 32-bit integer.
                gateway_int = int(gateway_hex, 16)
                return socket.inet_ntoa(struct.pack("<L", gateway_int))
    except Exception:
        log.debug("Could not read default gateway for %s", interface, exc_info=True)
    return None


def ping_host(host: str, interface: str = None, timeout: float = 1.5) -> dict:
    """
    Ping an arbitrary host once, optionally bound to a specific interface.

    Returns a dict:
        {
            "host": str,
            "success": bool,            # True if the ping got a reply
            "latency_ms": float or None,
        }
    """
    cmd = ["ping", "-c", "1", "-W", str(timeout)]
    if interface:
        cmd += ["-I", interface]
    cmd.append(host)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout + 1.0,
        )
    except Exception:
        log.debug("Ping subprocess failed for host %s", host, exc_info=True)
        return {"host": host, "success": False, "latency_ms": None}

    if proc.returncode != 0:
        return {"host": host, "success": False, "latency_ms": None}

    match = _PING_TIME_RE.search(proc.stdout)
    latency_ms = float(match.group(1)) if match else None
    return {"host": host, "success": True, "latency_ms": latency_ms}


def ping_gateway(interface: str, timeout: float = 1.5) -> dict:
    """
    Ping the default gateway for `interface` once.

    Returns a dict:
        {
            "gateway": str or None,     # gateway IP, or None if not found
            "success": bool,            # True if the ping got a reply
            "latency_ms": float or None,
        }
    """
    gateway = get_default_gateway(interface)
    if gateway is None:
        return {"gateway": None, "success": False, "latency_ms": None}

    result = ping_host(gateway, interface=interface, timeout=timeout)
    return {
        "gateway": result["host"],
        "success": result["success"],
        "latency_ms": result["latency_ms"],
    }


def ping_wan(interface: str, host: str = "1.1.1.1", timeout: float = 1.5) -> dict:
    """
    Ping an external host once, bound to `interface`, to check WAN/internet
    reachability beyond the local gateway.

    Returns a dict:
        {
            "host": str,
            "success": bool,
            "latency_ms": float or None,
        }
    """
    return ping_host(host, interface=interface, timeout=timeout)
