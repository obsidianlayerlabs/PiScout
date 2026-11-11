"""
jitter_test.py

Measures network jitter by sending several pings to a host and looking
at how much the round-trip time varies from one packet to the next.

Jitter here is computed as the average of the absolute differences
between consecutive ping RTTs (the RFC 3550/RTP-style "instantaneous
jitter" definition) rather than the standard deviation of all RTTs —
it reflects how much delay wobbles packet-to-packet, which is what
actually hurts real-time traffic like voice and video.

Used by the KEY4 button.

What this file does:
  - Send several pings to a host and parse the round-trip times
  - Compute average RTT and jitter from those samples

What this file does NOT do:
  - Touch the display
  - Know anything about GPIO or buttons
"""

import logging
import re
import subprocess

log = logging.getLogger(__name__)

_PING_TIME_RE = re.compile(r"time[=<]([\d.]+)\s*ms")


def measure_jitter(host: str, interface: str = None, count: int = 10, timeout: float = 1.5) -> dict:
    """
    Send `count` pings to `host` (optionally bound to `interface`) and
    compute jitter from the round-trip times.

    Returns a dict:
        {
            "host": str,
            "success": bool,             # True if at least 2 replies were received
            "replies": int,              # number of replies received
            "sent": int,                 # number of pings sent
            "avg_rtt_ms": float or None,
            "jitter_ms": float or None,
            "min_rtt_ms": float or None,
            "max_rtt_ms": float or None,
            "loss_percent": float,       # 0-100, meaningful even on total failure
        }
    """
    cmd = ["ping", "-c", str(count), "-W", str(timeout)]
    if interface:
        cmd += ["-I", interface]
    cmd.append(host)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            # Pings are sent ~1/sec by default, plus per-packet timeout slack.
            timeout=(timeout * count) + 5.0,
        )
    except Exception:
        log.debug("Jitter test subprocess failed for host %s", host, exc_info=True)
        return {
            "host": host,
            "success": False,
            "replies": 0,
            "sent": count,
            "avg_rtt_ms": None,
            "jitter_ms": None,
            "min_rtt_ms": None,
            "max_rtt_ms": None,
            "loss_percent": 100.0,
        }

    rtts = [float(m) for m in _PING_TIME_RE.findall(proc.stdout)]
    replies = len(rtts)
    loss_percent = ((count - replies) / count * 100.0) if count > 0 else 100.0

    if replies < 2:
        # Need at least 2 samples to compute a packet-to-packet difference,
        # but min/max/loss are still meaningful with 0 or 1 replies.
        return {
            "host": host,
            "success": False,
            "replies": replies,
            "sent": count,
            "avg_rtt_ms": (rtts[0] if rtts else None),
            "jitter_ms": None,
            "min_rtt_ms": (rtts[0] if rtts else None),
            "max_rtt_ms": (rtts[0] if rtts else None),
            "loss_percent": loss_percent,
        }

    diffs = [abs(rtts[i] - rtts[i - 1]) for i in range(1, len(rtts))]
    jitter_ms = sum(diffs) / len(diffs)
    avg_rtt_ms = sum(rtts) / len(rtts)

    return {
        "host": host,
        "success": True,
        "replies": replies,
        "sent": count,
        "avg_rtt_ms": avg_rtt_ms,
        "jitter_ms": jitter_ms,
        "min_rtt_ms": min(rtts),
        "max_rtt_ms": max(rtts),
        "loss_percent": loss_percent,
    }
