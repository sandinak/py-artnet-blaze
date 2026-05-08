"""TestController override semantics + sink integration."""

from __future__ import annotations

import logging

import pytest

from artnet_blaze.artnet import ArtNetReceiver
from artnet_blaze.controller import TestController
from artnet_blaze.dmx import (
    DMX_SLOT_COUNT,
    PROTOCOL_ENTTEC_PRO,
    DmxFixture,
    DmxSink,
)
from artnet_blaze.poe import POE_MAGIC, PoeSink, StripMapping


class _ManualClock:
    """Simple monotonic clock fake — advance via .tick(seconds)."""
    def __init__(self, t0: float = 1000.0) -> None:
        self.t = t0
    def __call__(self) -> float:
        return self.t
    def tick(self, secs: float) -> None:
        self.t += secs


def _quiet():
    log = logging.getLogger("ctrl-test")
    log.handlers.clear()
    log.addHandler(logging.NullHandler())
    log.setLevel(logging.CRITICAL)
    return log


def test_set_value_validates_range():
    rx = ArtNetReceiver("127.0.0.1", {0}, _quiet())
    c = TestController(rx)
    with pytest.raises(ValueError):
        c.set_value(-1)
    with pytest.raises(ValueError):
        c.set_value(256)


def test_clear_returns_none():
    rx = ArtNetReceiver("127.0.0.1", {0}, _quiet())
    c = TestController(rx)
    c.set_value(0xFF)
    assert c.current().value == 0xFF
    c.clear()
    assert c.current() is None
    state = c.state()
    assert state == {"active": False}


def test_override_held_during_min_hold_even_with_active_dmx(artdmx):
    rx = ArtNetReceiver("127.0.0.1", {0}, _quiet())
    clock = _ManualClock()
    c = TestController(rx, min_hold_s=5.0, dmx_active_window_s=1.0, clock=clock)

    # ArtNet has been active recently
    rx.handle(artdmx(0, b"\xAB" * 4))
    rx.buffers[0].last_seen = clock()  # mark seen at t0
    c.set_value(0xFF)

    # 1s in, still within hold window → override stays
    clock.tick(1.0)
    rx.buffers[0].last_seen = clock()
    assert c.current().value == 0xFF

    # 4s in, still within hold window
    clock.tick(3.0)
    rx.buffers[0].last_seen = clock()
    assert c.current().value == 0xFF


def test_override_expires_after_min_hold_when_dmx_active(artdmx):
    rx = ArtNetReceiver("127.0.0.1", {0}, _quiet())
    clock = _ManualClock()
    c = TestController(rx, min_hold_s=5.0, dmx_active_window_s=1.0, clock=clock)
    c.set_value(0x80)

    # Past the min hold AND DMX is currently active → expire.
    clock.tick(6.0)
    rx.handle(artdmx(0, b"\xCC" * 4))
    rx.buffers[0].last_seen = clock()
    assert c.current() is None
    assert c.state() == {"active": False}


def test_override_persists_when_no_dmx_arrives():
    rx = ArtNetReceiver("127.0.0.1", {0}, _quiet())
    clock = _ManualClock()
    c = TestController(rx, min_hold_s=5.0, dmx_active_window_s=1.0, clock=clock)
    c.set_value(0xFF)
    # Long time passes, no ArtNet ever arrives → override stays.
    clock.tick(60.0)
    assert c.current().value == 0xFF
    assert c.state()["active"] is True


def test_override_yields_when_dmx_resumes_after_silence(artdmx):
    rx = ArtNetReceiver("127.0.0.1", {0}, _quiet())
    clock = _ManualClock()
    c = TestController(rx, min_hold_s=5.0, dmx_active_window_s=1.0, clock=clock)
    c.set_value(0xFF)
    clock.tick(60.0)
    assert c.current().value == 0xFF  # held while silent

    # ArtNet resumes
    rx.handle(artdmx(0, b"\x11" * 4))
    rx.buffers[0].last_seen = clock()
    # Now dmx_active is true and elapsed>>min_hold → expires
    assert c.current() is None


