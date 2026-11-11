"""
config.py

PiScout configuration.

Edit this file to adjust behavior. All values have safe defaults
that work without any changes on most networks.

Most settings can also be overridden via environment variables using
the PS_ prefix (e.g. PS_NETWORK_INTERFACE=eth1). Environment variables
take precedence over the values in this file.

What this file does:
  - Define which display type to use
  - Configure the network interface
  - Set SNMP community strings to try
  - Tune timing values

What this file does NOT do:
  - Initialize hardware
  - Capture packets
  - Parse data
  - Draw anything on screen
"""

import os

# ============================================================
# ================= USER DISPLAY SELECTION ===================
# ============================================================
# Valid values:
#   "epaper" = Waveshare 2.7" HAT V2 e-paper display (default)
#   "lcd"    = Waveshare 1.44" LCD HAT display
#
# Override without editing this file:
#   PS_DISPLAY_TYPE=lcd python3 main.py
# ============================================================

DISPLAY_TYPE = os.environ.get("PS_DISPLAY_TYPE", "epaper")

# ============================================================
# -------------------- NETWORK SETTINGS ----------------------
# ============================================================

# Network interface to monitor.
# Override: PS_NETWORK_INTERFACE=eth1
NETWORK_INTERFACE = os.environ.get("PS_NETWORK_INTERFACE", "eth0")

# How long to wait for any discovery method before giving up.
# 120 seconds allows CDP's 60-second cycle to be caught up to 2 times.
# Override: PS_DISCOVERY_TIMEOUT=60
DISCOVERY_TIMEOUT = float(os.environ.get("PS_DISCOVERY_TIMEOUT", "120.0"))

# How long after the "Scanning..." screen appears before port data is
# allowed to replace it. This ensures the user sees the screen for at
# least this many seconds even if SNMP responds almost instantly.
# The e-paper draw time (~3s) is additional buffer on top of this.
RESULT_REVEAL_DELAY = 1.5

# How long to wait before displaying a partial result (one where switch_name
# is present but port is missing, or vice versa). Some switches such as
# FortiSwitch omit optional LLDP TLVs like the port VLAN ID. Showing partial
# data after this delay gives the user something useful rather than a blank
# screen while the device continues trying to get complete information.
# The background passive listener will upgrade the display to full data
# the moment a complete advertisement is received.
# Set to 0 to disable partial display entirely (wait for complete data only).
PARTIAL_DISPLAY_DELAY = 30.0

# How long to block waiting for a raw LLDP/CDP frame on each receive call.
# 2.0 seconds keeps the passive listener responsive to link-down events.
RAW_RECEIVE_TIMEOUT = 2.0

# ============================================================
# -------------------- SNMP SETTINGS -------------------------
# ============================================================

# User-defined SNMP community string.
# If set, this is tried FIRST before the built-in list below.
# Leave as empty string "" if you do not have a specific string.
# Override: PS_SNMP_COMMUNITY=mystring
SNMP_USER_COMMUNITY = os.environ.get("PS_SNMP_COMMUNITY", "")

# Built-in community strings tried in order after SNMP_USER_COMMUNITY.
# Covers the vast majority of network environments without configuration.
SNMP_COMMUNITY_STRINGS = [
    "public",
    "cisco",
    "community",
    "private",
    "manager",
    "snmp",
    "monitor",
    "readonly",
]

# Seconds to wait for each individual SNMP response.
# 1.0 second is sufficient for most switches while keeping the race
# responsive — if passive wins, the SNMP thread stops within 1 second.
SNMP_TIMEOUT = 1.0

# Number of SNMP retries per query before moving to the next community string.
SNMP_RETRIES = 1

# Seconds to wait for a DHCP lease before falling back to ARP observation.
SNMP_DHCP_WAIT = 8.0

# Seconds to listen for ARP traffic when DHCP is unavailable.
SNMP_ARP_WAIT = 3.0

# ============================================================
# -------------------- DISPLAY SETTINGS ----------------------
# ============================================================

