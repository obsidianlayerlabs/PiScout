"""
vlan_enrich.py

Fills in the access VLAN when the winning discovery result did not
carry one.

Why this exists:
  The data VLAN normally arrives in one of two optional advertisements:
  the IEEE 802.1 Port VLAN ID TLV in LLDP, or the Native VLAN TLV in
  CDP. Many switches ship with the LLDP TLV disabled (Extreme EXOS
  defaults it to no-advertise, for example), and CDP is Cisco-only. In
  those environments LLDP still delivers the switch name and port
  reliably, but the VLAN arrives blank.

  race.py stops the SNMP thread the moment passive LLDP/CDP wins, and
  a VLAN-less LLDP result counts as a win. So on a network where SNMP
  *could* supply the VLAN, it never gets the chance. This module runs a
  focused SNMP lookup after the fact, without touching race.py.

How it finds the VLAN (vendor-neutral, standard MIBs only):
  1. GET sysName                      -- confirms access + switch identity
  2. GET dot1dTpFdbPort.<our MAC>     -- the bridge port our MAC was
                                         learned on (BRIDGE-MIB)
  3. GET dot1qPvid.<bridge port>      -- that port's untagged VLAN
                                         (Q-BRIDGE-MIB)

  Step 2 makes the result our port *by construction*: it is looked up
  from our own MAC address, not inferred from port names or numbering.
  Step 3 indexes dot1qPvid by bridge port, which is what the Q-BRIDGE-
  MIB actually specifies (dot1qPortVlanTable augments dot1dBasePort-
  Entry). Indexing it by ifIndex or LLDP port number instead -- which
  only coincidentally line up on some switches -- returns a different
  port's VLAN with no error.

Safety rule:
  A VLAN is only accepted if the queried device's sysName matches the
  switch name LLDP/CDP already reported. An upstream switch or router
  also learns our MAC (on its uplink), and its answer would be the
  uplink's native VLAN -- plausible-looking and wrong. Requiring the
  name match means we only ever trust the switch we are plugged into.
  If identity can't be confirmed, we return nothing rather than guess.

Limits:
  - Cisco IOS keeps per-VLAN bridge tables reachable only via
    community@vlan indexing, so step 2 typically finds nothing there.
    Cisco is usually covered by CDP's Native VLAN TLV instead.
  - Needs SNMP enabled and a community string from config. It tries the
    same community list race.py already tries, so it adds no new kind
    of network activity -- but on hardened networks failed community
    guesses can raise SNMP authentication-failure alerts. Disable with
    VLAN_SNMP_ENRICH = False if that matters in your environment.

What this file does NOT do:
  - Touch the display
  - Run on its own thread (main.py runs it in the background)
"""

from __future__ import annotations

import ipaddress
import logging
import threading
import time
from typing import Optional

import discover_snmp
import parse_utils

log = logging.getLogger(__name__)

_OID_SYS_NAME          = "1.3.6.1.2.1.1.5.0"
_OID_DOT1D_TP_FDB_PORT = "1.3.6.1.2.1.17.4.3.1.2"   # .<mac as 6 decimal octets>
_OID_DOT1Q_PVID        = "1.3.6.1.2.1.17.7.1.4.5.1.1"  # .<bridge port>

# Outcomes of a single host/community attempt.
_AUTH_FAIL    = "auth_fail"     # no sysName: wrong community or no SNMP -> try next community
_WRONG_SWITCH = "wrong_switch"  # reachable, but not the switch we're on -> skip this host
_NO_DATA      = "no_data"       # right switch, but no usable VLAN       -> skip this host
_OK           = "ok"


def normalize_switch_name(name) -> str:
    """Normalize a switch name for identity comparison across sources."""
    return parse_utils.strip_domain(str(name or "")).strip().lower()


def _valid_vlan(value) -> str:
    """Return a VLAN ID string if value is a usable 1-4094 VLAN, else ''."""
    vlan = parse_utils.normalize_vlan_value(str(value or ""))
    try:
        if 1 <= int(vlan) <= 4094:
            return str(int(vlan))
    except (TypeError, ValueError):
        pass
    return ""


def _query_host(
    host: str,
    community: str,
    local_mac: bytes,
    expected_switch: str,
) -> tuple[str, str]:
    """
    Try one host with one community string.

    Returns (outcome, vlan). vlan is only non-empty when outcome == _OK.
    """
    sys_name = discover_snmp._snmp_get(host, community, _OID_SYS_NAME)
    if not sys_name:
        return _AUTH_FAIL, ""

    actual = normalize_switch_name(sys_name)
    if actual != expected_switch:
        log.debug(
            "VLAN enrich: %s is '%s', not the switch we're on ('%s') -- skipping",
            host, actual, expected_switch,
        )
        return _WRONG_SWITCH, ""

    mac_oid = ".".join(str(b) for b in local_mac)
    bridge_port_raw = discover_snmp._snmp_get(
        host, community, f"{_OID_DOT1D_TP_FDB_PORT}.{mac_oid}"
    )
    try:
        bridge_port = int(str(bridge_port_raw).strip())
    except (TypeError, ValueError):
        log.debug("VLAN enrich: %s has no forwarding entry for our MAC", host)
        return _NO_DATA, ""

    pvid_raw = discover_snmp._snmp_get(
        host, community, f"{_OID_DOT1Q_PVID}.{bridge_port}"
    )
    vlan = _valid_vlan(pvid_raw)
    if not vlan:
        log.debug("VLAN enrich: %s bridge port %d has no usable PVID", host, bridge_port)
        return _NO_DATA, ""

    return _OK, vlan


def _ipv4_or_none(value) -> Optional[str]:
    try:
        return str(ipaddress.IPv4Address(str(value).strip()))
    except (ipaddress.AddressValueError, ValueError):
        return None


def find_vlan(
    switch_name: str,
    switch_ip: str,
    local_mac: Optional[bytes],
    cancel_event: threading.Event,
    timeout: float = 60.0,
) -> Optional[str]:
    """
    Look up our access VLAN via SNMP on the switch we're plugged into.

    switch_name : name LLDP/CDP reported -- the identity every answer
                  must match before it is trusted
    switch_ip   : management IP LLDP/CDP advertised, if any. Queried
                  first: it is the exact switch we're on, unlike the
                  gateway, which is often a router.
    local_mac   : our 6-byte MAC, used to find our bridge port

    Returns a VLAN ID string, or None if it can't be determined safely.
    """
    if not getattr(discover_snmp, "_SNMP_AVAILABLE", False):
        log.debug("VLAN enrich: snmpget not installed -- skipping")
        return None

    expected = normalize_switch_name(switch_name)
    if not expected or not local_mac:
        return None

    hosts: list[str] = []
    for candidate in (switch_ip, discover_snmp._read_default_gateway()):
        ip = _ipv4_or_none(candidate)
        if ip and ip not in hosts:
            hosts.append(ip)
    if not hosts:
        log.debug("VLAN enrich: no switch IP or gateway to query")
        return None

    communities = discover_snmp._get_communities()
    deadline = time.monotonic() + timeout

    for host in hosts:
        for community in communities:
            if cancel_event.is_set() or time.monotonic() > deadline:
                return None
            outcome, vlan = _query_host(host, community, local_mac, expected)
            if outcome == _OK:
                log.info(
                    "VLAN enrich: VLAN %s for our port, from SNMP on %s (%s)",
                    vlan, host, expected,
                )
                return vlan
            if outcome in (_WRONG_SWITCH, _NO_DATA):
                break  # this host can't help; no point trying more communities on it

    log.debug("VLAN enrich: no VLAN found on %s", hosts)
    return None
