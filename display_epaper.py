"""
display_epaper.py

This file handles the Waveshare 2.7" e-Paper HAT (264 x 176), driven
from a Raspberry Pi 3B+.

CHANGES FROM UPSTREAM (originally written for the 2.13" V3 panel on a
Pi Zero 2 W):
  - Panel driver switched from `epd2in13_V3` to `epd2in7_V2` (confirmed
    V2 hardware — has the "V2" sticker on the back of the board).
    epd2in7_V2 supports partial refresh, same as the 2.13" V3 panel
    this code was originally written for, so the ghost-prevention /
    partial-refresh logic below works unchanged.
  - DISPLAY_WIDTH / DISPLAY_HEIGHT updated to the 2.7" panel's native
    264 x 176 resolution (after the existing 180-degree rotation).
  - Nothing about the Pi 3B+ itself required a code change here: the
    3B+ exposes the same 40-pin GPIO/SPI0 header as the Zero 2 W, so
    wiring and the SPI-based protocol are identical. The 3B+ also
    ships with the header already soldered.

What this file does:
  - Start the e-paper display
  - Draw a fixed header and 5 body lines onto an image
  - Show that image on the screen using partial or full refresh as
    appropriate for the detected driver
  - Limit how often the screen refreshes
  - Force a refresh when VLAN or VOICE changes
  - Manage ghosting by forcing a full refresh every N partial refreshes
    (only relevant when the V2/partial-refresh driver is in use)
  - Put the display to sleep when appropriate

What this file does NOT do:
  - Read config files
  - Capture packets
  - Parse LLDP or CDP data
  - Store broader application state
  - Decide what lines to show (that is main.py's job)
"""

import logging
import threading
import time

from PIL import Image, ImageDraw, ImageFont

from waveshare_epd import epd2in7_V2 as _epd_module

from parse_utils import normalize_display_lines

log = logging.getLogger(__name__)


