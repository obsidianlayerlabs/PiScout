# PiScout Setup Guide

## Overview

PiScout tells you which switch, port and VLAN an Ethernet jack connects to. Plug it into the jack and read the screen — no laptop needed.

It listens for the identity announcements switches send (LLDP and CDP), falls back to SNMP where available, and shows the result on a 2.7-inch e-paper display. Four buttons run one-press network checks: gateway reachability, internet reachability, DNS, and link quality.

The kit contains the case and mounting hardware only. The Raspberry Pi, PoE HAT and display are bought separately — see the next section.

## What you need

Check the kit contents before you start, then gather the electronics and tools listed below.

**In the kit**

| Part | Qty |
| --- | --- |
| Top case | 1 |
| Bottom case (heat-set inserts fitted) | 1 |
| Button caps | 4 |
| 2×20-pin extra-tall stacking header | 1 |
| 2×2-pin extra-tall stacking header | 1 |
| M2.5 × 18 mm brass standoff | 6 |
| M2.5 × 28 mm brass standoff | 1 |
| M2.5 × 5 mm PETG-CF standoff | 1 |
| M2.5 × 10 mm screw | 3 |
| Washer insert | 3 |

**Supplied by you**

| Part | Notes |
| --- | --- |
| Raspberry Pi 3B+ | The case is sized for this board. |
| Waveshare 2.7inch e-Paper HAT **V2** | Must be V2 — check for the "V2" sticker. The V1 panel uses a different driver. |
| LoveRPi PoE HAT (3KV isolated) | Powers the device from a PoE switch port. The assembly below assumes it is fitted. |
| microSD card, 16 GB or larger | A name-brand card holds up better to repeated power cuts. |
| Power source | An 802.3af PoE switch port, or a 5 V 2.5 A micro-USB supply. |
| Small Phillips screwdriver | For the M2.5 screws. |
| A computer with an SD card reader | To write the operating system. |

## Assembly

Build the stack from the bottom up, in this order. Work with everything unpowered, and press headers straight down — a bent pin is the most common assembly fault.

1. Place the Raspberry Pi 3B+ in the bottom case.
2. Install three of the 18 mm standoffs.
3. Install the 28 mm standoff.
4. Fit the extra-tall stacking headers: the 2×20-pin header onto the GPIO pins, and the 2×2-pin header onto the 4-pin PoE header.
5. Seat the PoE HAT on the stacking headers.
6. Press one washer insert into each PoE HAT mounting hole. These level the HAT so its pins connect evenly.
7. Screw the remaining three 18 mm standoffs on top.
8. Install the 5 mm PETG-CF standoff.
9. Seat the Waveshare 2.7inch e-Paper HAT on the stacking header. Handle it by the board edges — the thin ribbon cable between the board and the glass panel is fragile.
10. Fit the button caps into the top case from the inside: the dome pokes out through the hole, and the flange sits flush in the recess behind it. The flange is what keeps each cap from falling out.
11. Lower the top case over the stack, lining the caps up with the display's four switches.
12. Fasten with the three M2.5 × 10 mm screws. Snug is enough — overtightening can strip the heat-set inserts.

Press each button once before moving on. Each should click and spring back on its own.

## Software installation

Installation takes about 15 minutes and needs a wired network connection with DHCP and internet access. The installer turns off Wi-Fi and Bluetooth, so do every step over Ethernet.

1. **Write the operating system.** Open Raspberry Pi Imager (raspberrypi.com/software) and choose Raspberry Pi 3 › Raspberry Pi OS Lite (64-bit). In the OS customisation settings, set a hostname, a username and password, and enable SSH. Write the card.

2. **Boot and connect.** Insert the card, connect Ethernet, and power on. Wait about 90 seconds, then connect from your computer:

    ```
    ssh <username>@<hostname>.local
    ```

    If `.local` doesn't resolve, find the device's IP address in your router's client list and use that instead.

3. **Update the system.**

    ```
    sudo apt update && sudo apt full-upgrade -y
    ```

4. **Install PiScout, then reboot.** The installer sets up the display, the buttons and the background service. It also enables the display interface (SPI), which only takes effect after a reboot.

    ```
    sudo apt install -y git
    sudo git clone https://github.com/YOUR-USERNAME/PiScout.git /opt/piscout
    cd /opt/piscout
    sudo bash install.sh
    sudo reboot
    ```