# Optional font path used by both display types.
DISPLAY_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# ============================================================
# ------------------- E-PAPER SETTINGS -----------------------
# ============================================================

# Minimum seconds between normal e-paper refreshes.
EPAPER_MIN_REFRESH_INTERVAL = 10

# If True, the panel sleeps after each update (image stays visible).
EPAPER_AUTO_SLEEP = True

# Full refresh after this many partial refreshes to clear ghosting.
EPAPER_PARTIAL_REFRESH_LIMIT = 8

# ============================================================
# --------------------- LCD SETTINGS -------------------------
# ============================================================

LCD_ROTATE_180 = True
LCD_CLEAR_ON_START = True
LCD_BACKGROUND_COLOR = (0, 0, 0)
LCD_TEXT_COLOR = (255, 255, 255)
LCD_BACKLIGHT_BRIGHTNESS = 100

# ============================================================
# ------------------- BUTTON SETTINGS -------------------------
# ============================================================
# Onboard push-buttons on the Waveshare 2.7" e-Paper HAT V2.
# KEY1, KEY2, KEY3, and KEY4 are all wired up (see buttons.py / main.py):
#   KEY1 — pings the default gateway on NETWORK_INTERFACE
#   KEY2 — pings WAN_PING_HOST (an external host) on NETWORK_INTERFACE,
#          to check internet reachability beyond the local gateway
#   KEY3 — resolves DNS_LOOKUP_HOST via the system resolver, to check
#          whether DNS resolution is working
#   KEY4 — sends JITTER_TEST_COUNT pings to JITTER_TEST_HOST (falls back
#          to WAN_PING_HOST if unset) and reports average RTT + jitter
# All four show their result on screen for BUTTON_PING_RESULT_DISPLAY_SECONDS
# before restoring whatever was on screen beforehand. A small footer row
# on the display ("K1:GW K2:WAN K3:DNS K4:JIT") identifies which button
# does what — see BUTTON_LABELS in main.py if you rewire a callback and
# need to update its label too.
# ============================================================

# Set False to skip button setup entirely (e.g. testing off the real HAT).
BUTTONS_ENABLED = True

# Seconds to show a button-triggered result before restoring the previous screen.
BUTTON_PING_RESULT_DISPLAY_SECONDS = 4.0

# External host KEY2 pings to check WAN/internet reachability.
# 1.1.1.1 (Cloudflare) needs no DNS lookup and responds to ICMP reliably.
# Override: PS_WAN_PING_HOST=8.8.8.8
WAN_PING_HOST = os.environ.get("PS_WAN_PING_HOST", "1.1.1.1")

# Hostname KEY3 resolves to check DNS.
# Override: PS_DNS_LOOKUP_HOST=cloudflare.com
DNS_LOOKUP_HOST = os.environ.get("PS_DNS_LOOKUP_HOST", "google.com")

# Host KEY4 pings repeatedly to measure jitter. Leave empty to reuse
# WAN_PING_HOST instead of maintaining a separate target.
# Override: PS_JITTER_TEST_HOST=1.1.1.1
JITTER_TEST_HOST = os.environ.get("PS_JITTER_TEST_HOST", "")

# Number of pings KEY4 sends per jitter test. At the default ~1/sec
# ping interval, 10 pings takes roughly 10 seconds to complete.
JITTER_TEST_COUNT = int(os.environ.get("PS_JITTER_TEST_COUNT", "10"))


# ============================================================
# ------------------ SCREEN WATCHDOG -------------------------
# ============================================================
# Transient screens (button results, "Pinging..." progress screens)
# are supposed to be replaced by the normal home screen a few seconds
# after they appear -- the button handler that drew them does that
# itself once BUTTON_PING_RESULT_DISPLAY_SECONDS elapses.
#
# This watchdog is the backstop for when that does not happen: if a
# transient screen is still showing after this many seconds, a
# background thread forces the last real screen back. Under healthy
# operation it never fires.
#
# Set to 0 to disable the watchdog entirely.
# Override: PS_SCREEN_WATCHDOG=60
SCREEN_WATCHDOG_SECONDS = float(os.environ.get("PS_SCREEN_WATCHDOG", "30"))


