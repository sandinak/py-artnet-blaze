"""Readiness predicate.

Inspects the running App and returns a `ReadinessReport`: a single
state for the LED to display plus a per-check breakdown for the HTTP
panel and `journalctl` diagnostics. Kept separate from `status_led` so
the predicate can be evaluated without GPIO dependencies.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from .status_led import Readiness
from .sysinfo import has_network_ip

if TYPE_CHECKING:
    from .main import App


@dataclass
class ReadinessReport:
    """Resolved readiness state plus the per-check breakdown.

    `checks` keys map to bool, or None when the check doesn't apply
    (e.g. `dmx_port_open` is None when DMX isn't configured).
    """
    state: Readiness
    checks: dict[str, Optional[bool]]


def _port_open(port) -> bool:
    """True if a pyserial-like port is open. Treats ports without an
    `is_open` attribute (FakeSerial in tests) as open — they're alive
    by virtue of having been constructed."""
    if port is None:
        return False
    is_open = getattr(port, "is_open", None)
    if is_open is None:
        return True
    return bool(is_open)


def _artnet_recently_active(receiver, window_s: float) -> bool:
    now = time.monotonic()
    with receiver.lock:
        for buf in receiver.buffers.values():
            if buf.last_seen and (now - buf.last_seen) < window_s:
                return True
    return False


def evaluate_readiness(
    app: "App",
    artnet_active_window_s: float = 2.0,
) -> ReadinessReport:
    """Compute the current readiness state.

    Decision tree:
      * Any failed device or network check → FAULT (red).
      * All checks pass except ArtNet flowing → WAITING_ARTNET (amber).
      * Everything green → READY (green).
    """
    checks: dict[str, Optional[bool]] = {
        "network": has_network_ip(),
        "poe_port_open": _port_open(app.poe_port),
        "dmx_port_open": (
            _port_open(app.dmx_port) if app.dmx_sink is not None else None
        ),
        "artnet_active": _artnet_recently_active(
            app.rx, artnet_active_window_s
        ),
    }

    if not checks["network"]:
        state = Readiness.FAULT
    elif not checks["poe_port_open"]:
        state = Readiness.FAULT
    elif checks["dmx_port_open"] is False:
        state = Readiness.FAULT
    elif not checks["artnet_active"]:
        state = Readiness.WAITING_ARTNET
    else:
        state = Readiness.READY

    return ReadinessReport(state=state, checks=checks)