The display shows "Waiting for link..." once it has started. Press each button once to check it responds — each should briefly show a result screen.

**Optional: protect the SD card.** PoE devices lose power whenever they are unplugged, which can corrupt the card over time. The base software includes `make_readonly.sh`, which makes the card read-only. Run it once from `/opt/piscout` on stable USB power after everything works. Later software updates then need `sudo remount-rw` first.

**Updating.** To get the latest version later:

```
cd /opt/piscout
sudo git pull
sudo bash install.sh
sudo reboot
```

## Using PiScout

Plug PiScout into the jack you want to identify; most switches answer within 3 to 8 seconds. The screen moves through three stages:

1. **Waiting for link...** — no live Ethernet connection yet.
2. **Scanning...** — connected, and listening for the switch to identify itself.
3. **Result** — the switch and port details below.

Cisco switches that only speak CDP can take up to 60 seconds, because they announce themselves on a fixed timer. If nothing arrives after 120 seconds, PiScout stops waiting.

| Line | What it shows |
| --- | --- |
| Top-left tag | Where the data came from: LLDP, CDP or SNMP |
| SW | The switch's name |
| IP | This PiScout's own address, assigned by DHCP — not the switch's |
| PORT | The switch port you are plugged into |
| VLAN | The access VLAN — see *Reading the VLAN line* |
| VOICE | The voice VLAN, if the switch advertises one |
| LINK | Negotiated speed and duplex, e.g. 1G FD |

PiScout keeps listening while connected and updates the screen if the switch's details change.

E-paper holds its last image even with the power off. A screen showing old details doesn't mean the device is running — after moving PiScout to a new jack, wait for the screen to redraw.

## The four buttons

Each button runs one network check. The labels down the right edge of the screen match the buttons, in the same order.

| Label | What it checks | Healthy result |
| --- | --- | --- |
| [GW] | Pings your default gateway | Status: REACHABLE |
| [WAN] | Pings an internet host (1.1.1.1) | Status: REACHABLE |
| [DNS] | Looks up google.com, then pings the address it got back | Status: RESOLVED, Internet Route: OK |
| [JIT] | Sends 10 pings to 1.1.1.1 and measures consistency | Loss: 0%, Min / Avg / Max close together |

The JIT test takes about 10 seconds. The others finish in a second or two.

Results stay on screen for 4 seconds, then the main screen returns. If a result screen ever stays up, PiScout brings the main screen back after 30 seconds. Pressing a button again while its check is still running does nothing.

The DNS screen separates two different faults:

| Status | Internet Route | Meaning |
| --- | --- | --- |
| RESOLVED | OK | DNS and internet both working |
| FAILED | OK | Internet works, but DNS is broken |
| FAILED | FAIL | No internet path at all — DNS failing is a symptom |

## Reading the VLAN line

The VLAN line always tells you how much to trust it. PiScout never shows a guessed VLAN as if the switch reported it.

| Screen shows | Meaning |
| --- | --- |
| VLAN: 120 | Reported by the switch |
| VLAN: 120 (subnet) | Inferred from PiScout's IP address, using a map you set up — see *Configuration* |
| VLAN: not advertised | The switch isn't sharing it, and no subnet map matched |

"Not advertised" is a statement about the switch's settings, not a PiScout fault. The VLAN normally arrives in an optional part of the switch's LLDP announcement, and many switches ship with it turned off. Cisco switches report it through CDP instead. Where SNMP is enabled, PiScout also asks the switch directly.

PiScout can't work it out from network traffic either: on an access port, the switch removes VLAN tags before traffic reaches the device.

To get a reported VLAN, ask your network team to turn on the Port VLAN ID announcement. On Extreme Networks EXOS switches, for example:

```
configure lldp ports all advertise vendor-specific dot1 port-vlan-ID
```

This shares only each port's untagged VLAN number, which is often an easier request than enabling SNMP.

## Configuration

PiScout works without any configuration. To change a setting, use a service override rather than editing `config.py` — edits there conflict with software updates, which replace the file.

Open the override file:

```
sudo systemctl edit piscout
```