# ============================================================
# ------------------- VLAN RESOLUTION ------------------------
# ============================================================
# The access VLAN is normally reported by the switch (LLDP Port VLAN
# ID TLV, or CDP Native VLAN). Many networks disable those, so PiScout
# falls back through other sources. The screen always shows where the
# value came from:
#
#   "VLAN: 120"            reported by the switch (LLDP / CDP / SNMP)
#   "VLAN: 120 (subnet)"   inferred from VLAN_SUBNET_MAP below
#   "VLAN: not advertised" no source available
#
# The absence of a VLAN is itself useful: it tells you the switch has
# the TLV turned off, not that PiScout failed.
# ============================================================

# After a discovery win with no VLAN, query the switch over SNMP for
# it -- aimed at the switch's own LLDP-advertised management IP, and
# only trusted if the switch identifies itself by the same name LLDP
# reported. Uses the same community strings as SNMP discovery.
#
# Set False on networks where failed SNMP community attempts would
# trigger security alerts.
# Override: PS_VLAN_SNMP_ENRICH=0
VLAN_SNMP_ENRICH = os.environ.get("PS_VLAN_SNMP_ENRICH", "1") != "0"

# Give up on SNMP VLAN enrichment after this many seconds.
VLAN_SNMP_ENRICH_TIMEOUT = float(os.environ.get("PS_VLAN_SNMP_ENRICH_TIMEOUT", "60"))

# Optional: infer the VLAN from the DHCP subnet when no switch-reported
# VLAN is available. Leave empty to disable (the default) -- this only
# makes sense where each subnet maps to exactly one VLAN.
# Most specific matching subnet wins. Shown on screen as "(subnet)".
#
# Either edit the dict:
#   VLAN_SUBNET_MAP = {"10.10.120.0/24": 120, "10.10.130.0/24": 130}
# or set an environment variable, no file edit needed:
#   PS_VLAN_SUBNET_MAP="10.10.120.0/24=120,10.10.130.0/24=130"
VLAN_SUBNET_MAP = {}
VLAN_SUBNET_MAP_STR = os.environ.get("PS_VLAN_SUBNET_MAP", "")

# ============================================================
# ---------------------- LOG LEVEL ---------------------------
# ============================================================
# "WARNING" for normal appliance use (minimizes SD card writes).
# "DEBUG" for troubleshooting.
#
# Override without editing this file:
#   PS_LOG_LEVEL=DEBUG python3 main.py
# ============================================================

LOG_LEVEL = os.environ.get("PS_LOG_LEVEL", "WARNING")

# ============================================================
# ------------------- HISTORY SETTINGS -----------------------
# ============================================================
# Controls whether and how port discovery results are saved to disk.
#
# PORT_HISTORY_MODE:
#   0 = Off (default) — nothing written, fully compatible with read-only
#   1 = Port History — saves last PORT_HISTORY_LIMIT results to
#       PORT_HISTORY_PATH/history.jsonl
#   2 = Debug Log — writes verbose rotating log to
#       PORT_HISTORY_PATH/debug.log
#
# Notes:
#   - Modes 1 and 2 require the writable /data partition to be mounted.
#     Run make_readonly.sh to set this up.
#   - Mode 0 is safe on a read-only filesystem with no writable partition.
#   - Each history entry is ~180 bytes. 50 entries = ~9KB total.
#   - The debug log rotates at 5MB and keeps 3 backup files.
#
# Override: PS_HISTORY_MODE=1
# Override: PS_HISTORY_PATH=/data/piscout
# ============================================================

PORT_HISTORY_MODE = int(os.environ.get("PS_HISTORY_MODE", "0"))
PORT_HISTORY_LIMIT = int(os.environ.get("PS_HISTORY_LIMIT", "50"))
PORT_HISTORY_PATH = os.environ.get("PS_HISTORY_PATH", "/data/piscout")
