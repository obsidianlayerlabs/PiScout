# Changes from upstream

This fork targets the Raspberry Pi 3B+ with the Waveshare 2.7inch e-Paper HAT V2. Upstream is [MKWB/PiScout](https://github.com/MKWB/PiScout).

Every file not listed here is unchanged from upstream, which keeps upstream updates easy to merge.

## New files

| File | Purpose |
| --- | --- |
| `buttons.py` | Reads the HAT's four buttons (GPIO 5, 6, 13, 19). Each press runs its check on its own thread. A second press while that button's check is still running is ignored rather than started twice. |
| `ping_gateway.py` | Gateway ping (finds the default route from `/proc/net/route`) and internet ping. |
| `dns_lookup.py` | Resolves a hostname with a timeout, using the system resolver. |
| `jitter_test.py` | Sends a burst of pings and reports packet loss plus minimum, average and maximum latency. |
| `vlan_enrich.py` | Looks up the access VLAN over SNMP when the winning discovery result had none. See *VLAN resolution* below. |
| `docs/SETUP.md` | Build guide: kit contents, case assembly, installation, use and troubleshooting. |

## Changed files

**`display_epaper.py`** — rewritten for the 2.7inch V2 panel (`epd2in7_V2`, 264 × 176). It adds the button-label column down the right edge and moves the protocol tag to the top left.

Partial refresh passes the panel's native portrait dimensions (176 × 264), read from the driver. `getbuffer()` rotates a landscape image into that portrait buffer, so the refresh window must be given in portrait coordinates. Passing landscape dimensions left part of the screen out of every partial refresh, with no error raised.

**`main.py`**

- Wires the four buttons to their checks, with result screens for each.
- The IP line shows this device's address, read from the systemd-networkd DHCP lease, with a kernel query as fallback. Upstream shows the switch's management address.
- Screen watchdog: if a button result screen is still up after 30 seconds, the last main screen is restored. Button handlers restore from a shared record of the main screen. Previously each handler saved whatever was on screen when it started; pressing a second button during a first button's result could save that result as the screen to go back to.
- VLAN resolution chain — see below.

**`config.py`** — adds settings for the buttons, the screen watchdog and VLAN resolution. All have defaults and environment-variable overrides; existing upstream settings are unchanged.

**`discover_snmp.py`** — two fixes to how it reads a port's VLAN:

1. In the bridge-table path, `dot1qPvid` is now read at the bridge port number. Upstream read it at the interface index. The Q-BRIDGE-MIB indexes this table by bridge port, so upstream's lookup only works on switches where the two numbering schemes happen to match. Elsewhere it returns a different port's VLAN.
2. In the LLDP-MIB path, the VLAN lookup is removed. It assumed the LLDP port number equals the interface index, which isn't reliable. When this path wins, `vlan_enrich.py` fills in the VLAN instead.

Both are worth offering upstream, since they affect every PiScout user.

**`install.sh`** — installs `python3-rpi-lgpio` instead of `python3-rpi.gpio`. Edge detection in the legacy library fails on current Raspberry Pi OS kernels, so the buttons can't work with it. It also removes any pip-installed copy of `RPi.GPIO`, which would otherwise take precedence over the apt package.

**`requirements.txt`** — lists `rpi-lgpio` in place of `RPi.GPIO`, to match.

**`README.md`** — adds an *About this fork* section and points the clone command at this fork.

## VLAN resolution

The access VLAN normally comes from an optional LLDP field or from CDP. Many switches send neither: some vendors leave the LLDP field off by default, and CDP is Cisco-only. The display now tries each source in turn and labels the result:

| Source | Screen shows |
| --- | --- |
| Switch-reported (LLDP, CDP or SNMP) | `VLAN: 120` |
| Inferred from a subnet map in config | `VLAN: 120 (subnet)` |
| Nothing available | `VLAN: not advertised` |

Upstream stops SNMP discovery as soon as LLDP returns a result, and a result without a VLAN still counts. `vlan_enrich.py` therefore runs a separate SNMP lookup afterwards. It queries the switch's own advertised management address first, then the gateway.

It finds the bridge port where the switch learned this device's MAC address, then reads that port's VLAN. The answer is accepted only if the switch's name matches the one LLDP or CDP reported. An upstream router also learns this device's MAC — on its uplink port — and would otherwise return the uplink's VLAN.

The looked-up VLAN is stored separately from the discovery result. Passive refresh replaces the result with each new LLDP frame, which carries no VLAN either, so storing it in the result would erase it every 30 seconds or so.
