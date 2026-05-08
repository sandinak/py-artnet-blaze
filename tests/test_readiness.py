"""Readiness predicate: per-check evaluation + state resolution."""

from __future__ import annotations

import logging
import time
import types
from unittest.mock import patch

import pytest

from artnet_blaze.artnet import ArtNetReceiver
from artnet_blaze.readiness import evaluate_readiness
from artnet_blaze.status_led import Readiness


def _quiet() -> logging.Logger:
    log = logging.getLogger("readiness-test")
    log.handlers.clear()
    log.addHandler(logging.NullHandler())
    log.setLevel(logging.CRITICAL)
    return log


def _fake_app(*, dmx=False, poe_open=True, dmx_open=True, recent_artnet=False):
    """Build the smallest object that quacks like an `App` for the
    readiness evaluator. Only the attributes the predicate touches."""
    rx = ArtNetReceiver("127.0.0.1", {0, 1}, _quiet())
    if recent_artnet:
        rx.buffers[0].last_seen = time.monotonic()

    poe_port = types.SimpleNamespace(is_open=poe_open)
    dmx_port = types.SimpleNamespace(is_open=dmx_open) if dmx else None
    dmx_sink = object() if dmx else None

    return types.SimpleNamespace(
        rx=rx, poe_port=poe_port, dmx_port=dmx_port, dmx_sink=dmx_sink,
    )


def test_ready_when_everything_passes():
    app = _fake_app(dmx=True, recent_artnet=True)
    with patch("artnet_blaze.readiness.has_network_ip", return_value=True):
        report = evaluate_readiness(app)
    assert report.state == Readiness.READY
    assert report.checks["network"] is True
    assert report.checks["poe_port_open"] is True
    assert report.checks["dmx_port_open"] is True
    assert report.checks["artnet_active"] is True


def test_waiting_artnet_when_only_artnet_missing():
    app = _fake_app(dmx=True, recent_artnet=False)
    with patch("artnet_blaze.readiness.has_network_ip", return_value=True):
        report = evaluate_readiness(app)
    assert report.state == Readiness.WAITING_ARTNET
    assert report.checks["artnet_active"] is False


def test_fault_when_no_network():
    app = _fake_app(dmx=False, recent_artnet=True)
    with patch("artnet_blaze.readiness.has_network_ip", return_value=False):
        report = evaluate_readiness(app)
    assert report.state == Readiness.FAULT
    assert report.checks["network"] is False


def test_fault_when_poe_port_closed():
    app = _fake_app(dmx=False, poe_open=False, recent_artnet=True)
    with patch("artnet_blaze.readiness.has_network_ip", return_value=True):
        report = evaluate_readiness(app)
    assert report.state == Readiness.FAULT
    assert report.checks["poe_port_open"] is False


def test_fault_when_dmx_configured_but_port_closed():
    app = _fake_app(dmx=True, dmx_open=False, recent_artnet=True)
    with patch("artnet_blaze.readiness.has_network_ip", return_value=True):
        report = evaluate_readiness(app)
    assert report.state == Readiness.FAULT
    assert report.checks["dmx_port_open"] is False


def test_dmx_check_is_none_when_dmx_not_configured():
    app = _fake_app(dmx=False, recent_artnet=True)
    with patch("artnet_blaze.readiness.has_network_ip", return_value=True):
        report = evaluate_readiness(app)
    assert report.checks["dmx_port_open"] is None
    assert report.state == Readiness.READY


def test_artnet_window_respected():
    """A packet received outside the window doesn't count as active."""
    app = _fake_app(dmx=False)
    # Mark a packet seen "long ago" (10s before now).
    app.rx.buffers[0].last_seen = time.monotonic() - 10.0
    with patch("artnet_blaze.readiness.has_network_ip", return_value=True):
        report = evaluate_readiness(app, artnet_active_window_s=2.0)
    assert report.state == Readiness.WAITING_ARTNET
    assert report.checks["artnet_active"] is False


def test_port_open_treats_missing_attr_as_open():
    """FakeSerial-style ports without `is_open` are assumed alive."""
    from artnet_blaze.readiness import _port_open
    fake = object()  # no is_open attribute
    assert _port_open(fake) is True


def test_port_open_treats_none_as_closed():
    from artnet_blaze.readiness import _port_open
    assert _port_open(None) is False