class EPaperDisplay:
    # Screen size in pixels for the Waveshare 2.7" e-Paper display,
    # landscape orientation (native panel is 176 x 264 portrait; this
    # class renders in portrait buffer space and rotates 180 for
    # mounting, same as upstream — see _render_image).
    DISPLAY_WIDTH = 264
    DISPLAY_HEIGHT = 176

    # Text position on the screen.
    LEFT_MARGIN = 10
    TOP_MARGIN = 4

    # Font settings for the 6 body lines. Slightly larger than the
    # 2.13" defaults since the 2.7" panel has more vertical room.
    BASE_FONT_SIZE = 18
    MIN_FONT_SIZE = 12
    LINE_SPACING = 3

    # Fixed header drawn at the top of every screen.
    TITLE_TEXT = "PiScout"
    TITLE_FONT_SIZE = 20
    TITLE_UNDERLINE_GAP = 1
    TITLE_BODY_GAP = 3

    # Default font file.
    DEFAULT_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

    # Font size for the optional button-label sidebar (see
    # button_labels / set_button_labels). Kept small and separate from
    # MIN_FONT_SIZE so it never competes with body text for space.
    FOOTER_FONT_SIZE = 10
    # Gap, in px, between the body text column and the label sidebar.
    SIDEBAR_GAP = 4
    # Margin, in px, between the right edge of the screen and the labels.
    SIDEBAR_RIGHT_MARGIN = 3
    # Margin, in px, between the bottom edge of the screen and the
    # bottom of the sidebar's evenly-spaced label region.
    SIDEBAR_BOTTOM_MARGIN = 4

    # Number of partial refreshes before a full refresh is forced to clear
    # ghosting. Starting the counter at this limit ensures the very first
    # render is always a full refresh, giving a clean slate on boot.
    PARTIAL_REFRESH_LIMIT = 8

    def __init__(
        self,
        font_path=None,
        min_refresh_interval=10,
        auto_sleep=True,
        startup_mode=True,
        partial_refresh_limit=None,
        button_labels=None,
    ):
        """
        Set up the display object.

        font_path:
            Optional path to a .ttf font file.
        min_refresh_interval:
            Minimum number of seconds between normal display refreshes.
        auto_sleep:
            If True, the panel goes back to sleep after normal updates.
        startup_mode:
            If True, the display stays awake between updates during early
            boot so the panel does not repeatedly init/sleep before the
            first real result arrives.
        partial_refresh_limit:
            Override for PARTIAL_REFRESH_LIMIT. Useful for testing.
        button_labels:
            Optional list of up to 4 short strings identifying what
            KEY1-KEY4 do (e.g. ["GW", "WAN", "DNS", "JIT"]). When set, a
            small vertical sidebar of "[LABEL]" text is drawn along the
            right edge of every screen, stacked top-to-bottom in
            KEY1-KEY4 order. Pass None (default) to omit it. Can also
            be changed later via set_button_labels().
        """
        self.font_path = font_path or self.DEFAULT_FONT_PATH
        self.min_refresh_interval = min_refresh_interval
        self.auto_sleep = auto_sleep
        self.startup_mode = startup_mode

        # Allow the limit to be overridden at construction time.
        self._partial_refresh_limit = (
            int(partial_refresh_limit)
            if partial_refresh_limit is not None
            else self.PARTIAL_REFRESH_LIMIT
        )

        # Create the Waveshare display object using the confirmed
        # epd2in7_V2 driver imported above.
        self.epd = _epd_module.EPD()

        # RLock allows one method to safely call another method that also
        # uses the same lock, which happens during sleep/wake sequences.
        self.lock = threading.RLock()

        # Track display state.
        self.initialized = False
        self.sleeping = False

        # Start at the limit so the first render always performs a full
        # refresh.
        self.partial_refresh_count = self._partial_refresh_limit

        # Tracks whether display_Base has been called at least
        # once. Partial refresh requires both frame buffers to be seeded
        # first. Until this is True, every refresh is forced to be a full
        # refresh.
        self._partial_base_ready = False

        # Save the last 5 normalized body lines shown on screen.
        self.last_lines = None
        self._last_protocol = ""

        # Save the last refresh time.
        self.last_refresh_time = 0.0

        # Optional sidebar labels for the 4 buttons (e.g. ["GW","WAN","DNS","JIT"]).
        # None means no sidebar is drawn. See set_button_labels().
        self.button_labels = list(button_labels)[:4] if button_labels else None

        # Preload the body font sizes once so we do not repeatedly read
        # from disk.
        self.font_cache = self._build_font_cache()

        # Load the fixed title font once.
        self.title_font = self._load_title_font()

        # Load the small sidebar-label font once.
        self.footer_font = self._load_footer_font()

    # ------------------------------------------------------------------ #
    # Public interface                                                   #
    # ------------------------------------------------------------------ #

    def initialize(self, clear_on_start=False):
        """
        Start the e-paper display.

        clear_on_start:
            If True, clear the screen to white when starting up.
            Leave False for faster appliance behavior so we can jump
            directly to the boot screen without an extra blank-screen flash.
        """
        with self.lock:
            if self.initialized and not self.sleeping:
                return
            self.epd.init()
            self.initialized = True
            self.sleeping = False
            # After any hardware init, the next display call must be a
            # full refresh to ensure a clean starting state.
            self.partial_refresh_count = self._partial_refresh_limit
            if clear_on_start:
                self.clear()

    def clear(self, sleep_after=True):
        """
        Clear the screen to white.

        sleep_after:
            If True, put the panel back to sleep after clearing.
        """
        with self.lock:
            self._ensure_awake()
            self.epd.Clear()
            self.last_lines = ["", "", "", "", "", ""]
            self.last_refresh_time = time.monotonic()
            self.partial_refresh_count = self._partial_refresh_limit
            if sleep_after and self.auto_sleep and not self.startup_mode:
                self.sleep()

    def sleep(self):
        """
        Put the e-paper display into sleep mode.

        The image remains visible on screen after sleep.
        """
        with self.lock:
            if not self.initialized or self.sleeping:
                return
            self.epd.sleep()
            self.sleeping = True

    def shutdown(self, clear_before_sleep=False):
        """
        Shut down the display safely.

        clear_before_sleep:
            If True, blank the screen before sleeping.
            Leave False so the last result stays visible on screen.
        """
        with self.lock:
            if not self.initialized:
                return
            if clear_before_sleep:
                try:
                    self._ensure_awake()
                    self.epd.Clear()
                    self.last_lines = ["", "", "", "", "", ""]
                    self.last_refresh_time = time.monotonic()
                except Exception:
                    pass
            try:
                self.epd.sleep()
            except Exception:
                pass
            self.sleeping = True
            self.initialized = False

    def set_startup_mode(self, enabled):
        """
        Enable or disable startup mode.

        Startup mode keeps the display awake between early updates to
        avoid constant init/sleep churn before the first real result
        arrives.

        Typical use:
          - startup_mode=True while booting and waiting for first discovery
          - startup_mode=False after the first real neighbor result is shown
        """
        with self.lock:
            self.startup_mode = bool(enabled)

    def show_lines(self, lines, force=False, protocol=""):
        """
        Show 5 body lines on the e-paper display.

        The fixed "PiScout" header and underline are always drawn by this
        method. The caller does not need to include a header in lines.

        lines:
            A list of up to 5 body text strings to show below the header.
            Missing lines are filled with blank strings.
        force:
            If True, refresh the display regardless of whether content
            changed or the refresh timer has elapsed.

        Returns:
            True if the display was refreshed.
            False if the update was skipped.
        """
        with self.lock:
            normalized_lines = normalize_display_lines(lines)
            protocol_label = str(protocol).upper().strip()[:4] if protocol else ""

            # Skip if body text and protocol are identical to what is on
            # screen.
            if not force and normalized_lines == self.last_lines and protocol_label == self._last_protocol:
                return False

            # VLAN or VOICE changes always trigger an immediate refresh,
            # bypassing the normal minimum refresh interval.
            if self._important_fields_changed(normalized_lines):
                force = True

            if not force and not self._refresh_allowed():
                return False

            self._ensure_awake()

            image = self._render_image(normalized_lines, protocol_label)
            buffer = self.epd.getbuffer(image)

            if self.partial_refresh_count >= self._partial_refresh_limit or not self._partial_base_ready:
                # Full refresh: clears ghosting and seeds both e-paper
                # frame buffers via display_Base. Both buffers
                # must be seeded before display_Partial can run without
                # ghosting.
                self.epd.Clear()
                self.epd.display_Base(buffer)
                self.partial_refresh_count = 0
                self._partial_base_ready = True
                log.debug("Full refresh performed (ghost prevention or first render)")
            else:
                # Partial refresh: faster, avoids full-screen flash.
                #
                # IMPORTANT: these bounds must be the panel's own native
                # width/height (self.epd.width/height -- 176x264,
                # portrait), NOT self.DISPLAY_WIDTH/HEIGHT (264x176,
                # landscape). getbuffer() auto-rotates a landscape-shaped
                # image into the native portrait buffer internally, so
                # the driver's row/column math for a partial window is
                # entirely in the native frame -- passing landscape
                # values here left ~1/3 of the screen (roughly the left
                # portion of every line, since text starts near the left
                # margin) permanently excluded from every partial update
                # after the first full refresh. No exception was ever
                # raised because the driver just silently skips whatever
                # falls outside the requested window.
                try:
                    self.epd.display_Partial(
                        buffer, 0, 0, self.epd.width, self.epd.height
                    )
                    self.partial_refresh_count += 1
                    log.debug(
                        "Partial refresh performed (%d/%d)",
                        self.partial_refresh_count,
                        self._partial_refresh_limit,
                    )
                except Exception:
                    # If partial refresh fails, fall back to a full refresh.
                    log.warning(
                        "Partial refresh failed. Falling back to full refresh.",
                        exc_info=True,
                    )
                    self.epd.Clear()
                    self.epd.display_Base(buffer)
                    self.partial_refresh_count = 0
                    self._partial_base_ready = True

            self.last_lines = normalized_lines
            self._last_protocol = protocol_label
            self.last_refresh_time = time.monotonic()

            if self.auto_sleep and not self.startup_mode:
                self.sleep()

            return True

    def force_refresh(self):
        """
        Refresh the current screen contents again.

        This redraws the same body lines and resets the partial refresh
        counter so the next call performs a full refresh.

        Returns:
            True if the display was redrawn.
            False if there was nothing to redraw.
        """
        with self.lock:
            if self.last_lines is None:
                return False
            # Reset the counter so the forced refresh is always a full
            # refresh.
            self.partial_refresh_count = self._partial_refresh_limit
            return self.show_lines(self.last_lines, force=True)

    def set_button_labels(self, labels):
        """
        Set or change the sidebar labels for KEY1-KEY4 (e.g.
        ["GW", "WAN", "DNS", "JIT"]). Pass None to remove the sidebar.

        Takes effect on the next show_lines()/force_refresh() call —
        this method does not redraw the screen itself.
        """
        with self.lock:
            self.button_labels = list(labels)[:4] if labels else None

    def get_status(self):
        """
        Return basic display status info.

        Useful for debugging.
        """
        with self.lock:
            return {
                "initialized": self.initialized,
                "sleeping": self.sleeping,
                "startup_mode": self.startup_mode,
                "last_lines": self.last_lines,
                "last_protocol": self._last_protocol,
                "last_refresh_time": self.last_refresh_time,
                "min_refresh_interval": self.min_refresh_interval,
                "auto_sleep": self.auto_sleep,
                "partial_refresh_count": self.partial_refresh_count,
                "partial_refresh_limit": self._partial_refresh_limit,
            }

    # ------------------------------------------------------------------ #
    # Private helpers                                                    #
    # ------------------------------------------------------------------ #

    def _build_font_cache(self):
        """
        Preload the body font sizes used by the display.

        Falls back to PIL's default font if the font file cannot be loaded.
        """
        cache = {}
        try:
            for size in range(self.MIN_FONT_SIZE, self.BASE_FONT_SIZE + 1):
                cache[size] = ImageFont.truetype(self.font_path, size)
        except OSError:
            log.warning(
                "Font file not found at '%s'. Using PIL default font.",
                self.font_path,
            )
            default_font = ImageFont.load_default()
            for size in range(self.MIN_FONT_SIZE, self.BASE_FONT_SIZE + 1):
                cache[size] = default_font
        return cache

    def _load_title_font(self):
        """
        Load the fixed font used for the header.

        Falls back to PIL's default font if the font file cannot be loaded.
        """
        try:
            return ImageFont.truetype(self.font_path, self.TITLE_FONT_SIZE)
        except OSError:
            return ImageFont.load_default()

    def _load_footer_font(self):
        """
        Load the small font used for the optional button-label sidebar.

        Falls back to PIL's default font if the font file cannot be loaded.
        """
        try:
            return ImageFont.truetype(self.font_path, self.FOOTER_FONT_SIZE)
        except OSError:
            return ImageFont.load_default()

    def _render_image(self, body_lines, protocol=""):
        """
        Turn the 6 normalized body lines into a display image.

        Always draws the fixed "PiScout" header with an underline, then
        renders each body line below it.

        If protocol is provided ("SNMP", "LLDP", or "CDP"), it is drawn in
        small text to the right of the title on the header row, giving the
        technician a quick visual cue about which method produced the
        current result.

        If button_labels is set, a "[LABEL]" is drawn beside each of body
        lines 2-5 (index 1-4 — IP/PORT/VLAN/VOICE on a result screen),
        vertically centered against that row, and a divider line is drawn
        below row 5 to set the LINK line apart — matching the physical
        button row below the panel.

        A vertical overflow warning is logged if content would exceed the
        panel height, so font size misconfigurations are visible in logs.
        """
        image = Image.new("1", (self.DISPLAY_WIDTH, self.DISPLAY_HEIGHT), 255)
        draw = ImageDraw.Draw(image)

        # Draw centered fixed-size header.
        header_text = self.TITLE_TEXT
        header_width = draw.textlength(header_text, font=self.title_font)
        header_x = int((self.DISPLAY_WIDTH - header_width) / 2)
        header_y = self.TOP_MARGIN
        draw.text((header_x, header_y), header_text, font=self.title_font, fill=0)

        # Draw protocol indicator (e.g. "SNMP", "LLDP", "CDP") at the left
        # side of the header row, small font so it doesn't compete with
        # the centered title.
        if protocol:
            proto_font = self.font_cache.get(self.MIN_FONT_SIZE, self.font_cache[self.MIN_FONT_SIZE])
            proto_text = str(protocol).upper()[:4]
            proto_x = self.LEFT_MARGIN
            draw.text((proto_x, header_y), proto_text, font=proto_font, fill=0)

        header_bbox = draw.textbbox(
            (header_x, header_y), header_text, font=self.title_font
        )
        underline_y = header_bbox[3] + self.TITLE_UNDERLINE_GAP
        draw.line(
            (header_bbox[0], underline_y, header_bbox[2], underline_y),
            fill=0,
            width=1,
        )

        # Compute the right-side label sidebar first (if any), so we know
        # how much width to leave for body text.
        sidebar_width = 0
        if self.button_labels:
            bracketed = [f"[{label}]" for label in self.button_labels if label]
            sidebar_width = max(
                (int(draw.textlength(text, font=self.footer_font)) for text in bracketed),
                default=0,
            )

        # Body starts below the underline. Reserve sidebar_width plus a
        # small gap on the right so body text never runs under the labels.
        y = underline_y + self.TITLE_BODY_GAP
        max_width = self.DISPLAY_WIDTH - (self.LEFT_MARGIN * 2) - sidebar_width
        if sidebar_width:
            max_width -= self.SIDEBAR_GAP

        # Tracks (y_top, font) per body line, or None if a line was
        # skipped due to overflow. Used to row-align sidebar labels
        # and the pre-LINK divider against the actual drawn positions.
        row_positions = []

        for line in body_lines:
            if y >= self.DISPLAY_HEIGHT:
                log.warning(
                    "Display overflow: y=%d >= DISPLAY_HEIGHT=%d. "
                    "'%s' and any remaining lines were not drawn. "
                    "Consider reducing font size.",
                    y,
                    self.DISPLAY_HEIGHT,
                    line,
                )
                break
            font = self._fit_font_for_line(draw, line, max_width)
            draw.text((self.LEFT_MARGIN, y), line, font=font, fill=0)
            row_positions.append((y, font))
            y += font.size + self.LINE_SPACING

        # Pad to match body_lines length so label/divider row indices
        # below still line up even if some trailing lines overflowed.
        row_positions += [None] * (len(body_lines) - len(row_positions))

        # Draw the button-label sidebar along the right edge, row-aligned
        # with body lines 2-5 (IP/PORT/VLAN/VOICE on a result screen), so
        # each label sits next to the row it's most related to, e.g.:
        #   IP: 192.168.1.82        [GW]
        #   PORT: Port 6            [WAN]
        #   VLAN: 1 (Native)        [DNS]
        #   VOICE:                  [JIT]
        # All labels share the same left edge (computed from the widest
        # one, sidebar_width) rather than each right-aligning individually
        # — keeps the column looking uniform instead of staggered.
        # A divider is drawn below row 5, separating VOICE/labels from
        # the LINK line the same way the header underline separates the
        # title from the body. This layout is driven purely by row
        # position (indices 1-4 of body_lines), not by row content, so
        # it stays identical across every screen (waiting/scanning/
        # result/button-result) — the labels never jump around.
        if self.button_labels and len(row_positions) >= 6:
            label_x = self.DISPLAY_WIDTH - self.SIDEBAR_RIGHT_MARGIN - sidebar_width
            label_rows = row_positions[1:5]
            for text, row in zip(bracketed, label_rows):
                if row is None:
                    continue
                row_y, row_font = row
                label_y = row_y + (row_font.size - self.FOOTER_FONT_SIZE) // 2
                draw.text((label_x, label_y), text, font=self.footer_font, fill=0)

            row4, row5 = row_positions[4], row_positions[5]
            if row4 is not None and row5 is not None:
                row4_bottom = row4[0] + row4[1].size
                divider_y = (row4_bottom + row5[0]) // 2
                divider_right = self.DISPLAY_WIDTH - self.LEFT_MARGIN
                draw.line(
                    (self.LEFT_MARGIN, divider_y, divider_right, divider_y),
                    fill=0,
                    width=1,
                )
        elif self.button_labels:
            # Fallback for unexpectedly short body_lines: evenly space
            # the labels instead of row-aligning them. Same consistent
            # left-edge alignment as the row-aligned block above.
            n = len(bracketed)
            region_top = underline_y + self.TITLE_BODY_GAP
            region_bottom = self.DISPLAY_HEIGHT - self.SIDEBAR_BOTTOM_MARGIN
            region_height = region_bottom - region_top
            gap = (region_height - n * self.FOOTER_FONT_SIZE) / (n + 1)
            label_x = self.DISPLAY_WIDTH - self.SIDEBAR_RIGHT_MARGIN - sidebar_width
            label_y = region_top + gap
            for text in bracketed:
                draw.text((label_x, int(label_y)), text, font=self.footer_font, fill=0)
                label_y += self.FOOTER_FONT_SIZE + gap

        # Rotate so that the Ethernet port is at the top of the physical
        # device.
        return image.rotate(180)

    def _fit_font_for_line(self, draw, text, max_width):
        """
        Pick the largest body font size that fits within max_width for one
        line.
        """
        for size in range(self.BASE_FONT_SIZE, self.MIN_FONT_SIZE - 1, -1):
            font = self.font_cache[size]
            if draw.textlength(text, font=font) <= max_width:
                return font
        return self.font_cache[self.MIN_FONT_SIZE]

    def _important_fields_changed(self, new_lines):
        """
        Check whether VLAN (index 3), VOICE (index 4), or LINK (index 5)
        changed.

        These fields trigger an immediate refresh, bypassing the normal
        minimum refresh interval, so the technician sees changes right
        away.
        """
        if self.last_lines is None:
            return True
        return (
            self.last_lines[3] != new_lines[3]
            or self.last_lines[4] != new_lines[4]
            or self.last_lines[5] != new_lines[5]
        )

    def _refresh_allowed(self):
        """
        Check whether enough time has passed since the last refresh.
        """
        elapsed = time.monotonic() - self.last_refresh_time
        return elapsed >= self.min_refresh_interval

    def _ensure_awake(self):
        """
        Make sure the display is ready to receive a new image.

        If the display was never started or was sleeping, call init() to
        wake it. After any hardware init, the partial refresh counter is
        reset to the limit so the next display call performs a full
        refresh.
        """
        if not self.initialized:
            self.epd.init()
            self.initialized = True
            self.sleeping = False
            self.partial_refresh_count = self._partial_refresh_limit
            return
        if self.sleeping:
            self.epd.init()
            self.sleeping = False
            self.partial_refresh_count = self._partial_refresh_limit
