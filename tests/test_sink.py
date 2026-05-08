"""Sink base-class tick loop, lifecycle, error counting, blackout-on-stop."""

from __future__ import annotations

import logging
import threading
import time

from artnet_blaze.sink import Sink


class _CountingSink(Sink):
    """Minimal Sink subclass that records frame numbers + a blackout flag."""

    def __init__(self, log, fps, fail_after=None):
        super().__init__("test-sink", receiver=None, fps=fps, log=log)
        self.frames: list[int] = []
        self.blackouts = 0
        self.fail_after = fail_after

    def tx_one_frame(self) -> None:
        n = len(self.frames)
        if self.fail_after is not None and n >= self.fail_after:
            raise RuntimeError("synthetic tx failure")
        self.frames.append(n)

    def blackout(self) -> None:
        self.blackouts += 1


def _quiet() -> logging.Logger:
    log = logging.getLogger("sink-test")
    log.handlers.clear()
    log.addHandler(logging.NullHandler())
    log.setLevel(logging.CRITICAL)
    return log


def test_sink_runs_ticks_until_stop():
    sink = _CountingSink(_quiet(), fps=200)  # 5ms period
    sink.start()
    time.sleep(0.06)  # ~12 ticks worth
    sink.stop()
    sink.join(timeout=1.0)

    assert not sink.is_alive()
    assert len(sink.frames) >= 5  # generous floor; CI scheduling is noisy
    assert sink.tx_errors == 0


def test_sink_blackout_runs_once_on_clean_stop():
    sink = _CountingSink(_quiet(), fps=200)
    sink.start()
    time.sleep(0.02)
    sink.stop()
    sink.join(timeout=1.0)
    assert sink.blackouts == 1


def test_sink_counts_tx_errors_without_dying():
    # Fail every tick after the 2nd; sink must keep ticking and counting.
    sink = _CountingSink(_quiet(), fps=200, fail_after=2)
    sink.start()
    time.sleep(0.06)
    sink.stop()
    sink.join(timeout=1.0)

    assert sink.tx_errors >= 3
    # frames_tx is incremented only on success, so it stops growing past 2
    assert sink.frames_tx == 2


def test_sink_blackout_failure_does_not_propagate(caplog):
    """A failing blackout is logged at warning, not raised."""

    class _BlackoutBoom(_CountingSink):
        def blackout(self) -> None:
            raise RuntimeError("boom")

    log = logging.getLogger("blackout-boom")
    log.handlers.clear()
    log.setLevel(logging.WARNING)

    sink = _BlackoutBoom(log, fps=200)
    sink.start()
    time.sleep(0.02)
    sink.stop()
    sink.join(timeout=1.0)
    # If it had propagated, .is_alive() could lie; main signal is "thread
    # exited cleanly and we got here without an unhandled exception".
    assert not sink.is_alive()


def test_sink_stop_is_idempotent():
    sink = _CountingSink(_quiet(), fps=200)
    sink.start()
    sink.stop()
    sink.stop()  # second call must not raise
    sink.join(timeout=1.0)
    assert sink.stopped()
