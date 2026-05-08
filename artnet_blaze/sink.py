"""Sink base class.

A Sink is an output target driven by a fixed-tick loop running on its own
thread. Each sink subscribes to the shared ArtNetReceiver and converts
universe snapshots into wire packets at its own configured FPS.

Subclasses implement `tx_one_frame()` (called every tick) and `blackout()`
(called once on clean shutdown). Lifecycle, pacing, and error counters are
handled here so subclasses stay focused on protocol.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .artnet import ArtNetReceiver


class Sink(threading.Thread):
    """Fixed-tick output thread."""

    def __init__(
        self,
        name: str,
        receiver: "ArtNetReceiver",
        fps: float,
        log: logging.Logger,
    ) -> None:
        super().__init__(daemon=True, name=name)
        self.receiver = receiver
        self.period = 1.0 / fps
        self.log = log
        self._stop = threading.Event()
        self.frames_tx = 0
        self.tx_errors = 0
        self.late_ticks = 0

    def stop(self) -> None:
        self._stop.set()

    def stopped(self) -> bool:
        return self._stop.is_set()

    def run(self) -> None:
        next_tick = time.monotonic()
        while not self._stop.is_set():
            next_tick += self.period
            try:
                self.tx_one_frame()
                self.frames_tx += 1
            except Exception as e:
                self.tx_errors += 1
                self.log.error("%s tx failed: %s", self.name, e)
            sleep = next_tick - time.monotonic()
            if sleep > 0:
                time.sleep(sleep)
            elif sleep < -self.period:
                self.late_ticks += 1
                if self.late_ticks % 10 == 1:
                    self.log.warning(
                        "%s loop behind by %.1fms, resyncing",
                        self.name, -sleep * 1000,
                    )
                next_tick = time.monotonic()
        try:
            self.blackout()
        except Exception as e:
            self.log.warning("%s blackout failed: %s", self.name, e)

    def tx_one_frame(self) -> None:
        raise NotImplementedError

    def blackout(self) -> None:
        raise NotImplementedError
