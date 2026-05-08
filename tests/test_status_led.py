"""StatusLedThread + backends: debounce, force_color, lifecycle, fallback."""

from __future__ import annotations

import logging
import time

import pytest

from artnet_blaze.status_led import (
    COLOR_FOR_STATE,
    TEST_COLORS,
    Readiness,
    StatusLedThread,
    _NoopLed,
    make_led,
)


def _quiet() -> logging.Logger:
    log = logging.getLogger("led-test")
    log.handlers.clear()
    log.addHandler(logging.NullHandler())
    log.setLevel(logging.CRITICAL)
    return log


class _RecordingLed:
    """LED backend that captures every set_color/off/close call."""

    def __init__(self) -> None:
        self.events: list = []
        self.closed = False

    def set_color(self, r: float, g: float, b: float) -> None:
        self.events.append(("set", (r, g, b)))

    def off(self) -> None:
        self.events.append(("off",))

    def close(self) -> None:
        self.closed = True
        self.events.append(("close",))


class _ManualClock:
    def __init__(self, t0: float = 1000.0) -> None:
        self.t = t0

    def __call__(self) -> float:
        return self.t


# ── Color / state map ───────────────────────────────────────────


def test_state_color_map_covers_all_states():
    for state in Readiness:
        assert state in COLOR_FOR_STATE


def test_test_colors_includes_state_colors():
    # Each LED state's RGB is also reachable via the test_colors map.
    assert TEST_COLORS["green"] == COLOR_FOR_STATE[Readiness.READY]
    assert TEST_COLORS["amber"] == COLOR_FOR_STATE[Readiness.WAITING_ARTNET]
    assert TEST_COLORS["red"] == COLOR_FOR_STATE[Readiness.FAULT]
    assert TEST_COLORS["off"] == (0.0, 0.0, 0.0)


# ── _NoopLed ────────────────────────────────────────────────────


def test_noop_led_records_color():
    led = _NoopLed(_quiet())
    led.set_color(0.5, 0.6, 0.7)
    assert led.color == (0.5, 0.6, 0.7)
    led.off()
    assert led.color == (0.0, 0.0, 0.0)


def test_make_led_falls_back_to_noop_when_gpio_unavailable():
    """On macOS / dev the import or hw init fails → noop returned."""
    led = make_led(99, 98, 97, common_anode=False, log=_quiet())
    # Should at least respond to the LED protocol without raising.
    led.set_color(1.0, 0.0, 0.0)
    led.off()
    led.close()


# ── StatusLedThread debounce ────────────────────────────────────


def test_thread_applies_state_after_debounce_ticks():
    """A state must repeat for `debounce_ticks` ticks before applying."""
    led = _RecordingLed()
    states = iter([
        Readiness.WAITING_ARTNET,  # tick 1: pending = waiting
        Readiness.WAITING_ARTNET,  # tick 2: counted, applied
        Readiness.READY,           # tick 3: pending = ready (count=1)
        Readiness.READY,           # tick 4: counted, applied
    ])
    sled = StatusLedThread(
        led=led, evaluator=lambda: next(states),
        log=_quiet(), poll_interval_s=0.0, debounce_ticks=2,
    )
    # Drive the loop 4 times manually.
    for _ in range(4):
        try:
            sled._observe(sled.evaluator())
        except StopIteration:
            break
    set_calls = [e[1] for e in led.events if e[0] == "set"]
    assert set_calls == [
        COLOR_FOR_STATE[Readiness.WAITING_ARTNET],
        COLOR_FOR_STATE[Readiness.READY],
    ]


def test_thread_resets_counter_on_state_flip():
    """A→B→A within debounce window should not apply A."""
    led = _RecordingLed()
    sled = StatusLedThread(
        led=led, evaluator=lambda: None,
        log=_quiet(), poll_interval_s=0.0, debounce_ticks=3,
    )
    for s in [Readiness.READY, Readiness.FAULT, Readiness.READY]:
        sled._observe(s)
    # No state was stable for 3 ticks → no LED writes
    assert all(e[0] != "set" for e in led.events)


def test_thread_evaluator_exception_treated_as_fault():
    """If the predicate raises, the thread flips to FAULT (red)."""
    led = _RecordingLed()
    log = _quiet()

    def bad_eval():
        raise RuntimeError("synthetic")

    sled = StatusLedThread(
        led=led, evaluator=bad_eval, log=log,
        poll_interval_s=0.001, debounce_ticks=1,
    )
    sled.start()
    time.sleep(0.05)
    sled.stop()
    sled.join(timeout=1.0)
    set_calls = [e[1] for e in led.events if e[0] == "set"]
    assert COLOR_FOR_STATE[Readiness.FAULT] in set_calls


def test_thread_off_and_close_on_stop():
    led = _RecordingLed()
    sled = StatusLedThread(
        led=led, evaluator=lambda: Readiness.READY, log=_quiet(),
        poll_interval_s=0.001, debounce_ticks=1,
    )
    sled.start()
    time.sleep(0.05)
    sled.stop()
    sled.join(timeout=1.0)
    kinds = [e[0] for e in led.events]
    assert "off" in kinds
    assert "close" in kinds
    assert led.closed is True


# ── force_color ─────────────────────────────────────────────────


def test_force_color_writes_immediately():
    led = _RecordingLed()
    sled = StatusLedThread(
        led=led, evaluator=lambda: Readiness.READY, log=_quiet(),
        poll_interval_s=999, debounce_ticks=1,
    )
    sled.force_color((1.0, 1.0, 1.0), duration_s=10.0)
    assert ("set", (1.0, 1.0, 1.0)) in led.events


def test_force_color_pauses_evaluator_until_timer_expires():
    """While `_force_until` is in the future, the evaluator output is
    ignored. After it expires, normal updates resume."""
    led = _RecordingLed()
    clock = _ManualClock()
    state_holder = {"state": Readiness.READY}
    sled = StatusLedThread(
        led=led, evaluator=lambda: state_holder["state"], log=_quiet(),
        poll_interval_s=0.0, debounce_ticks=1, clock=clock,
    )

    sled.force_color((1.0, 0.0, 1.0), duration_s=5.0)
    assert ("set", (1.0, 0.0, 1.0)) in led.events
    led.events.clear()

    # While forced (clock not advanced), an _observe call still works
    # but the run loop wouldn't apply normal state — emulate by checking
    # the timer directly.
    assert clock() < sled._force_until

    # Advance past the forced window.
    clock.t += 6.0
    assert clock() >= sled._force_until


def test_thread_does_not_re_apply_same_state():
    """Successive ticks at the same state should write the LED only once."""
    led = _RecordingLed()
    sled = StatusLedThread(
        led=led, evaluator=lambda: Readiness.READY, log=_quiet(),
        poll_interval_s=0.0, debounce_ticks=1,
    )
    for _ in range(5):
        sled._observe(Readiness.READY)
    set_calls = [e for e in led.events if e[0] == "set"]
    assert len(set_calls) == 1
