"""
buttons.py

Reads the 4 onboard push-buttons on the Waveshare 2.7" e-Paper HAT V2.

Hardware wiring (from the Waveshare schematic for this HAT): each
button ties its GPIO line to ground when pressed, with the other side
of the switch on GND. So all four pins are configured as inputs with
the internal pull-up enabled, and a press is a falling edge.

    KEY1 -> BCM GPIO5
    KEY2 -> BCM GPIO6
    KEY3 -> BCM GPIO13
    KEY4 -> BCM GPIO19

What this file does:
  - Set up the 4 button GPIOs
  - Debounce presses in hardware via RPi.GPIO's edge detection
  - Run each button's callback on its own thread, so a slow callback
    (e.g. one that pings the network and redraws the display) never
    blocks detection of the other 3 buttons
  - Ignore a press on a button whose previous press is still running,
    rather than starting a second overlapping thread for it

What this file does NOT do:
  - Decide what a button press should do (that's main.py's job — it
    passes in the callbacks)
  - Touch the display directly
"""

import logging
import threading

import RPi.GPIO as GPIO

log = logging.getLogger(__name__)

# BCM pin numbers for each button, per the 2.7" HAT V2 schematic.
KEY_PINS = {
    "KEY1": 5,
    "KEY2": 6,
    "KEY3": 13,
    "KEY4": 19,
}

# Minimum milliseconds between accepted presses on the same button.
# Handled by RPi.GPIO's built-in debounce.
DEBOUNCE_MS = 300


class Buttons:
    def __init__(self, callbacks=None):
        """
        callbacks:
            Optional dict mapping "KEY1".."KEY4" to a zero-argument
            callable. Keys not present in the dict are simply left
            unwired — pressing that button does nothing. Callbacks run
            in their own daemon thread, one per press, so a callback
            that blocks (network I/O, display refresh, sleep) never
            stalls detection of other buttons.

            If a button is pressed again while its own previous press
            is still running its callback, the new press is ignored
            (logged, not queued or stacked) rather than starting a
            second thread that would run concurrently with the first.
            Two overlapping runs of the same handler would otherwise
            race on the display — each capturing/restoring "what was
            on screen before" at a different moment — and can leave
            the screen stuck showing a stale result. Handler runtimes
            here are a few seconds at most (button-result display
            delay plus one network round trip), so this is a short
            wait for the case where a button is genuinely double-
            pressed, not a real limitation.
        """
        self._callbacks = dict(callbacks or {})
        self._started = False
        self._running = set()
        self._running_lock = threading.Lock()

    def start(self):
        """Arm GPIO edge detection for every button that has a callback."""
        if self._started:
            return
        GPIO.setmode(GPIO.BCM)
        for name, pin in KEY_PINS.items():
            GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            if name in self._callbacks:
                GPIO.add_event_detect(
                    pin,
                    GPIO.FALLING,
                    callback=self._make_handler(name),
                    bouncetime=DEBOUNCE_MS,
                )
                log.info("%s (GPIO%d) armed", name, pin)
        self._started = True

    def _make_handler(self, name):
        callback = self._callbacks[name]

        def _handler(_channel):
            with self._running_lock:
                if name in self._running:
                    log.debug("%s pressed, but still handling a previous press — ignoring", name)
                    return
                self._running.add(name)

            log.debug("%s pressed", name)
            threading.Thread(
                target=self._run_safely,
                args=(name, callback),
                name=f"button-{name}",
                daemon=True,
            ).start()

        return _handler

    def _run_safely(self, name, callback):
        try:
            callback()
        except Exception:
            log.exception("Button %s callback raised", name)
        finally:
            with self._running_lock:
                self._running.discard(name)

    def cleanup(self):
        """Remove GPIO edge detection. Safe to call even if never started."""
        if not self._started:
            return
        for pin in KEY_PINS.values():
            try:
                GPIO.remove_event_detect(pin)
            except Exception:
                pass
        self._started = False
        with self._running_lock:
            self._running.clear()