def test_dmx_active_flag_reflects_recent_activity(artdmx):
    rx = ArtNetReceiver("127.0.0.1", {0, 1}, _quiet())
    clock = _ManualClock()
    c = TestController(rx, dmx_active_window_s=1.0, clock=clock)

    rx.handle(artdmx(0, b"\xAA"))
    rx.buffers[0].last_seen = clock()
    assert c.dmx_active() == {0: True, 1: False}

    clock.tick(2.0)  # both stale now
    assert c.dmx_active() == {0: False, 1: False}


# ── Sink integration ─────────────────────────────────────────────


def test_poe_sink_writes_override_value_for_every_pixel_byte(
    silent_log, fake_serial, artdmx
):
    rx = ArtNetReceiver("127.0.0.1", {0}, silent_log)
    rx.handle(artdmx(0, b"\x12" * 12))  # source data; should be ignored
    strips = [
        StripMapping(poe_channel=0, universe=0, offset=0, pixel_count=2),
        StripMapping(poe_channel=1, universe=0, offset=6, pixel_count=2),
    ]
    c = TestController(rx, min_hold_s=99)
    c.set_value(0xFF)
    sink = PoeSink(rx, fake_serial, strips, fps=50, log=silent_log,
                   controller=c)
    sink.tx_one_frame()
    out = fake_serial.writes()[0]
    # Body of first set_channel record (offset 13..18) should be 0xFF * 6
    assert out[13:19] == b"\xFF" * 6
    # Two POE records visible by magic-byte count
    assert out.count(POE_MAGIC) == 3  # two set + one draw


def test_poe_sink_returns_to_live_after_clear(
    silent_log, fake_serial, artdmx
):
    rx = ArtNetReceiver("127.0.0.1", {0}, silent_log)
    rx.handle(artdmx(0, b"\xAB" * 6))
    strips = [StripMapping(poe_channel=0, universe=0, offset=0, pixel_count=2)]
    c = TestController(rx, min_hold_s=99)
    sink = PoeSink(rx, fake_serial, strips, fps=50, log=silent_log,
                   controller=c)

    c.set_value(0xFF)
    sink.tx_one_frame()
    assert fake_serial.writes()[0][13:19] == b"\xFF" * 6

    c.clear()
    sink.tx_one_frame()
    assert fake_serial.writes()[1][13:19] == b"\xAB" * 6


def test_dmx_sink_override_paints_each_fixtures_slots(silent_log, fake_serial):
    """Override fills each fixture's allocated DMX slots; unmapped slots
    stay at zero. Two fixtures so we verify the placement, not just a
    single block."""
    rx = ArtNetReceiver("127.0.0.1", {0}, silent_log)
    fixtures = [
        DmxFixture(universe=0, offset=0,  dmx_start=1,  length=8),
        DmxFixture(universe=0, offset=20, dmx_start=20, length=4),
    ]
    c = TestController(rx, min_hold_s=99)
    c.set_value(0x80)
    sink = DmxSink(
        rx, fake_serial, fixtures,
        protocol=PROTOCOL_ENTTEC_PRO, fps=40, log=silent_log,
        controller=c,
    )
    sink.tx_one_frame()
    frame = fake_serial.writes()[0]
    slots = frame[5:5 + DMX_SLOT_COUNT]
    # Fixture 1: slots 1..8 (0-based: 0..7) = 0x80
    assert slots[0:8] == b"\x80" * 8
    # Gap: slots 9..19 (0-based: 8..18) = 0x00
    assert slots[8:19] == bytes(11)
    # Fixture 2: slots 20..23 (0-based: 19..22) = 0x80
    assert slots[19:23] == b"\x80" * 4
    # Tail unaffected
    assert slots[23:30] == bytes(7)
