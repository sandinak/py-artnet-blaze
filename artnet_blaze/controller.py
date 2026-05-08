"""Test-pattern controller.

Holds an active `Override` object (or None) that, when set, replaces
the live ArtNet snapshot on every sink for a window of time. The
controller doesn't know what the override paints — it just routes the
override object to sinks and applies the activity-aware expiry rule.

Override clearing rules:
  * If ArtNet is currently active, override is held for at least
    `min_hold_s` seconds, then yields back to live ArtNet.
  * If ArtNet is not active when the override is set, it stays in place
    until ArtNet starts arriving (then the min_hold_s grace applies).
  * The user can clear it explicitly via the API at any time.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Optional

from .overrides import Override, UniformByteOverride, make_uniform

if TYPE_CHECKING:
    from .artnet import ArtNetReceiver


class TestController:
    # Tell pytest not to collect this class — the `Test` prefix names a
    # rig test-pattern controller, not a unit-test class.
    __test__ = False

    def __init__(
        self,
        receiver: "ArtNetReceiver",
        min_hold_s: float = 5.0,
        dmx_active_window_s: float = 1.0,
        clock=time.monotonic,
    ) -> None:
        self._lock = threading.Lock()
        self._override: Optional[Override] = None
        self._set_at: float = 0.0
        self._receiver = receiver
        self._min_hold_s = min_hold_s
        self._dmx_active_window_s = dmx_active_window_s
        self._clock = clock

    # ── Setters ──────────────────────────────────────────────

    def set_value(self, value: int) -> None:
        """Convenience: install a UniformByteOverride."""
        self.set_override(make_uniform(value))

    def set_override(self, override: Override) -> None:
        with self._lock:
            self._override = override
            self._set_at = self._clock()

    def clear(self) -> None:
        with self._lock:
            self._override = None

    # ── Getters ──────────────────────────────────────────────

    def current(self) -> Optional[Override]:
        """Return active override, or None if expired/cleared.

        Side effect: clears an override that has aged out with active
        ArtNet, so the result reflects what the next sink tick will use.
        """
        with self._lock:
            if self._override is None:
                return None
            elapsed = self._clock() - self._set_at
            if elapsed >= self._min_hold_s and self._dmx_active_locked():
                self._override = None
                return None
            return self._override

    def state(self) -> dict:
        with self._lock:
            if self._override is None:
                return {"active": False}
            elapsed = self._clock() - self._set_at
            remaining = max(0.0, self._min_hold_s - elapsed)
            payload: dict = {
                "active": True,
                "kind": self._override.kind,
                "elapsed_s": round(elapsed, 2),
                "min_hold_s": self._min_hold_s,
                "min_hold_remaining_s": round(remaining, 2),
            }
            payload.update(self._override.info())
            return payload

    def dmx_active(self) -> dict[int, bool]:
        """Per-universe activity flag (True if a packet arrived recently)."""
        now = self._clock()
        win = self._dmx_active_window_s
        with self._receiver.lock:
            return {
                u: bool(buf.last_seen) and (now - buf.last_seen) < win
                for u, buf in self._receiver.buffers.items()
            }

    # ── internal ───────────────────────────────────────────────

    def _dmx_active_locked(self) -> bool:
        # Caller must already hold self._lock. Receiver lock is independent.
        now = self._clock()
        win = self._dmx_active_window_s
        with self._receiver.lock:
            for buf in self._receiver.buffers.values():
                if buf.last_seen and (now - buf.last_seen) < win:
                    return True
        return False