Add one `Environment` line per setting, then save and restart:

```
[Service]
Environment="PS_WAN_PING_HOST=8.8.8.8"
Environment="PS_VLAN_SUBNET_MAP=10.10.120.0/24=120,10.10.130.0/24=130"
```

```
sudo systemctl restart piscout
```

| Variable | Default | What it controls |
| --- | --- | --- |
| PS_WAN_PING_HOST | 1.1.1.1 | Host pinged by [WAN] |
| PS_DNS_LOOKUP_HOST | google.com | Name looked up by [DNS] |
| PS_JITTER_TEST_HOST | same as WAN host | Host pinged by [JIT] |
| PS_JITTER_TEST_COUNT | 10 | Pings per [JIT] test |
| PS_SCREEN_WATCHDOG | 30 | Seconds before a stuck result screen returns to the main screen; 0 turns it off |
| PS_SNMP_COMMUNITY | (none) | Your SNMP community string, tried first |
| PS_VLAN_SNMP_ENRICH | 1 | Ask the switch for the VLAN over SNMP when LLDP doesn't include it; 0 turns it off |
| PS_VLAN_SUBNET_MAP | (none) | Subnet-to-VLAN map, as `subnet=vlan` pairs separated by commas |
| PS_DISCOVERY_TIMEOUT | 120 | Seconds to wait for the switch to identify itself |
| PS_NETWORK_INTERFACE | eth0 | Network interface to watch |
| PS_LOG_LEVEL | WARNING | Log detail; DEBUG for troubleshooting |

Only set a subnet map where each subnet belongs to exactly one VLAN. When several entries match, the most specific subnet wins.

**SNMP on monitored networks.** To identify switches over SNMP, PiScout tries a list of common community strings such as "public". On networks that alert on failed SNMP logins, this can appear in security logs. Setting `PS_VLAN_SNMP_ENRICH=0` reduces it; setting `PS_SNMP_COMMUNITY` to your real string makes the first attempt succeed.

## Troubleshooting

Most problems come down to the network jack, the switch's settings, or the service not running. Start with the symptom.

| Symptom | Check | Fix |
| --- | --- | --- |
| Screen doesn't change | `sudo systemctl status piscout` — is it active? | `sudo systemctl restart piscout`. E-paper keeps its last image, so a frozen screen often just means the service stopped. |
| "Waiting for link..." with the cable plugged in | `cat /sys/class/net/eth0/carrier` — 1 means a live link, 0 means none | At 0, try another cable or jack, or check the port is enabled. At 1, restart the service and check the logs. |
| Stuck on "Scanning..." | Run the switch check below | The switch may not send LLDP or CDP on that port. CDP-only switches can take up to 60 seconds. |
| IP shows "Unknown" | Is there a DHCP server on this VLAN? | PiScout still identifies the switch without an IP, but the button checks need one. |
| VLAN shows "not advertised" | See *Reading the VLAN line* | Ask for the Port VLAN ID announcement to be enabled, or set a subnet map. |
| Buttons do nothing | Look in the logs for "Failed to add edge detection" | Re-run `sudo bash /opt/piscout/install.sh`, then reboot. It reinstalls the button driver library and removes any conflicting copy. |
| Can't connect over SSH | Are you on Wi-Fi? | The installer turns Wi-Fi off. Connect over Ethernet. |
| "GPIO busy" when running a command by hand | Is the service running? | Run `sudo systemctl stop piscout` first. Only one program can use the buttons and display at a time. |

**Check what the switch is sending.** This shows the raw LLDP announcements arriving on the cable, independent of PiScout:

```
sudo systemctl stop piscout
sudo tcpdump -i eth0 -n -vv 'ether proto 0x88cc' -c 3
sudo systemctl start piscout
```

Nothing arriving means the switch isn't sending LLDP on that port. A frame with no "Port VLAN Id" line means the switch isn't sharing the VLAN.

**Read the logs.** In normal operation PiScout logs almost nothing, so an empty log is not a fault:

```
sudo journalctl -u piscout -n 50 --no-pager
```

For a full trace, add `Environment="PS_LOG_LEVEL=DEBUG"` with `sudo systemctl edit piscout`, restart, and reproduce the problem. Remove the line afterwards.
