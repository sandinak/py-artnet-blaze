"""DMX framing + DmxSink behavior for both protocols."""

from __future__ import annotations

import struct

import pytest

from artnet_blaze.artnet import ArtNetReceiver
from artnet_blaze.dmx import (
    DMX_SLOT_COUNT,
    DMX_START_CODE,
    ENTTEC_END,
    ENTTEC_LABEL_SEND_DMX,
    ENTTEC_START,
    PROTOCOL_ENTTEC_PRO,
    PROTOCOL_OPEN_DMX,
    DmxFixture,
    DmxSink,
    build_dmx_frame_payload,
    build_enttec_pro_frame,
    merge_fixtures,
)


def test_enttec_pro_frame_layout():
    slots = bytes(range(256)) + bytes(range(256))  # 512 bytes
    frame = build_enttec_pro_frame(slots)
    assert frame[0] == ENTTEC_START
    assert frame[1] == ENTTEC_LABEL_SEND_DMX
    (length,) = struct.unpack("<H", frame[2:4])
    assert length == 513  # start code + 512 slots
    assert frame[4] == DMX_START_CODE
    assert frame[5:5 + 512] == slots
    assert frame[-1] == ENTTEC_END
    assert len(frame) == 6 + 512


def test_enttec_pro_rejects_wrong_slot_count():
    with pytest.raises(ValueError):
        build_enttec_pro_frame(bytes(511))


def test_open_dmx_payload_layout():
    slots = b"\xAA" * 512
    payload = build_dmx_frame_payload(slots)
    assert payload[0] == DMX_START_CODE
    assert payload[1:] == slots
    assert len(payload) == 513


def test_merge_fixtures_copies_into_correct_slots():
    universe_data = bytes(range(256)) + bytes(range(256))
    universes = {0: universe_data}
    fixtures = [
        DmxFixture(universe=0, offset=384, dmx_start=1,  length=24),
        DmxFixture(universe=0, offset=408, dmx_start=25, length=24),
    ]
    out = merge_fixtures(universes, fixtures)
    assert len(out) == DMX_SLOT_COUNT
    # Slots 1..24 carry universe[384:408]
    assert bytes(out[0:24]) == universe_data[384:408]
    # Slots 25..48 carry universe[408:432]
    assert bytes(out[24:48]) == universe_data[408:432]
    # Everything else stays zero
    assert bytes(out[48:]) == bytes(DMX_SLOT_COUNT - 48)


def test_merge_fixtures_skips_unseen_universe():
    fixtures = [DmxFixture(universe=7, offset=0, dmx_start=1, length=10)]
    out = merge_fixtures({}, fixtures)
    assert bytes(out) == bytes(DMX_SLOT_COUNT)


def test_merge_fixtures_truncates_at_universe_end():
    universes = {0: b"\x11" * 100}  # short universe (test edge)
    fixtures = [DmxFixture(universe=0, offset=90, dmx_start=1, length=20)]
    out = merge_fixtures(universes, fixtures)
    assert bytes(out[0:10]) == b"\x11" * 10
    # Past the universe end, we don't write anything.
    assert bytes(out[10:20]) == bytes(10)


def test_dmx_sink_enttec_pro_writes_one_frame_per_tick(
    silent_log, fake_serial, artdmx
):
    rx = ArtNetReceiver("127.0.0.1", {4}, silent_log)
    rx.handle(artdmx(universe=4, data=b"\xCC" * 24))
    fixtures = [DmxFixture(universe=4, offset=0, dmx_start=1, length=24)]
    sink = DmxSink(
        rx, fake_serial, fixtures,
        protocol=PROTOCOL_ENTTEC_PRO, fps=40, log=silent_log,
    )
    sink.tx_one_frame()
    writes = fake_serial.writes()
    assert len(writes) == 1
    frame = writes[0]
    assert frame[0] == ENTTEC_START
    assert frame[-1] == ENTTEC_END
    # Slots 1..24 should be 0xCC
    assert frame[5:5 + 24] == b"\xCC" * 24
    # Slot 25 onwards should be zero
    assert frame[5 + 24:5 + 30] == b"\x00" * 6


def test_dmx_sink_open_dmx_emits_break_mab_then_payload(
    silent_log, fake_serial, artdmx
):
    rx = ArtNetReceiver("127.0.0.1", {0}, silent_log)
    rx.handle(artdmx(universe=0, data=b"\xAB" * 4))
    fixtures = [DmxFixture(universe=0, offset=0, dmx_start=1, length=4)]
    sleep_calls: list[float] = []
    sink = DmxSink(
        rx, fake_serial, fixtures,
        protocol=PROTOCOL_OPEN_DMX, fps=40, log=silent_log,
        sleep_fn=sleep_calls.append,
    )
    sink.tx_one_frame()

    # Expected event sequence: break=True, sleep, break=False, sleep, write
    kinds = [e[0] for e in fake_serial.events]
    assert kinds == ["break", "break", "write"]
    assert fake_serial.events[0][1] is True   # asserted
    assert fake_serial.events[1][1] is False  # released
    # Two sleep calls bracket the two break transitions
    assert len(sleep_calls) == 2
    # BREAK ≥ 88 µs, MAB ≥ 8 µs
    assert sleep_calls[0] >= 88e-6
    assert sleep_calls[1] >= 8e-6
    # Payload starts with start code 0x00, then our 4 bytes, then zeros
    payload = fake_serial.events[2][1]
    assert payload[0] == 0x00
    assert payload[1:5] == b"\xAB" * 4
    assert len(payload) == 513


def test_dmx_sink_blackout_sends_all_zero_slots(
    silent_log, fake_serial
):
    rx = ArtNetReceiver("127.0.0.1", {0}, silent_log)
    fixtures = [DmxFixture(universe=0, offset=0, dmx_start=1, length=8)]
    sink = DmxSink(
        rx, fake_serial, fixtures,
        protocol=PROTOCOL_ENTTEC_PRO, fps=40, log=silent_log,
    )
    sink.blackout()
    frame = fake_serial.writes()[0]
    assert frame[5:5 + DMX_SLOT_COUNT] == bytes(DMX_SLOT_COUNT)


def test_dmx_sink_rejects_unknown_protocol(silent_log, fake_serial):
    rx = ArtNetReceiver("127.0.0.1", {0}, silent_log)
    with pytest.raises(ValueError):
        DmxSink(
            rx, fake_serial, [],
            protocol="bogus", fps=40, log=silent_log,
        )
