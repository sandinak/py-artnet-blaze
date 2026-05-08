"""End-to-end: ArtDmx in → POE bytes + DMX bytes out, no real I/O."""

from __future__ import annotations

from artnet_blaze.artnet import ArtNetReceiver
from artnet_blaze.dmx import (
    DMX_SLOT_COUNT,
    PROTOCOL_ENTTEC_PRO,
    DmxFixture,
    DmxSink,
)
from artnet_blaze.poe import PoeSink, StripMapping


def test_universe_piggybacks_poe_and_dmx(silent_log, artdmx):
    """Universe 0 carries pixel data in slots 0..383 and bar-light DMX
    starting at byte 384. The same ArtDmx packet drives both sinks."""
    from tests.conftest import FakeSerial

    poe_port = FakeSerial()
    dmx_port = FakeSerial()

    # Build a 432-byte payload: 384 bytes of pixel data + 48 bytes of DMX.
    pixel_bytes = bytes([0x10, 0x20, 0x30] * 128)   # 384 bytes
    dmx_bytes = bytes(range(48))                    # 48 bytes
    universe_payload = pixel_bytes + dmx_bytes

    rx = ArtNetReceiver("127.0.0.1", {0}, silent_log)
    rx.handle(artdmx(universe=0, data=universe_payload))

    # 2 strips × 64 LEDs share universe 0 at offsets 0 and 192.
    strips = [
        StripMapping(poe_channel=0, universe=0, offset=0,   pixel_count=64),
        StripMapping(poe_channel=1, universe=0, offset=192, pixel_count=64),
    ]
    fixtures = [
        DmxFixture(universe=0, offset=384, dmx_start=1,  length=24),
        DmxFixture(universe=0, offset=408, dmx_start=25, length=24),
    ]

    poe = PoeSink(rx, poe_port, strips, fps=50, log=silent_log)
    dmx = DmxSink(
        rx, dmx_port, fixtures,
        protocol=PROTOCOL_ENTTEC_PRO, fps=40, log=silent_log,
    )

    poe.tx_one_frame()
    dmx.tx_one_frame()

    # POE: pixel data for both strips appears verbatim in payload regions.
    poe_out = poe_port.writes()[0]
    # First strip set_channel record body starts at byte 13
    assert poe_out[13:13 + 192] == pixel_bytes[0:192]
    # Second strip record starts after first record (13 + 192 + 4 = 209)
    s2 = 13 + 192 + 4
    assert poe_out[s2 + 13: s2 + 13 + 192] == pixel_bytes[192:384]

    # DMX: slots 1..48 carry the bar-light bytes from offset 384.
    dmx_out = dmx_port.writes()[0]
    assert dmx_out[5:5 + 48] == dmx_bytes
    # Remaining slots zero
    assert dmx_out[5 + 48:5 + DMX_SLOT_COUNT] == bytes(DMX_SLOT_COUNT - 48)


def test_dedicated_dmx_universe(silent_log, artdmx):
    """POE on universes 0..3, DMX on its own universe 4."""
    from tests.conftest import FakeSerial

    poe_port = FakeSerial()
    dmx_port = FakeSerial()

    rx = ArtNetReceiver("127.0.0.1", {0, 4}, silent_log)
    # POE strip on universe 0
    rx.handle(artdmx(universe=0, data=b"\x55" * 192))
    # Bar lights on universe 4
    rx.handle(artdmx(universe=4, data=bytes(range(48))))

    strips = [StripMapping(poe_channel=0, universe=0, offset=0, pixel_count=64)]
    fixtures = [DmxFixture(universe=4, offset=0, dmx_start=1, length=48)]

    poe = PoeSink(rx, poe_port, strips, fps=50, log=silent_log)
    dmx = DmxSink(
        rx, dmx_port, fixtures,
        protocol=PROTOCOL_ENTTEC_PRO, fps=40, log=silent_log,
    )

    poe.tx_one_frame()
    dmx.tx_one_frame()

    assert poe_port.writes()[0][13:13 + 192] == b"\x55" * 192
    assert dmx_port.writes()[0][5:5 + 48] == bytes(range(48))
