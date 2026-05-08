"""RGB status LED driver.

Drives a 3-pin RGB LED to signal daemon readiness. Three colors:

    🟢 green  — READY: network up, all configured devices alive,
                ArtNet is currently flowing.
    🟡 amber  — WAITING_ARTNET: daemon is healthy, devices ready,
                still waiting for the show controller to start sending.
    🔴 red    — FAULT: missing network, a configured port isn't open,
                or the readiness evaluator itself errored.

Two backends:
  * gpiozero (preinstalled on Raspberry Pi OS — covers Pi 3/4/5).
  * noop (logs only) for dev machines, CI, or any environment where
    `gpiozero` isn't importable.

The driver thread polls a readiness predicate every `poll_interval_s`
and applies a state change only after `debounce_ticks` consecutive
ticks return the same state. That keeps a 1-frame ArtNet stutter from
flickering the LED. `force_color()` is provided for the HTTP test panel
to verify wiring at install time without waiting for ArtNet.
"""

from __future__ import annotations

import logging
import threading
import time
from enum import Enum
from typing import Callable, Optional, Protocol

try:
    from gpiozero import RGBLED  # type: ignore
    _HAS_GPIOZERO = True
except ImportError:
    RGBLED = None  # type: ignore
    _HAS_GPIOZERO = False


# ── State + colors ───────────────────────────────────────────────


class Readiness(str, Enum):
    """Daemon readiness as displayed by the status LED."""
    READY = "ready"
    WAITING_ARTNET = "waiting_artnet"
    FAULT = "fault"


# RGB tuples in 0..1 (gpiozero convention).
COLOR_FOR_STATE: dict[Readiness, tuple[float, float, float]] = {
    Readiness.READY:           (0.0, 1.0, 0.0),   # green
    Readiness.WAITING_ARTNET:  (1.0, 0.4, 0.0),   # amber
    Readiness.FAULT:           (1.0, 0.0, 0.0),   # red
}

# Named test colors exposed via `/test/led/{name}`. Same set as states
# plus "off" for clearing. Kept in one place so the HTTP route and the
# evaluator agree on what "amber" looks like.
TEST_COLORS: dict[str, tuple[float, float, float]] = {
    "red":   (1.0, 0.0, 0.0),
    "green": (0.0, 1.0, 0.0),
    "amber": (1.0, 0.4, 0.0),
    "blue":  (0.0, 0.0, 1.0),
    "white": (1.0, 1.0, 1.0),
    "off":   (0.0, 0.0, 0.0),
}


# ── Backends ─────────────────────────────────────────────────────


class _LedBackend(Protocol):
    def set_color(self, r: float, g: float, b: float) -> None: ...
    def off(self) -> None: ...
    def close(self) -> None: ...


class _NoopLed:
    """Records state changes via the logger; used off-Pi and in tests."""

    def __init__(self, log: logging.Logger) -> None:
        self.log = log
        self._color: tuple[float, float, float] = (0.0, 0.0, 0.0)

    def set_color(self, r: float, g: float, b: float) -> None:
        self._color = (r, g, b)
        self.log.debug("status-led (noop) → (%.2f, %.2f, %.2f)", r, g, b)

    def off(self) -> None:
        self.set_color(0.0, 0.0, 0.0)

    def close(self) -> None:
        pass

    @property
    def color(self) -> tuple[float, float, float]:
        return self._color


class _GpiozeroLed:
    """gpiozero RGBLED wrapper. Common-cathode by default."""

    def __init__(
        self,
        red_pin: int,
        green_pin: int,
        blue_pin: int,
        common_anode: bool = False,
    ) -> None:
        if not _HAS_GPIOZERO:
            raise RuntimeError("gpiozero not available")
        self._led = RGBLED(
            red=red_pin, green=green_pin, blue=blue_pin,
            active_high=not common_anode,
        )

    def set_color(self, r: float, g: float, b: float) -> None:
        self._led.color = (r, g, b)

    def off(self) -> None:
        self._led.off()

    def close(self) -> None:
        self._led.close()


def make_led(
    red_pin: int,
    green_pin: int,
    blue_pin: int,
    common_anode: bool,
    log: logging.Logger,
) -> _LedBackend:
    """Return a gpiozero-backed LED if available, else a noop fallback."""
    if not _HAS_GPIOZERO:
        log.warning("gpiozero not installed; status LED runs in noop mode")
        return _NoopLed(log)
    try:
        return _GpiozeroLed(red_pin, green_pin, blue_pin, common_anode)
    except Exception as e:
        log.warning("status LED hardware init failed (%s); using noop", e)
        return _NoopLed(log)


# ── Driver thread ────────────────────────────────────────────────


class StatusLedThread(threading.Thread):
    """Polls a readiness predicate, drives the LED, debounces transitions."""

    def __init__(
        self,
        led: _LedBackend,
        evaluator: Callable[[], Readiness],
        log: logging.Logger,
        poll_interval_s: float = 0.5,
        debounce_ticks: int = 2,
        clock=time.monotonic,
    ) -> None:
        super().__init__(daemon=True, name="status-led")
        self.led = led
        self.evaluator = evaluator
        self.log = log
        self.poll_interval_s = poll_interval_s
        self.debounce_ticks = debounce_ticks
        self._clock = clock
        self._stop = threading.Event()
        self._applied: Optional[Readiness] = None
        self._pending: Optional[Readiness] = None
        self._pending_count = 0
        self._force_until: float = 0.0

    @property
    def applied_state(self) -> Optional[Readiness]:
        return self._applied

    def stop(self) -> None:
        self._stop.set()

    def force_color(
        self,
        rgb: tuple[float, float, float],
        duration_s: float = 5.0,
    ) -> None:
        """Override the LED for `duration_s` seconds.

        Useful from the HTTP test panel to verify wiring without
        waiting for ArtNet. Auto-update resumes when the timer expires.
        """
        self.led.set_color(*rgb)
        self._force_until = self._clock() + duration_s

    def run(self) -> None:
        try:
            while not self._stop.is_set():
                if self._clock() < self._force_until:
                    self._stop.wait(self.poll_interval_s)
                    continue
                try:
                    state = self.evaluator()
                except Exception as e:
                    self.log.error("readiness evaluator failed: %s", e)
                    state = Readiness.FAULT
                self._observe(state)
                self._stop.wait(self.poll_interval_s)
        finally:
            try:
                self.led.off()
            finally:
                self.led.close()

    # ── internals ──────────────────────────────────────────────

    def _observe(self, state: Readiness) -> None:
        """Apply state if it has been stable for `debounce_ticks` ticks."""
        if state != self._pending:
            self._pending = state
            self._pending_count = 1
        else:
            self._pending_count += 1
        if (self._pending_count >= self.debounce_ticks
                and self._pending != self._applied):
            color = COLOR_FOR_STATE.get(self._pending, (0.0, 0.0, 0.0))
            self.led.set_color(*color)
            self._applied = self._pending
            self.log.info("status: %s", self._pending.value)
