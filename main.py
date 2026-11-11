"""
main.py

PiScout — entry point and main loop.

Monitors Ethernet carrier state, runs the discovery race on link-up,
and updates the e-paper display with switch port information.

Discovery is handled entirely by race.py, which runs SNMP and passive
LLDP/CDP capture in parallel and returns the fastest result.

What this file does:
  - Initialize the display
  - Monitor link state on the configured interface
  - Show appropriate screens (waiting, scanning, result, stale)
  - Call race.run() on link-up and feed results to the display
  - Wire up the onboard e-paper HAT buttons (currently: KEY1 pings the
    default gateway and shows the result)
  - Handle SIGTERM gracefully for clean shutdown

What this file does NOT do:
  - Implement any discovery logic (that belongs in discover_*.py and race.py)
  - Parse LLDP or CDP frames directly
  - Query SNMP directly
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import sys
import ipaddress
import threading
import time
from pathlib import Path
from typing import Optional

import config
import race
import trigger
import discover_passive
import history
import buttons
import ping_gateway
import dns_lookup
import jitter_test
import vlan_enrich

# Configure logging before importing display or discovery modules
# so their loggers inherit the correct level.
_log_level_str = (getattr(config, "LOG_LEVEL", "WARNING") or "WARNING").upper()
_log_level = getattr(logging, _log_level_str, logging.WARNING)
logging.basicConfig(
    level=_log_level,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)
log.info("Logging initialized at level %s", _log_level_str)


# ============================================================
# -------------------- DISPLAY FACTORY -----------------------
# ============================================================

def _get_display_type() -> str:
    return str(getattr(config, "DISPLAY_TYPE", "epaper")).lower().strip()


def _create_display():
    """
    Instantiate the correct display driver based on config.DISPLAY_TYPE.
    """
    display_type = _get_display_type()
    font_path = getattr(config, "DISPLAY_FONT_PATH", None)

    if display_type == "epaper":
        from display_epaper import EPaperDisplay

        return EPaperDisplay(
            font_path=font_path,
            min_refresh_interval=int(getattr(config, "EPAPER_MIN_REFRESH_INTERVAL", 10)),
            auto_sleep=bool(getattr(config, "EPAPER_AUTO_SLEEP", True)),
            startup_mode=True,
            partial_refresh_limit=int(getattr(config, "EPAPER_PARTIAL_REFRESH_LIMIT", 8)),
        )

    if display_type == "lcd":
        from display_lcd import LCDDisplay

        return LCDDisplay(
            font_path=font_path,
            rotate_180=bool(getattr(config, "LCD_ROTATE_180", True)),
            clear_on_start=bool(getattr(config, "LCD_CLEAR_ON_START", True)),
            background_color=getattr(config, "LCD_BACKGROUND_COLOR", (0, 0, 0)),
            text_color=getattr(config, "LCD_TEXT_COLOR", (255, 255, 255)),
            backlight_brightness=int(getattr(config, "LCD_BACKLIGHT_BRIGHTNESS", 100)),
        )

    log.warning(
        "Unknown DISPLAY_TYPE '%s' — defaulting to epaper.",
        display_type,
    )
    from display_epaper import EPaperDisplay

    return EPaperDisplay(font_path=font_path)


# ============================================================
# -------------------- DISPLAY HELPERS -----------------------
# ============================================================

def _truncate(text: str, max_len: int) -> str:
    text = str(text).strip()
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def _format_link_line(interface: str) -> str:
    """
    Build the LINK line from live sysfs state.

    Speed is read from /sys/class/net/{iface}/speed (Mbps integer).
    Duplex is read from /sys/class/net/{iface}/duplex ("full" or "half").
    Returns e.g. "LINK: 1G FD", "LINK: 100M HD", "LINK: --" if not negotiated.
    """
    speed = _read_link_speed(interface)
    if speed <= 0:
        return "LINK: --"
    speed_str = f"{speed // 1000}G" if speed >= 1000 else f"{speed}M"
    duplex_path = Path(f"/sys/class/net/{interface}/duplex")
    try:
        duplex_raw = duplex_path.read_text(encoding="ascii").strip().lower()
        duplex_str = "FD" if duplex_raw == "full" else "HD"
    except Exception:
        duplex_str = "?"
    return f"LINK: {speed_str} {duplex_str}"


def _read_dhcp_lease(interface: str) -> Optional[dict]:
    """
    Read the systemd-networkd DHCP lease file for `interface`.

    install.sh configures eth0 via systemd-networkd (NetworkManager and
    dhclient are both disabled/replaced), which stores each interface's
    active DHCP lease as a plain key=value text file at
    /run/systemd/netif/leases/<ifindex> — not the classic dhclient
    lease format. The ifindex (a small integer, not the interface name)
    is read from sysfs first since that's the only way to find the
    right lease file without assuming a fixed index.

    Returns a dict of the lease fields (ADDRESS, LIFETIME, ROUTER, DNS,
    etc.) or None if no lease file exists — e.g. the interface is
    statically configured rather than via DHCP, or DHCP hasn't
    completed yet.
    """
    ifindex_path = Path(f"/sys/class/net/{interface}/ifindex")
    try:
        ifindex = ifindex_path.read_text(encoding="ascii").strip()
    except Exception:
        return None

    lease_path = Path(f"/run/systemd/netif/leases/{ifindex}")
    try:
        lines = lease_path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return None

    lease = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        lease[key.strip()] = value.strip()
    return lease or None


def _get_local_ip(interface: str) -> str:
    """
    Return this Pi's own IPv4 address on `interface`, or "Unknown" if
    it doesn't have one yet.

    Reads the actual DHCP lease systemd-networkd holds for this
    interface first — this reflects exactly what the DHCP server
    handed out (and makes the lease's LIFETIME, ROUTER, DNS, etc.
    available too, in case any of that is ever wanted on screen).
    Falls back to a direct SIOCGIFADDR ioctl query — a plain kernel
    query for whatever address is currently bound — if no lease file
    exists, e.g. on a statically-configured interface.
    """
    lease = _read_dhcp_lease(interface)
    if lease and lease.get("ADDRESS"):
        return lease["ADDRESS"]

    import fcntl
    import struct

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            packed_iface = struct.pack("256s", interface.encode("utf-8")[:15])
            ip_bytes = fcntl.ioctl(s.fileno(), 0x8915, packed_iface)[20:24]  # SIOCGIFADDR
            return socket.inet_ntoa(ip_bytes)
    except OSError:
        return "Unknown"


# ============================================================
# ---------------------- VLAN RESOLUTION ---------------------
# ============================================================
# The access VLAN can come from several places, and many networks
# provide none of them. Rather than trusting one source, the display
# walks a chain and labels the result by confidence, so a tech can
# always tell a switch-reported VLAN from an inferred one:
#
#   1. Switch-reported, in the discovery result itself
#      (LLDP Port VLAN ID TLV, CDP Native VLAN, or an SNMP race win)  -> "120"
#   2. Switch-reported, backfilled by SNMP after a VLAN-less win
#      (see vlan_enrich.py -- verified against the LLDP switch name) -> "120"
#   3. Inferred from our DHCP subnet via VLAN_SUBNET_MAP              -> "120 (subnet)"
#   4. Nothing available                                              -> "not advertised"
#
# The enrichment VLAN is kept separate from the discovery result on
# purpose. The passive-refresh thread replaces the result with each new
# LLDP frame (~every 30s); those frames still carry no VLAN, so writing
# the backfilled value into the result would erase it on the next
# refresh and make the VLAN flicker in and out.
_vlan_enrichment = {"switch": "", "vlan": ""}
_vlan_lock = threading.Lock()
_subnet_map_cache: Optional[list] = None


def _reset_vlan_enrichment() -> None:
    with _vlan_lock:
        _vlan_enrichment["switch"] = ""
        _vlan_enrichment["vlan"] = ""


def _set_vlan_enrichment(switch_name: str, vlan: str) -> None:
    with _vlan_lock:
        _vlan_enrichment["switch"] = vlan_enrich.normalize_switch_name(switch_name)
        _vlan_enrichment["vlan"] = vlan


def _subnet_map() -> list:
    """
    Parse VLAN_SUBNET_MAP (dict) and VLAN_SUBNET_MAP_STR ("net=vlan,...")
    from config into [(IPv4Network, "vlan")], most specific first.
    Bad entries are logged and skipped -- a typo in one entry must not
    disable the rest, or crash the display.
    """
    global _subnet_map_cache
    if _subnet_map_cache is not None:
        return _subnet_map_cache

    raw: dict = {}
    raw.update(getattr(config, "VLAN_SUBNET_MAP", {}) or {})
    for item in str(getattr(config, "VLAN_SUBNET_MAP_STR", "") or "").split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            log.warning("VLAN subnet map: ignoring malformed entry %r (want net=vlan)", item)
            continue
        net, _, vlan = item.partition("=")
        raw[net.strip()] = vlan.strip()

    parsed = []
    for net, vlan in raw.items():
        try:
            network = ipaddress.IPv4Network(str(net), strict=False)
            vlan_id = int(str(vlan).strip())
            if not 1 <= vlan_id <= 4094:
                raise ValueError("VLAN out of range 1-4094")
        except (ValueError, TypeError) as exc:
            log.warning("VLAN subnet map: ignoring %r=%r (%s)", net, vlan, exc)
            continue
        parsed.append((network, str(vlan_id)))

    parsed.sort(key=lambda pair: pair[0].prefixlen, reverse=True)
    _subnet_map_cache = parsed
    return parsed


def _vlan_from_subnet(interface: str) -> str:
    """Look up our IP in the configured subnet map (longest prefix wins)."""
    entries = _subnet_map()
    if not entries:
        return ""
    try:
        ip = ipaddress.IPv4Address(_get_local_ip(interface))
    except (ValueError, ipaddress.AddressValueError):
        return ""
    for network, vlan in entries:
        if ip in network:
            return vlan
    return ""


def _resolve_vlan_text(neighbor: dict, interface: str) -> str:
    """Walk the VLAN source chain; see the section comment above."""
    vlan = str(neighbor.get("vlan") or "").strip()
    if vlan:
        return vlan

    with _vlan_lock:
        enr_switch = _vlan_enrichment["switch"]
        enr_vlan = _vlan_enrichment["vlan"]
    # Only use the backfill if it was verified against THIS switch.
    if enr_vlan and enr_switch and enr_switch == vlan_enrich.normalize_switch_name(
        neighbor.get("switch_name")
    ):
        return enr_vlan

    subnet_vlan = _vlan_from_subnet(interface)
    if subnet_vlan:
        return f"{subnet_vlan} (subnet)"

    return "not advertised"


def build_display_lines(neighbor: dict, interface: str) -> list[str]:
    """Build the 6 body lines for a valid neighbor result."""
    return [
        f"SW: {_truncate(neighbor.get('switch_name', 'Unknown'), 18)}",
        f"IP: {_truncate(_get_local_ip(interface), 18)}",
        f"PORT: {_truncate(neighbor.get('port', 'Unknown'), 16)}",
        f"VLAN: {_resolve_vlan_text(neighbor, interface)}",
        f"VOICE: {neighbor.get('voice_vlan', 'None')}",
        _format_link_line(interface),
    ]


def build_scanning_lines() -> list[str]:
    """Shown immediately after link-up while discovery is running."""
    return ["", "", "Scanning...", "", ""]


def build_waiting_for_link_lines() -> list[str]:
    """Shown when no Ethernet carrier is detected."""
    return ["", "", "Waiting for", "link...", ""]


def build_stale_lines() -> list[str]:
    """Shown when a previously seen neighbor has not re-advertised."""
    return ["", "No active", "neighbor data.", "", ""]


def build_ping_lines(result: dict, link_active: bool) -> list[str]:
    """
    Shown while/after pinging the default gateway (see the gateway-ping
    button handler below).

    result is the dict returned by ping_gateway.ping_gateway():
        {"gateway": str or None, "success": bool, "latency_ms": float or None}
    """
    gateway = result.get("gateway")
    link_str = "Active" if link_active else "Down"
    if gateway is None:
        return ["PING: Gateway", "IP: Not found", "Status: NO ROUTE", f"Link: {link_str}", ""]
    status = "REACHABLE" if result.get("success") else "UNREACHABLE"
    return ["PING: Gateway", f"IP: {gateway}", f"Status: {status}", f"Link: {link_str}", ""]


def build_pinging_lines() -> list[str]:
    """Shown immediately when the gateway-ping button handler starts, before the result is known."""
    return ["", "", "Pinging", "gateway...", ""]


def build_wan_ping_lines(result: dict, link_active: bool) -> list[str]:
    """
    Shown after a KEY2 press while/after pinging an external WAN host.

    result is the dict returned by ping_gateway.ping_wan():
        {"host": str, "success": bool, "latency_ms": float or None}
    """
    host = result.get("host", "?")
    link_str = "Active" if link_active else "Down"
    status = "REACHABLE" if result.get("success") else "UNREACHABLE"
    return ["PING: WAN", f"IP: {host}", f"Status: {status}", f"Link: {link_str}", ""]


def build_pinging_wan_lines() -> list[str]:
    """Shown immediately when the WAN-ping button handler starts, before the result is known."""
    return ["", "", "Pinging", "WAN...", ""]


def build_dns_lookup_lines(result: dict) -> list[str]:
    """
    Shown after a KEY3 press once DNS resolution and the follow-up
    internet-route check both finish.

    result is the dict returned by dns_lookup.resolve_host(), with an
    extra "route_ok" key added by the button handler:
        {"hostname": str, "success": bool, "ip": str or None,
         "elapsed_ms": float or None, "route_ok": bool}
    """
    hostname = result.get("hostname", "?")
    dns_ok = result.get("success", False)
    route_ok = result.get("route_ok", False)
    return [
        "DNS DIAGNOSTIC",
        f"Domain: {hostname}",
        f"Status: {'RESOLVED' if dns_ok else 'FAILED'}",
        f"Internet Route: {'OK' if route_ok else 'FAIL'}",
        f"Resolution: {'SUCCESS' if dns_ok else 'FAILED'}",
    ]


def build_looking_up_lines(hostname: str) -> list[str]:
    """Shown immediately when the DNS-lookup button handler starts, before the result is known."""
    return ["", "", "Looking up", f"{hostname}...", ""]


def build_jitter_lines(result: dict) -> list[str]:
    """
    Shown after a KEY4 press once the jitter test finishes.

    result is the dict returned by jitter_test.measure_jitter():
        {"host": str, "success": bool, "replies": int, "sent": int,
         "avg_rtt_ms": float or None, "jitter_ms": float or None,
         "min_rtt_ms": float or None, "max_rtt_ms": float or None,
         "loss_percent": float}
    """
    host = result.get("host", "?")
    loss = result.get("loss_percent")
    min_rtt = result.get("min_rtt_ms")
    avg_rtt = result.get("avg_rtt_ms")
    max_rtt = result.get("max_rtt_ms")

    if not result.get("success") or min_rtt is None:
        replies = result.get("replies", 0)
        sent = result.get("sent", 0)
        loss_str = f"{loss:.0f}%" if loss is not None else "?"
        return [
            f"LINK QUALITY [{host}]",
            f"Loss: {loss_str}",
            f"Only {replies}/{sent} replies",
            "",
            "",
        ]

    return [
        f"LINK QUALITY [{host}]",
        f"Loss: {loss:.0f}%",
        f"Min Latency: {min_rtt:.1f}ms",
        f"Avg Latency: {avg_rtt:.1f}ms",
        f"Max Latency: {max_rtt:.1f}ms",
    ]


def build_measuring_jitter_lines(host: str) -> list[str]:
    """Shown immediately when the jitter-test button handler starts, before it finishes."""
    return ["", "", "Measuring", f"jitter {host}...", ""]


# ============================================================
# ---------------- SCREEN STATE / WATCHDOG --------------------
# ============================================================
# _home_screen holds the last "real" screen drawn -- a discovery
# result, waiting-for-link, scanning, or stale screen. Transient
# screens (button results, "Pinging...", etc.) deliberately do NOT
# update it, so there is always a known-good screen to fall back to.
#
# _transient_since is the monotonic timestamp when a transient screen
# was drawn, or None when a real screen is showing. The watchdog
# thread uses it to force a restore if a transient screen somehow
# outlives its handler -- e.g. the handler thread died mid-way, or two
# button presses interleaved such that nothing restored the screen.
_home_screen = {"lines": None, "protocol": ""}
_transient_since = [None]
_screen_lock = threading.Lock()


def _show(display, lines: list[str], force: bool = False, protocol: str = "",
          transient: bool = False) -> bool:
    """
    Show lines on whichever display is connected.

    Wraps the display's show_lines method and passes the optional
    protocol string for the top-corner indicator. Falls back gracefully
    if the display driver does not support the protocol parameter.

    transient:
        True for short-lived screens that should automatically be
        replaced by the home screen shortly afterwards (button results,
        "Pinging..." progress screens). These do not become the home
        screen, and they arm the watchdog. False (the default) marks
        this as a real screen: it becomes the new home screen and
        disarms the watchdog.
    """
    with _screen_lock:
        if transient:
            _transient_since[0] = time.monotonic()
        else:
            _home_screen["lines"] = list(lines)
            _home_screen["protocol"] = protocol
            _transient_since[0] = None

    try:
        return display.show_lines(lines, force=force, protocol=protocol)
    except TypeError:
        return display.show_lines(lines, force=force)
    except Exception as exc:
        log.error("Display update failed: %s", exc)
        return False


def _restore_home_screen(display) -> None:
    """
    Redraw the last real (non-transient) screen.

    Used both by button handlers when their result-display delay ends,
    and by the watchdog thread if a transient screen outlives it. Reads
    the home screen from shared state rather than from a snapshot taken
    when the handler started -- taking a snapshot meant that pressing a
    second button while a first button's result was still showing would
    capture THAT transient result as the thing to restore to, stranding
    the display on it permanently.
    """
    with _screen_lock:
        lines = _home_screen["lines"]
        protocol = _home_screen["protocol"]
    if lines is None:
        lines = build_waiting_for_link_lines()
        protocol = ""
    _show(display, lines, force=True, protocol=protocol)


def _screen_watchdog(display, shutdown_event: threading.Event) -> None:
    """
    Background thread: force the home screen back if a transient screen
    has been showing longer than SCREEN_WATCHDOG_SECONDS.

    This is a safety net, not the primary restore path -- button
    handlers restore the home screen themselves as soon as their
    display delay elapses. The watchdog only fires when that normal
    path did not happen for some reason, so under healthy operation it
    never triggers at all.
    """
    timeout = float(getattr(config, "SCREEN_WATCHDOG_SECONDS", 30.0))
    if timeout <= 0:
        log.debug("Screen watchdog disabled (SCREEN_WATCHDOG_SECONDS <= 0)")
        return

    log.debug("Screen watchdog started (%.0fs timeout)", timeout)
    while not shutdown_event.is_set():
        shutdown_event.wait(1.0)
        if shutdown_event.is_set():
            break
        with _screen_lock:
            since = _transient_since[0]
        if since is None:
            continue
        elapsed = time.monotonic() - since
        if elapsed >= timeout:
            log.warning(
                "Screen watchdog: transient screen up for %.0fs (limit %.0fs) "
                "-- forcing home screen back",
                elapsed,
                timeout,
            )
            _restore_home_screen(display)


# ============================================================
# -------------------- BUTTON HANDLERS ------------------------
# ============================================================

# Short labels for the on-screen sidebar identifying what each button
# does, listed top-to-bottom in the order they appear on screen (not
# in KEY1-KEY4 order — see the callbacks dict in run() for why).
BUTTON_LABELS = ["GW", "WAN", "DNS", "JIT"]


def _ping_gateway_and_show(display, interface: str) -> None:
    """
    Gateway-ping button handler (wired to the physical top button —
    see the callbacks dict in run()): ping the default gateway for
    `interface`, show the
    result on screen for a few seconds, then restore whatever was on
    screen before the button was pressed.

    Runs on its own thread (see buttons.Buttons), so this can safely
    block on the ping and on the result-display delay without stalling
    the main loop or other buttons.
    """
    _show(display, build_pinging_lines(), force=True, transient=True)

    result = ping_gateway.ping_gateway(interface)
    log.info(
        "Gateway ping | gateway=%s success=%s latency_ms=%s",
        result.get("gateway"),
        result.get("success"),
        result.get("latency_ms"),
    )
    _show(display, build_ping_lines(result, _read_carrier(interface)), force=True, transient=True)

    display_seconds = float(getattr(config, "BUTTON_PING_RESULT_DISPLAY_SECONDS", 4.0))
    time.sleep(display_seconds)

    _restore_home_screen(display)


def _ping_wan_and_show(display, interface: str) -> None:
    """
    WAN-ping button handler (wired to the second-from-top physical
    button — see the callbacks dict in run()): ping an external host
    (config.WAN_PING_HOST) bound to
    `interface` to check WAN/internet reachability beyond the local
    gateway, show the result on screen, then restore whatever was on
    screen before the button was pressed.

    Runs on its own thread (see buttons.Buttons), so this can safely
    block on the ping and on the result-display delay without stalling
    the main loop or other buttons.
    """
    _show(display, build_pinging_wan_lines(), force=True, transient=True)

    host = str(getattr(config, "WAN_PING_HOST", "1.1.1.1"))
    result = ping_gateway.ping_wan(interface, host=host)
    log.info(
        "WAN ping | host=%s success=%s latency_ms=%s",
        result.get("host"),
        result.get("success"),
        result.get("latency_ms"),
    )
    _show(display, build_wan_ping_lines(result, _read_carrier(interface)), force=True, transient=True)

    display_seconds = float(getattr(config, "BUTTON_PING_RESULT_DISPLAY_SECONDS", 4.0))
    time.sleep(display_seconds)

    _restore_home_screen(display)


def _dns_lookup_and_show(display, interface: str) -> None:
    """
    DNS-lookup button handler (wired to the second-from-bottom physical
    button — see the callbacks dict in run()): resolve
    config.DNS_LOOKUP_HOST (default google.com) via the system resolver,
    then separately confirm actual internet connectivity by pinging the
    resolved IP (or a known-good fallback host if DNS itself failed) —
    this distinguishes "DNS is broken" from "everything is broken" on
    screen, rather than conflating the two. Shows the result, then
    restores whatever was on screen before the button was pressed.

    Runs on its own thread (see buttons.Buttons), so this can safely
    block on the lookup, the route check, and the result-display delay
    without stalling the main loop or other buttons.
    """
    hostname = str(getattr(config, "DNS_LOOKUP_HOST", "google.com"))
    _show(display, build_looking_up_lines(hostname), force=True, transient=True)

    result = dns_lookup.resolve_host(hostname)
    log.info(
        "DNS lookup | hostname=%s success=%s ip=%s elapsed_ms=%s",
        result.get("hostname"),
        result.get("success"),
        result.get("ip"),
        result.get("elapsed_ms"),
    )

    # Internet-route check: ping whatever the lookup resolved to. If DNS
    # itself failed, fall back to a known-good IP so a route failure and
    # a DNS-only failure show up as distinct statuses rather than both
    # just reading "FAIL".
    route_target = result.get("ip") or str(getattr(config, "WAN_PING_HOST", "1.1.1.1"))
    route_result = ping_gateway.ping_host(route_target, interface=interface, timeout=1.5)
    result["route_ok"] = bool(route_result.get("success"))
    log.info("DNS route check | target=%s route_ok=%s", route_target, result["route_ok"])

    _show(display, build_dns_lookup_lines(result), force=True, transient=True)

    display_seconds = float(getattr(config, "BUTTON_PING_RESULT_DISPLAY_SECONDS", 4.0))
    time.sleep(display_seconds)

    _restore_home_screen(display)


def _jitter_test_and_show(display, interface: str) -> None:
    """
    Jitter-test button handler (wired to the physical bottom button —
    see the callbacks dict in run()): send several pings to
    config.JITTER_TEST_HOST (falls
    back to config.WAN_PING_HOST if unset) and show average RTT and
    jitter, then restore whatever was on screen before the button was
    pressed.

    This takes longer than the other three button actions — sending
    config.JITTER_TEST_COUNT (default 10) pings takes roughly 10
    seconds at the default 1/sec interval — but still runs on its own
    thread (see buttons.Buttons), so it never stalls the main loop or
    the other buttons.
    """
    host = str(
        getattr(config, "JITTER_TEST_HOST", None)
        or getattr(config, "WAN_PING_HOST", "1.1.1.1")
    )
    count = int(getattr(config, "JITTER_TEST_COUNT", 10))

    _show(display, build_measuring_jitter_lines(host), force=True, transient=True)

    result = jitter_test.measure_jitter(host, interface=interface, count=count)
    log.info(
        "Jitter test | host=%s success=%s replies=%s/%s avg_rtt_ms=%s jitter_ms=%s",
        result.get("host"),
        result.get("success"),
        result.get("replies"),
        result.get("sent"),
        result.get("avg_rtt_ms"),
        result.get("jitter_ms"),
    )
    _show(display, build_jitter_lines(result), force=True, transient=True)

    display_seconds = float(getattr(config, "BUTTON_PING_RESULT_DISPLAY_SECONDS", 4.0))
    time.sleep(display_seconds)

    _restore_home_screen(display)


# ============================================================
# -------------------- CARRIER DETECTION ---------------------
# ============================================================

def _read_carrier(interface: str) -> bool:
    """
    Read the Ethernet carrier state from the kernel sysfs.

    Returns True if link is up, False if down or the file cannot be read.
    """
    carrier_path = Path(f"/sys/class/net/{interface}/carrier")
    try:
        return carrier_path.read_text(encoding="ascii").strip() == "1"
    except Exception:
        return False


def _interface_exists(interface: str) -> bool:
    return Path(f"/sys/class/net/{interface}").exists()


def _read_link_speed(interface: str) -> int:
    """
    Read the negotiated link speed from sysfs.

    Returns the speed in Mbps (100 or 1000 on typical ports).
    Returns -1 if the link has not yet negotiated or the file cannot be read.
    Auto-negotiation on 1GbE takes 1-3 seconds after physical link-up.
    """
    speed_path = Path(f"/sys/class/net/{interface}/speed")
    try:
        val = int(speed_path.read_text(encoding="ascii").strip())
        return val if val > 0 else -1
    except Exception:
        return -1


def _wait_for_negotiation(
    interface: str,
    shutdown_event: threading.Event,
    timeout: float = 5.0,
) -> bool:
    """
    Wait for Ethernet auto-negotiation to complete.

    During the 1-3 seconds after physical link-up, the NIC and switch
    are negotiating speed and duplex. No frames can be sent or received
    during this window. This function blocks until negotiation completes
    or the timeout expires.

    Returns True if negotiation completed, False if it timed out.
    """
    deadline = time.monotonic() + timeout
    while not shutdown_event.is_set() and time.monotonic() < deadline:
        if _read_link_speed(interface) > 0:
            return True
        time.sleep(0.05)
    return _read_link_speed(interface) > 0


# ============================================================
# -------------------- MAIN LOOP -----------------------------
# ============================================================

def run() -> None:
    """
    Main loop. Never returns unless a fatal error occurs or SIGTERM fires.
    """
    # --- Shutdown event (set by SIGTERM/SIGINT handler) ---
    shutdown_event = threading.Event()

    def _sigterm_handler(signum, frame):
        log.info("Signal %d received — initiating graceful shutdown", signum)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, _sigterm_handler)
    signal.signal(signal.SIGINT, _sigterm_handler)

    # --- Configuration ---
    interface = str(getattr(config, "NETWORK_INTERFACE", "eth0"))
    disc_timeout = float(getattr(config, "DISCOVERY_TIMEOUT", 120.0))
    reveal_delay = float(getattr(config, "RESULT_REVEAL_DELAY", 1.5))
    partial_display_delay = float(getattr(config, "PARTIAL_DISPLAY_DELAY", 30.0))

    log.info("Starting PiScout")
    log.info("DISPLAY_TYPE=%s", _get_display_type())
    log.info("NETWORK_INTERFACE=%s", interface)
    log.info("DISCOVERY_TIMEOUT=%s", disc_timeout)
    log.info("RESULT_REVEAL_DELAY=%s", reveal_delay)
    log.info("PARTIAL_DISPLAY_DELAY=%s", partial_display_delay)

    # --- Verify interface exists ---
    if not _interface_exists(interface):
        log.error(
            "Network interface '%s' not found. Check NETWORK_INTERFACE in config.py.",
            interface,
        )
        sys.exit(1)
    log.info("Network interface '%s' found.", interface)

    # --- Get local MAC for self-frame filtering ---
    local_mac = trigger.get_interface_mac(interface)
    if local_mac is None:
        log.warning("Could not read MAC address for %s — self-frame filtering disabled", interface)

    # --- Create display ---
    log.info("Creating %s display object", _get_display_type())
    display = _create_display()
    display.initialize()

    # --- Buttons (KEY1-KEY4) ---
    # Wrapped in try/except so a missing/misbehaving GPIO stack degrades
    # to "no button support" instead of crashing the whole appliance.
    # Set up before the first screen draw so the button-label footer
    # (if the display supports it) appears from the very first screen.
    buttons_obj: Optional[buttons.Buttons] = None
    if bool(getattr(config, "BUTTONS_ENABLED", True)):
        try:
            # On this HAT, the buttons are physically arranged with KEY4
            # at the top of the board, descending to KEY3, KEY2, KEY1 at
            # the bottom — the reverse of the KEY1-KEY4 numbering. The
            # on-screen sidebar lists GW/WAN/DNS/JIT top-to-bottom (see
            # BUTTON_LABELS above), so the callbacks below are assigned
            # in the same reversed order: whichever physical button sits
            # at the top of the board (KEY4) triggers the action shown
            # at the top of the sidebar (GW), and so on down to the
            # bottom button (KEY1) triggering the bottom label (JIT).
            buttons_obj = buttons.Buttons(
                callbacks={
                    "KEY4": lambda: _ping_gateway_and_show(display, interface),
                    "KEY3": lambda: _ping_wan_and_show(display, interface),
                    "KEY2": lambda: _dns_lookup_and_show(display, interface),
                    "KEY1": lambda: _jitter_test_and_show(display, interface),
                }
            )
            buttons_obj.start()
            if hasattr(display, "set_button_labels"):
                display.set_button_labels(BUTTON_LABELS)
        except Exception:
            log.exception("Failed to initialize buttons — continuing without button support")
            buttons_obj = None

    # --- Screen watchdog ---
    # Safety net that forces the home screen back if a transient screen
    # (a button result, a "Pinging..." progress screen) is still up
    # after SCREEN_WATCHDOG_SECONDS. Daemon thread so it never blocks
    # shutdown. Started even when buttons failed to initialize, since
    # transient screens are cheap to guard either way.
    threading.Thread(
        target=_screen_watchdog,
        args=(display, shutdown_event),
        name="screen-watchdog",
        daemon=True,
    ).start()

    # --- Initial screen ---
    _show(display, build_waiting_for_link_lines(), force=True)

    # --- Main event loop ---
    while not shutdown_event.is_set():
        carrier = _read_carrier(interface)

        if not carrier:
            _show(display, build_waiting_for_link_lines())
            _wait_or_shutdown(shutdown_event, 0.5)
            continue

        # ---- Link is up — start a discovery session ----
        log.info("Link up on %s — starting discovery", interface)

        # Wait for auto-negotiation to complete before transmitting.
        # During the 1-3 second negotiation window no frames can be sent
        # or received. Polling here prevents wasted trigger frames being
        # dropped by the NIC before the link is ready.
        negotiated = _wait_for_negotiation(interface, shutdown_event, timeout=5.0)
        if not negotiated:
            log.debug("Link speed did not negotiate on %s — proceeding anyway", interface)

        if shutdown_event.is_set():
            break

        if not _read_carrier(interface):
            # Link dropped during negotiation.
            _show(display, build_waiting_for_link_lines(), force=True)
            continue

        # Send the LLDP trigger immediately after negotiation, before the
        # display starts drawing "Scanning...". The e-paper takes ~400ms
        # for a partial refresh — every millisecond of head start matters
        # on LLDP switches that respond almost immediately.
        trigger.send_lldp_trigger(interface, local_mac)
        log.debug("Early LLDP trigger sent on %s before display draw", interface)

        # Show "Scanning..." and note when it finishes drawing.
        # The reveal delay timer starts AFTER the display finishes drawing
        # so the user always sees the screen for at least reveal_delay seconds.
        _show(display, build_scanning_lines(), force=True)
        scan_drawn_at = time.monotonic()
        log.debug(
            "Scanning screen shown. Reveal delay: %.2fs after screen draw.",
            reveal_delay,
        )

        # New discovery session: drop any VLAN backfilled for the previous
        # link (the cable may now be in a different port or switch).
        _reset_vlan_enrichment()

        # cancel_event is set when link goes down or shutdown fires.
        cancel_event = threading.Event()

        # Run the discovery race in a background thread so we can
        # simultaneously monitor carrier state.
        result_holder: list[Optional[dict]] = [None]

        def _race_thread():
            result_holder[0] = race.run(
                interface=interface,
                local_mac=local_mac,
                cancel_event=cancel_event,
                timeout=disc_timeout,
            )

        race_thread = threading.Thread(
            target=_race_thread,
            name="rf-race",
            daemon=True,
        )
        race_thread.start()

        # Monitor carrier while the race runs.
        while not shutdown_event.is_set():
            if not _read_carrier(interface):
                log.info("Link lost on %s — cancelling discovery", interface)
                cancel_event.set()
                break
            if not race_thread.is_alive():
                break  # Race finished
            _wait_or_shutdown(shutdown_event, 0.5)

        # Wait for the race thread to finish (it will exit quickly once
        # cancel_event is set or when it completes naturally).
        race_thread.join(timeout=5.0)
        cancel_event.set()  # Ensure all threads stop

        if shutdown_event.is_set():
            break

        result = result_holder[0]

        if not _read_carrier(interface):
            # Link went down while we were waiting — go back to top of loop.
            _show(display, build_waiting_for_link_lines(), force=True)
            continue

        if result is None:
            # Discovery timed out with link still up.
            log.info("Discovery timed out on %s", interface)
            _show(display, build_stale_lines(), force=True)
            # Stay on stale screen until link drops.
            while _read_carrier(interface) and not shutdown_event.is_set():
                _wait_or_shutdown(shutdown_event, 1.0)
            continue

        # ---- We have a result (may be partial) ----
        is_partial = result.get("is_partial", False)
        result_at = time.monotonic()
        displayed = False

        if is_partial:
            log.info(
                "Partial result from race | protocol=%s switch=%s port=%s vlan=%s",
                result.get("protocol"),
                result.get("switch_name"),
                result.get("port"),
                result.get("vlan"),
            )
        else:
            # Complete result — enforce normal reveal delay then show immediately.
            elapsed = time.monotonic() - scan_drawn_at
            if elapsed < reveal_delay:
                _wait_or_shutdown(shutdown_event, reveal_delay - elapsed)
            if not shutdown_event.is_set():
                protocol = result.get("protocol", "")
                display.set_startup_mode(False)
                _show(display, build_display_lines(result, interface), force=True, protocol=protocol)
                displayed = True
                history.record(result)
                log.info(
                    "Display updated | protocol=%s switch=%s ip=%s port=%s vlan=%s voice=%s",
                    protocol,
                    result.get("switch_name"),
                    result.get("switch_ip"),
                    result.get("port"),
                    result.get("vlan"),
                    result.get("voice_vlan"),
                )

        if shutdown_event.is_set():
            break

        # ---- Monitor link while showing result ----
        # Background passive listener serves three purposes:
        # 1. Resets the stale timer on every switch re-advertisement
        # 2. Upgrades display from partial to complete when more data arrives
        # 3. Updates display if switch data changes (e.g. VLAN reassignment)
        refresh_cancel = threading.Event()
        last_success_ts = [time.monotonic()]
        current_result = [result]
        stale_shown = False

        def _passive_refresh():
            while not shutdown_event.is_set() and not refresh_cancel.is_set():
                fresh = discover_passive.discover(
                    interface=interface,
                    local_mac=local_mac,
                    cancel_event=refresh_cancel,
                    timeout=disc_timeout,
                )
                if not fresh or refresh_cancel.is_set():
                    continue
                last_success_ts[0] = time.monotonic()
                log.debug(
                    "Passive refresh | protocol=%s switch=%s port=%s partial=%s",
                    fresh.get("protocol"),
                    fresh.get("switch_name"),
                    fresh.get("port"),
                    fresh.get("is_partial"),
                )
                prev = current_result[0]
                is_upgrade = prev.get("is_partial") and not fresh.get("is_partial")
                data_changed = fresh != prev
                if is_upgrade or data_changed:
                    current_result[0] = fresh
                    _show(
                        display,
                        build_display_lines(fresh, interface),
                        force=True,
                        protocol=fresh.get("protocol", ""),
                    )
                    if is_upgrade:
                        history.record(fresh)
                    log.info(
                        "Display %s | protocol=%s switch=%s port=%s vlan=%s voice=%s",
                        "upgraded from partial" if is_upgrade else "refreshed",
                        fresh.get("protocol"),
                        fresh.get("switch_name"),
                        fresh.get("port"),
                        fresh.get("vlan"),
                        fresh.get("voice_vlan"),
                    )

        refresh_thread = threading.Thread(
            target=_passive_refresh,
            name="rf-passive-refresh",
            daemon=True,
        )
        refresh_thread.start()

        # ---- VLAN enrichment ----
        # If the winning result had no VLAN, try to backfill it via SNMP
        # from the switch we're actually plugged into. Runs in the
        # background so the screen never waits on it; cancelled with the
        # session if the link drops (refresh_cancel).
        def _vlan_enrich_thread():
            first = current_result[0]
            if str(first.get("vlan") or "").strip():
                return  # switch already reported it
            vlan = vlan_enrich.find_vlan(
                switch_name=first.get("switch_name", ""),
                switch_ip=first.get("switch_ip", ""),
                local_mac=local_mac,
                cancel_event=refresh_cancel,
                timeout=float(getattr(config, "VLAN_SNMP_ENRICH_TIMEOUT", 60.0)),
            )
            if not vlan or refresh_cancel.is_set() or shutdown_event.is_set():
                return
            _set_vlan_enrichment(first.get("switch_name", ""), vlan)

            # Not on screen yet (partial result still waiting out its
            # delay)? Nothing to redraw -- the VLAN will be picked up
            # automatically when it is first drawn.
            if not displayed:
                return
            latest = current_result[0]
            lines = build_display_lines(latest, interface)
            protocol = latest.get("protocol", "")
            with _screen_lock:
                transient_up = _transient_since[0] is not None
                if transient_up:
                    # A button result is showing -- don't stomp it. Update
                    # the home screen instead; the button handler's normal
                    # restore will draw it with the VLAN filled in.
                    _home_screen["lines"] = list(lines)
                    _home_screen["protocol"] = protocol
            if not transient_up:
                _show(display, lines, force=True, protocol=protocol)

        if bool(getattr(config, "VLAN_SNMP_ENRICH", True)):
            threading.Thread(
                target=_vlan_enrich_thread,
                name="rf-vlan-enrich",
                daemon=True,
            ).start()

        while not shutdown_event.is_set():
            if not _read_carrier(interface):
                log.info("Link lost on %s", interface)
                refresh_cancel.set()
                refresh_thread.join(timeout=3.0)
                display.set_startup_mode(True)
                _show(display, build_waiting_for_link_lines(), force=True)
                break

            # If result was partial and not yet displayed, check the timer.
            # Show partial data after PARTIAL_DISPLAY_DELAY even if we still
            # haven't received a complete result — something is better than nothing.
            if not displayed:
                partial_elapsed = time.monotonic() - result_at
                if partial_elapsed >= partial_display_delay:
                    best = current_result[0]
                    protocol = best.get("protocol", "")
                    display.set_startup_mode(False)
                    _show(display, build_display_lines(best, interface), force=True, protocol=protocol)
                    displayed = True
                    history.record(best)
                    log.info(
                        "Partial result displayed after %.0fs delay | "
                        "switch=%s port=%s vlan=%s",
                        partial_elapsed,
                        best.get("switch_name"),
                        best.get("port"),
                        best.get("vlan"),
                    )
                elif current_result[0] != result and not current_result[0].get("is_partial"):
                    # Background refresh already got a complete result — show it now.
                    best = current_result[0]
                    protocol = best.get("protocol", "")
                    display.set_startup_mode(False)
                    _show(display, build_display_lines(best, interface), force=True, protocol=protocol)
                    displayed = True
                    history.record(best)
                    log.info(
                        "Complete result arrived during partial wait | "
                        "switch=%s port=%s vlan=%s",
                        best.get("switch_name"),
                        best.get("port"),
                        best.get("vlan"),
                    )

            # Show stale warning if no refreshed data within timeout window.
            stale_elapsed = time.monotonic() - last_success_ts[0]
            if not stale_shown and stale_elapsed > disc_timeout:
                log.info(
                    "Neighbor data stale on %s (%.0fs since last success)",
                    interface,
                    stale_elapsed,
                )
                _show(display, build_stale_lines(), force=True)
                stale_shown = True

            _wait_or_shutdown(shutdown_event, 1.0)

        refresh_cancel.set()

    # ---- Graceful shutdown ----
    log.info("Shutting down PiScout")
    if buttons_obj is not None:
        try:
            buttons_obj.cleanup()
        except Exception:
            log.debug("Button cleanup error", exc_info=True)
    try:
        display.shutdown()
    except Exception as exc:
        log.debug("Display shutdown error: %s", exc)


def _wait_or_shutdown(shutdown_event: threading.Event, seconds: float) -> None:
    """Sleep for up to `seconds` but wake immediately if shutdown fires."""
    shutdown_event.wait(timeout=seconds)


# ============================================================
# -------------------- ENTRY POINT ---------------------------
# ============================================================

if __name__ == "__main__":
    try:
        run()
    except Exception:
        log.exception("Fatal error in main loop")
        sys.exit(1)
